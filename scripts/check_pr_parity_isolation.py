"""issue #348 gate: 一份 parity 的临时现场只能被它自己收掉。

现场：同机两处 parity 同时活着时，其中一边的门禁整片报
`can't open file '.../idlejot-parity-XXXX/pristine/scripts/<gate>.py'`——树在跑的过程中
被另一边从底下抽走了。原先的收尾是「`remove` 自己的 → `prune` 全仓 → `rmtree`」，
且建树前不核对登记，所以任意一边都能抽掉另一边的树。

本门禁把修法的各条分别钉住，每条都配「不修就会红」的对照：

1. **跨进程互斥**：`check_pr_parity_lock` 的字节锁真能挡住第二个**进程**；同一个进程里
   两个句柄也互斥（钩子同进程连跑两次 parity 靠的就是这一条）；
2. **只认自己那份**：登记表按规范化路径比对；收尾只 `remove` 自己那条，别人正在用的树
   一格都不许动；
3. **不静默**：收不掉的残留要说进 `Report.problems`，不许一声不响地当成功；
4. **孤儿登记不复用**：登记在、目录不在时必须按失败处理并提示 `prune`，不许把一棵不存在
   的树当成可复用现场（取消全局 `prune` 之后的新坑，issue #348 复核第 ② 条）；
5. **`main()` 持锁接线**：跑真的 `check_pr_parity.main()`，锁被占期间它不许进入 `_parity`，
   放锁之后才进——只验锁模块本身证明不了 `main()` 用上了它（issue #348 复核第 ① 条）；
6. **收尾不误伤别人的登记**：本门禁收尾时若表里还有**现场之外活着**的 `idlejot-parity-*`
   树，`unregister_scene()` 必须拒绝全仓 prune、只把本现场的残留说出来——这是硬断言；
   旁人只剩**孤儿登记**（目录早没了）时才可能放行 `prune`，而这一步有前置条件：环境里
   本来就有旁树活着时打印 `SKIP` 不判红（issue #378 C1：环境脏不该染红），干净前提下
   仍留覆盖。
7. **收尾本身不再制造残留（issue #378 非破坏性部分）**：收尾某一步抛 `OSError` 时后续
   步骤仍执行且失败被说出来；建树中途失败不留半截现场；`fail` 分支先摘登记再删目录
   不造孤儿登记；`--keep` 给出可复制清理命令。清理只认 `idlejot-parity-` 前缀，绝不碰
   `.tmp/wt-*` / `tulpa-*`（前缀约束落成断言，不只写在注释里）。
8. **夹具自建自摘、登记表零差异（issue #378 卫生部分）**：用例造出的每条登记都在
   `finally` 里用 `git worktree remove --force` **只摘自己那一条**，目录已经不在的孤儿
   登记也一样（不把收尾寄托给全仓 `prune`，旁树活着时它本来就会被拦）；整个门禁跑完
   后 `git worktree list --porcelain` 与 `.git/worktrees` 条目相对跑前必须逐条一致，
   夹具漏登记就翻红。`prunable` 条数只作 WARN 级盘点（#378 自动回收的观测入口）。

9. **`--reclaim` 只报告档（issue #378 回收设计第一档）**：候选必须同时满足四条——前缀只认
   `idlejot-parity-*`（`.tmp/wt-*` / `tulpa-*` / `parity-348-*` 一律不进候选）、`pristine`
   不是活树、没有进程引用（按现场名扫 `Win32_Process` 的 CommandLine）、不在 `--keep` 现场
   （最后一条当前**不可判定**：没有标记机制，只提示不排除）。这一档**只打印命令、不删除**，
   断言里加一条反向对照（把前缀放宽到 `tulpa-*` 后该目录必进候选）证明判据不是空转。

本门禁**自己不跑全仓 prune 来造现场**：`cleanup()` 明文不调 `git worktree prune`，因为那是
全仓写操作。用例要的「真树」换一个 scratch 直接建就行；收尾也不再依赖 `prune`——本门禁
自己造出来的孤儿登记逐条 `git worktree remove --force` 摘掉（`forget_registration()`），
`unregister_scene()` 的 `prune` 分支只在环境干净（现场之外没有活树）时才走得到。

跑法：`python scripts/check_pr_parity_isolation.py`（本机手跑；它建真 worktree，量级与
`check_pr_parity.py` 同档，所以不在 CI 分片里跑）。
"""
import contextlib
from io import StringIO
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import check_pr_parity as parity  # noqa: E402
import check_pr_parity_lock as lock  # noqa: E402

FAILS = []
LOCK_SCRIPT = SCRIPTS / 'check_pr_parity_lock.py'


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f'{name}：期望 {want!r}，实得 {got!r}')
    print(f'{"ok  " if ok else "FAIL"} {name}')


@contextlib.contextmanager
def patched(**attributes):
    """临时替换 `check_pr_parity` 的函数/属性，出块必还原（异常路径也一样）。"""
    saved = {name: getattr(parity, name) for name in attributes}
    for name, value in attributes.items():
        setattr(parity, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(parity, name, value)


def call_parity(args, sha):
    """跑一次 `_parity()` 并捕获两路输出；替身函数由 `patched()` 负责还原。"""
    out, err = StringIO(), StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = parity._parity(args, sha)
    return code, out.getvalue(), err.getvalue()


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True,
                          encoding='utf-8')


def scene_env(scene):
    env = dict(os.environ, **{lock.HOME_ENV: str(scene), lock.LOCK_ENV: str(scene / lock.LOCK_NAME)})
    env.pop('PYTHONPATH', None)
    return env


def lock_cli(scene, *args):
    return subprocess.run([sys.executable, str(LOCK_SCRIPT), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding='utf-8',
                          env=scene_env(scene), timeout=120)


def head_sha():
    return git('rev-parse', 'HEAD').stdout.strip()


def scene_paths(pattern):
    r"""登记表里路径落在 `scene`（`pattern` 通配）内的那些条目——`git worktree list --porcelain` 的逐行产物。

    匹配走**规范化后的精确前缀**，不走 `fnmatch`：`git worktree list` 打的是正斜杠路径
    （`/` 分隔），`Path` 拼出来的是反斜杠；而 `fnmatch` 在 Windows 上不把分隔符当成可匹配
    字符，`<根>\a\b` 匹配不上 `<根>\a\*`。所以统一成 `PureWindowsPath` 两侧比，前缀比较
    交给 `is_relative_to()`，不自己拼分隔符（`check_path_hygiene.py` 的口径）。
    """
    out = git('worktree', 'list', '--porcelain').stdout.splitlines()
    want_all = pattern == '*'
    base = None if want_all else PureWindowsPath(os.path.normcase(os.path.normpath(pattern)))
    hits = []
    prefix = 'worktree '
    for index, line in enumerate(out):
        if not line.startswith(prefix):
            continue
        path = line[len(prefix):]
        if base is not None:
            here = PureWindowsPath(os.path.normcase(os.path.normpath(path)))
            # 严格前缀：`scene` 本身不算「落在里面」，否则现场目录自己的登记会被算进来。
            if here == base or not here.is_relative_to(base):
                continue
        fields = {}
        for follow in out[index + 1:]:
            if not follow.strip():
                break
            key, _, value = follow.partition(' ')
            fields[key] = value or True
        hits.append((path, fields))
    return hits


def unregister_scene(scene):
    """把**本门禁自己**留下的登记一条条摘掉：先 `worktree remove --force`，摘不掉的
    （目录已经不在、登记还在）才 `prune`。

    这不是「收尾顺手 prune」——`cleanup()` 明文不做全仓 prune，是因为那会清掉**别人**的
    登记。这里 prune 的正当性只来自一个事实：那一刻表里**活着的** `idlejot-parity-*` 树
    全部落在本轮 `mkdtemp(prefix='parity-348-')` 的现场里，而现场目录已经在上一行删掉了。
    所以先断言「现场条目之外没有别的活着的 idlejot-parity-* 树」，断言不过就不 prune，
    只把残留说出来——别人的现场一格都不碰。旁人只剩孤儿登记时不拦（见 `_unregister()`）。

    返回值是 `(还没摘掉的孤儿, 摘完之后本现场是否干净)`：调用方（[9] 的两组对照）不采信
    自己的自述，只看第二个值。`main()` 的收尾**不再**走这里——本门禁的登记逐条自摘
    （`forget_registration()`），否则全仓 `prune` 会把跑前就存在的孤儿一并清掉，让
    「跑前/跑后逐条一致」的回归断言失去意义。
    """
    return _unregister(scene)[0], _scene_clean(scene)


def _scene_clean(scene):
    """本现场还剩没剩登记——现场目录已经删了，还剩就一定是没摘干净的孤儿。"""
    return not scene_paths(str(scene))


def _alive_scene(candidate, scene):
    """登记表里这条 `idlejot-parity-*` 是不是一棵**活着**的树：上一轮硬杀留下的活树。

    这是收尾时唯一能拦住全仓 `prune` 的判据，两边的坑都踩过了：
    - 只看登记表有没有 `prunable` **不够**：`git worktree list` 对「目录还在、`.git` 却指着
      一条已经不存在的登记」不标 `prunable`（本机实测：`.git` 指向 `.git/worktrees/pristine1`，
      那条登记已被别人 prune 掉），`scripts/gates.json` 照样在；
    - 只看目录在不在也不够：目录刚被 `rmtree`、登记还没反应过来的那段中间态目录已经不在。
    所以按 `_usable()` 同一口径摸盘（目录在 + `scripts/gates.json` 在），**并且**登记表没标
    `prunable`。

    本轮的树（`scene` 底下）一律算活着：调用方（`main()` 的收尾）在 `rmtree` 现场之后才调
    这里，那时**一切的**孤儿都是本门禁自己留下的，全是死账。把它们算成活的会让 `prune` 永远
    拦在自己身上——`unregister_scene()` 的第一句断言就永远过不去。
    """
    path = os.path.normcase(os.path.normpath(str(candidate)))
    here = PureWindowsPath(path)
    base = PureWindowsPath(os.path.normcase(os.path.normpath(str(scene))))
    if here == base or here.is_relative_to(base):
        return False
    fields = _scene_registry().get(os.path.normpath(str(candidate)), {})
    return 'prunable' not in fields and parity._usable(candidate)


def _worktree_fields():
    """`git worktree list --porcelain` 里每棵树 → 它的特征字段（`prunable` 在值里）。"""
    registry = {}
    current = None
    for line in git('worktree', 'list', '--porcelain').stdout.splitlines():
        if line.startswith('worktree '):
            current = line[len('worktree '):]
            registry[os.path.normpath(current)] = {}
        elif current is not None and line:
            key, _, value = line.partition(' ')
            registry[os.path.normpath(current)][key] = value
    return registry


def _scene_registry():
    """`git worktree list --porcelain` 里每个 `idlejot-parity-*` 树 → 它的特征字段。"""
    return {path: fields for path, fields in _worktree_fields().items()
            if 'idlejot-parity-' in path}


def _unregister(scene):
    """`unregister_scene()` 的本体，多回一个 `pruned` 供对照用例断言「这次到底动没动全仓」。"""
    own = scene_paths(str(scene))
    losers = [(path, fields) for path, fields in own if 'prunable' in fields]
    if not losers:
        return [], False
    mine = {os.path.normcase(os.path.normpath(p)) for p, _ in own}
    others = [path for path in _scene_registry() if 'idlejot-parity-' in path
              and os.path.normcase(os.path.normpath(path)) not in mine]
    # 旁人的死活是唯一能拦住 `prune` 的东西（`cleanup()` 明文不做全仓 prune，正是因为
    # `prune` 会清掉别人的登记）。只有**活着**的才拦：别人的活树可能在跑门禁；
    # 只剩孤儿登记（目录早没了、`prune` 正是给它们准备的）时不拦，否则一台跑过几次
    # parity 的机器上「没有旁人时才 prune」的对照永远绿不了，「永远不 prune」被偷偷钉死
    # ——那正是 issue #348 复核第 3 轮第 ① 条。
    live = [path for path in others if _alive_scene(path, scene)]
    if live:
        print(f'WARN 现场之外还有 {len(live)} 棵 idlejot-parity-* 树活着'
              f'（{[scene_rel(scene, p) for p in live]}），为免误伤不 prune；'
              f'本门禁留下 {len(losers)} 条孤儿登记：{[scene_rel(scene, p) for p, _ in losers]}')
        return losers, False
    git('worktree', 'prune')
    if scene_paths(str(scene)):
        print(f'WARN 本门禁的孤儿登记 prune 之后仍在：'
              f'{[scene_rel(scene, p) for p, _ in scene_paths(str(scene))]}')
    return [], True


def scene_rel(scene, path):
    """把登记路径写成相对本现场的形态（`scratch/pristine`），报错与断言都用这个口径。"""
    return Path(os.path.relpath(os.path.normpath(path),
                                os.path.normpath(str(scene)))).as_posix()


def forget_registration(path):
    """把登记表里 `path` 这一条**只这一条**摘掉，绝不 prune 全仓（issue #378 卫生部分）。

    目录已经不在的孤儿登记同样适用：`git worktree remove --force` 会按登记表找回那条
    记录（实测目录连同父目录都不在时也能摘掉）。返回 `None` 表示摘掉或本来就没有；
    否则回传原因，由调用方打印可复制命令——删不净不许静默。
    """
    if path is None:
        return None
    path = Path(path)
    if not parity.is_registered(path):
        return None
    result = git('worktree', 'remove', '--force', str(path))
    if result.returncode == 0 and not parity.is_registered(path):
        return None
    return parity.output_of(result) or f'rc={result.returncode}，登记仍在'


def forget_or_report(path):
    """`forget_registration()` 的收尾包装：摘不掉就打印原因与可直接粘贴的清理命令。"""
    reason = forget_registration(path)
    if reason is not None:
        print(f'WARN 夹具登记没摘掉 — {path}：{reason}', file=sys.stderr)
        print(f'WARN 手工清理命令 — git worktree remove --force "{path}"', file=sys.stderr)
    return reason is None


def registry_state():
    """真实仓库登记表的可核对快照：worktree 路径 + `.git/worktrees` 条目名。"""
    paths = tuple(sorted(path for path, _ in scene_paths('*')))
    base = Path(git('rev-parse', '--git-common-dir').stdout.strip())
    if not base.is_absolute():
        base = ROOT / base
    entries = base / 'worktrees'
    names = tuple(sorted(entry.name for entry in entries.iterdir())) if entries.is_dir() else ()
    return paths, names


def registry_diff(before, after):
    """两次快照的差异逐条写出来；空列表 = 登记表逐条一致（issue #378 回归断言）。"""
    old_paths, old_entries = before
    new_paths, new_entries = after
    problems = []
    added = sorted(set(new_paths) - set(old_paths))
    removed = sorted(set(old_paths) - set(new_paths))
    if added:
        problems.append(f'登记表多了 worktree：{added}')
    if removed:
        problems.append(f'登记表少了 worktree：{removed}')
    if new_entries != old_entries:
        problems.append(f'.git/worktrees 条目不一致：{len(old_entries)} → {len(new_entries)}，'
                        f'差异 {sorted(set(new_entries) ^ set(old_entries))}')
    return problems


# prunable 盘点只看这两类前缀：`idlejot-parity-*` 是 parity 现场，`parity-348-*` 是本门禁
# 自己的现场（issue #378 的自动回收观测入口）。
PRUNABLE_PREFIXES = ('idlejot-parity-', 'parity-348-')


def _has_prefix(path, prefix):
    """路径里有没有一段以 `prefix` 开头的目录名（`.../idlejot-parity-x/dead/pristine` 也算）。"""
    return any(part.lower().startswith(prefix) for part in PureWindowsPath(path).parts)


def prunable_report(label):
    """WARN 级盘点：各现场前缀还剩多少条 prunable——#378 自动回收的观测入口。

    只打印、不判红：本机陈年残留是环境前提，不该把门禁染成红灯（issue #378 C1）。
    """
    fields = _worktree_fields()
    for prefix in PRUNABLE_PREFIXES:
        hits = sorted(path for path, row in fields.items()
                      if 'prunable' in row and _has_prefix(path, prefix))
        print(f'WARN 登记表健康度（{label}）：{prefix}* prunable {len(hits)} 条')
        for path in hits:
            print(f'WARN 可 prune：{path}')


def wait_for(predicate, seconds=30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return bool(predicate())


def case_lock_is_cross_process(scene):
    """1. 跨进程：一个进程持锁期间别的进程拿不到，收掉持锁进程即恢复。"""
    print('\n[1] 跨进程互斥')
    holder = subprocess.Popen([sys.executable, str(LOCK_SCRIPT), '--hold', '--ref', 'case-1'],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, cwd=ROOT, env=scene_env(scene),
                              text=True, encoding='utf-8')
    try:
        wait_for(lambda: (scene / lock.HOLDER_NAME).exists())
        busy = lock_cli(scene, '--probe')
        check('持锁期间 probe rc=1', busy.returncode, 1)
        check('probe 说出持锁者 ref', 'case-1' in busy.stdout, True)
        check('持锁期间 serialize rc=1', lock_cli(scene, '--serialize').returncode, 1)
    finally:
        # --hold 靠**进程退出**放锁（不靠 stdin EOF）：句柄继承给子进程时关 stdin 不放锁，
        # 拿「关 stdin」当释放手段就会在这里挂满 120s（issue #348 复核第 ③ 条）。
        lock.terminate(holder)
    check('持锁进程被收掉', holder.poll() is not None, True)
    # 对照：同一条命令在没人拿着时必须反过来，否则上面那句 rc=1 什么都不证明。
    check('释放后 probe rc=0（对照）', lock_cli(scene, '--probe').returncode, 0)
    check('释放后 serialize rc=0', lock_cli(scene, '--serialize').returncode, 0)


def case_same_process_two_handles(scene):
    """2. 同进程两个句柄也互斥；释放后可重拿。"""
    print('\n[2] 同进程重入互斥')
    lockfile = scene / lock.LOCK_NAME
    first = lock.acquire(lockfile)
    check('第一个句柄拿到锁', first is not None, True)
    check('第二个句柄拿不到（同一进程）', lock.acquire(lockfile), None)
    lock.release(first, lockfile)
    second = lock.acquire(lockfile)
    check('释放后第二个句柄拿得到', second is not None, True)
    lock.release(second, lockfile)


def case_lock_serializes_threads(scene):
    """3. 两个线程抢同一把锁时，临界区互不重叠（等的时候真的在等）。"""
    print('\n[3] 等锁串行化')
    lockfile = scene / lock.LOCK_NAME
    previous = {key: os.environ.get(key) for key in (lock.HOME_ENV, lock.LOCK_ENV)}
    os.environ[lock.HOME_ENV] = str(scene)
    os.environ[lock.LOCK_ENV] = str(lockfile)
    seen, guard = [], threading.Lock()
    inside = []

    def worker(name):
        with lock.held(name):
            with guard:
                seen.append(name)
                inside.append(name)
            time.sleep(0.4)
            with guard:
                inside.remove(name)

    try:
        threads = [threading.Thread(target=worker, args=(n,)) for n in ('a', 'b')]
        for thread in threads:
            thread.start()
        peak = 0
        deadline = time.monotonic() + 120
        while any(t.is_alive() for t in threads) and time.monotonic() < deadline:
            with guard:
                peak = max(peak, len(inside))
            time.sleep(0.05)
        for thread in threads:
            thread.join(timeout=120)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    check('两个线程都进了临界区', sorted(seen), ['a', 'b'])
    check('临界区最多同时一个人', peak, 1)


def case_registration(scene):
    """4. 建树前先核对登记；收尾只动自己那份。"""
    print('\n[4] 只认自己那份')
    sha = head_sha()
    scratch = scene / 'own'
    status, path, reason = parity.add_worktree(sha, scratch)
    try:
        check('建树状态', status, parity.ADD_OK)
        check('登记表认得返回的路径', parity.is_registered(path), True)
        check('返回的就是 pristine 目录', Path(path).name, 'pristine')
        again, same, _ = parity.add_worktree(sha, scratch)
        check('重复调用复用同一棵（不重复 add）', (again, same), ('reuse', Path(path)))
        report = parity.cleanup(path)
        check('收尾 ok', report.ok, True)
        check('收尾后登记已清', parity.is_registered(path), False)
        check('收尾后目录已清', Path(path).exists(), False)
    finally:
        forget_or_report(path)


def case_prune_spares_others(scene):
    """5. 收尾不许连坐别人正在用的树（#348 的原现场）。"""
    print('\n[5] 不连坐')
    sha = head_sha()
    status, mine, _ = parity.add_worktree(sha, scene / 'mine')
    theirs = scene / 'theirs' / 'pristine'
    try:
        check('自己的树建好', status, parity.ADD_OK)
        added = git('worktree', 'add', '--detach', str(theirs), sha)
        check('别人的树建好', added.returncode, 0)
        try:
            parity.cleanup(mine)
            check('别人的树还在盘上', (theirs / 'scripts' / 'gates.json').is_file(), True)
            check('别人的树还在登记里', parity.is_registered(theirs), True)
        finally:
            git('worktree', 'remove', '--force', str(theirs))
    finally:
        forget_or_report(mine)


def case_cleanup_not_silent(scene):
    """6. 收得掉就不许报问题；登记被抹掉时必须说出来（不静默）。"""
    print('\n[6] 不静默')
    sha = head_sha()
    first = second = None
    try:
        status, first, _ = parity.add_worktree(sha, scene / 'gone')
        check('树建好', status, parity.ADD_OK)
        clean = parity.cleanup(first)
        check('正常收尾 ok', clean.ok, True)
        check('正常收尾不报问题（对照）', clean.problems, [])
        check('正常收尾确实 remove 过', clean.removed, True)

        status, second, _ = parity.add_worktree(sha, scene / 'orphan')
        check('第二棵树建好', status, parity.ADD_OK)
        # 把登记抹掉（模拟上一轮硬杀留下的孤儿目录，或别处 prune 过）：此时 remove 走不下去，
        # 收尾必须把「登记表里没有这棵树」记进 problems，而不是静默 ok。
        git('worktree', 'remove', '--force', str(second))
        Path(second).mkdir(parents=True, exist_ok=True)
        (Path(second) / 'leftover.txt').write_text('x', encoding='utf-8')
        report = parity.cleanup(second)
        check('孤儿目录如实记账', bool(report.problems), True)
        check('孤儿目录没走 remove 路径', report.removed, False)
        check('孤儿目录仍被删掉', Path(second).exists(), False)
    finally:
        forget_or_report(first)
        forget_or_report(second)


def case_orphan_not_reused(scene):
    """7. 登记在、目录不在的孤儿不许被当成可复用现场（砍掉全局 prune 之后的新坑）。"""
    print('\n[7] 孤儿登记不复用')
    sha = head_sha()
    scratch = scene / 'orphan-reuse'
    orphan = control = None
    try:
        status, orphan, _ = parity.add_worktree(sha, scratch)
        check('先建一棵真树', status, parity.ADD_OK)
        # 只拆目录、留登记：正是上一轮被硬杀（或别处 prune 掉目录）之后的现场。
        # 等登记真的变成 prunable 再往下走：`rmtree` 返回后 `git worktree list` 可能还认那棵树
        # 一两个 tick（Windows 删目录是延迟生效的），不等就会看到「目录不在、但也不是孤儿」的
        # 中间态，后面那句「孤儿不许 reuse」会误红。
        shutil.rmtree(orphan)
        check('目录已拆掉但登记还在', parity.is_registered(orphan), True)
        check('登记已变成 prunable',
              wait_for(lambda: any('prunable' in fields for _, fields in scene_paths(str(scene)))), True)

        again, reused, reason = parity.add_worktree(sha, scratch)
        check('孤儿登记不许 reuse', again, 'fail')
        check('孤儿不给路径', reused, None)
        check('孤儿原因点名 prune', 'prune' in (reason or ''), True)
        # 对照：登记与目录都在时才必须 reuse（否则上面那条 fail 什么都不证明）。
        # **不跑 `git worktree prune` 来造这棵对照树**——那是全仓写操作，正是 `cleanup()`
        # 明文不做的事（`check_pr_parity.py:cleanup` docstring），在门禁里跑一遍等于把
        # 别人的孤儿登记顺手清掉。换一个 scratch 直接建第二棵真树即可。
        other = scene / 'orphan-reuse-control'
        status, control, _ = parity.add_worktree(sha, other)
        check('对照树建成', status, parity.ADD_OK)
        third, same, _ = parity.add_worktree(sha, other)
        check('真树仍在时才 reuse（对照）', (third, same), ('reuse', Path(control)))
        parity.cleanup(control)
    finally:
        # 孤儿登记目录早没了，靠 `git worktree remove --force` 逐条自摘（不 prune 全仓）。
        forget_or_report(orphan)
        forget_or_report(control)


def case_unregister_spares_others(scene):
    """9. 收尾的孤儿清理只动本现场：现场之外还有**活着**的 `idlejot-parity-*` 时必须不 prune
    （硬断言）；只剩孤儿登记时才有资格放行 prune，而环境本就不干净（旁树活着）时该分支
    打印 SKIP、不判红（issue #378 C1：环境脏不该染红）。"""
    print('\n[9] 收尾不误伤别人的登记')
    sha = head_sha()
    own = scene / 'own-orphan'
    own2 = scene / 'own-orphan-2'
    stranger_root = Path(tempfile.mkdtemp(prefix='idlejot-parity-stranger-'))
    stranger = stranger_root / 'pristine'
    stranger_dead = stranger_root / 'dead' / 'pristine'
    status, own_path, _ = parity.add_worktree(sha, own)
    try:
        check('本现场的树建好', status, parity.ADD_OK)
        shutil.rmtree(own_path)
        check('本现场的孤儿登记还在', parity.is_registered(own_path), True)
        check('本现场的登记已变成 prunable',
              wait_for(lambda: any('prunable' in fields for _, fields in scene_paths(str(scene)))), True)
        # 现场之外的「别人」：换一个不在 `scene` 下的前缀，假装是另一处 parity 留下的登记。
        added = git('worktree', 'add', '--detach', str(stranger), sha)
        check('别人的树建好', added.returncode, 0)

        # 硬断言：别人的树活着时，全仓 prune 一律不许动——本现场的孤儿必须原样留着并报出来。
        # 这一条与机器上有没有别的残留无关，任何环境都必须成立。
        leftovers, clean = unregister_scene(scene)
        check('别人的树活着时不动全仓 prune',
              sorted(scene_rel(scene, p) for p, _ in scene_paths(str(scene))), ['own-orphan/pristine'])
        check('本现场的残留被如实报出',
              sorted(scene_rel(scene, p) for p, _ in leftovers), ['own-orphan/pristine'])
        check('本现场没被判干净', clean, False)
        check('别人的活树原样留着', parity.is_registered(stranger), True)

        shutil.rmtree(stranger)  # 只拆目录，让它的登记也变成 prunable
        check('别人的孤儿登记还在', parity.is_registered(stranger), True)

        # 本现场的登记只有这一条：别的组各自在 finally 里收干净了（[7] 不再把孤儿留给本组）。
        check('本现场的登记只有这一条',
              sorted(scene_rel(scene, p) for p, _ in scene_paths(str(scene))), ['own-orphan/pristine'])

        # 「旁人只剩孤儿登记 → 放行 prune」的前提是现场之外没有活树。本机常有硬杀留下的旁树
        # （#378 对账：c_toyqdz / qj6vp4xk），此时这一支本来就到不了——打印 SKIP，不判红。
        outside_live = [path for path in _scene_registry() if _alive_scene(path, scene)]
        if outside_live:
            print('SKIP [9] 「只剩孤儿登记时放行 prune」：前置不成立'
                  f'（现场之外还有 {len(outside_live)} 棵 idlejot-parity-* 树活着：'
                  f'{[Path(path).parent.name for path in outside_live]}）')
            # 没走 prune，本现场的孤儿就地逐条自摘，给下面的对照一个干净起点。
            forget_or_report(own / 'pristine')
        else:
            # 干净前提下的覆盖：prune 谁也伤不到（本现场孤立 + 旁人只剩孤儿登记）。
            leftovers, clean = unregister_scene(scene)
            check('旁人只剩孤儿登记时放行 prune', scene_paths(str(scene)), [])
            check('prune 之后本现场判干净', clean, True)
            check('别人的孤儿登记没被这次 prune 留着', parity.is_registered(stranger), False)

        # 对照：真的活树旁人必须拦得住——否则上面那条「放行」只是「一律放行」。
        added = git('worktree', 'add', '--detach', str(stranger_dead), sha)
        check('对照用的旁人树建好', added.returncode, 0)
        status, own2_path, _ = parity.add_worktree(sha, own2)
        check('再造一条本现场的孤儿', status, parity.ADD_OK)
        shutil.rmtree(own2_path)
        check('本现场的孤儿登记已变成 prunable',
              wait_for(lambda: 'own-orphan-2/pristine'
                       in {scene_rel(scene, p) for p, f in scene_paths(str(scene))
                           if 'prunable' in f}), True)
        leftovers, clean = unregister_scene(scene)
        check('活树旁人拦住 prune（对照）',
              sorted(scene_rel(scene, p) for p, _ in scene_paths(str(scene))), ['own-orphan-2/pristine'])
        check('本现场的孤儿如实报出',
              sorted(scene_rel(scene, p) for p, _ in leftovers), ['own-orphan-2/pristine'])
        check('活树旁人原样留着（对照）', parity.is_registered(stranger_dead), True)
    finally:
        # 夹具自建自摘（issue #378 卫生）：本组造的登记逐条摘掉，不把收尾寄托给全仓 prune
        # ——旁树活着时它本来就会被拦，寄托给它就一定会留残留。
        forget_or_report(own / 'pristine')
        forget_or_report(own2 / 'pristine')
        forget_or_report(stranger)
        forget_or_report(stranger_dead)
        shutil.rmtree(stranger_root, ignore_errors=True)


def case_main_holds_lock(scene):
    """8. `main()` 真的持锁：锁被占期间绝不进入 `_parity`，放锁之后才进去。

    这是整条修复的接线处——`check_pr_parity.py:main()` 里那段 `lock.acquire(...)`。
    只验 `check_pr_parity_lock` 自身（第 1–3 组）证明不了 `main()` 用上了它：把那段
    删掉，门禁照样全绿。所以这里跑**真的 `main()`**，只把 `_parity` 换成一行记录。
    """
    print('\n[8] main() 持锁接线')
    driver = scene / 'driver.py'
    marker = scene / 'entered.txt'
    driver.write_text(
        'import pathlib, sys\n'
        f'sys.path.insert(0, {str(SCRIPTS)!r})\n'
        'import check_pr_parity as parity\n'
        f'marker = pathlib.Path({str(marker)!r})\n'
        'def fake(args, sha):\n'
        '    marker.write_text("entered", encoding="utf-8")\n'
        '    return 0\n'
        'parity._parity = fake\n'
        'sys.argv = ["check_pr_parity.py", "--ref", sys.argv[1]]\n'
        'sys.exit(parity.main())\n', encoding='utf-8')
    sha = head_sha()
    runner = subprocess.Popen([sys.executable, str(driver), sha], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=scene_env(scene), text=True, encoding='utf-8')
    holder = None
    try:
        holder = lock.acquire(scene / lock.LOCK_NAME, blocking=True)
        check('门禁自己拿着锁', holder is not None, True)
        # 锁在门禁手里时 main() 必须卡在排队上：给足让它跑完 _parity 的时间。
        time.sleep(3.0)
        check('锁被占时没进入 _parity', marker.exists(), False)
        check('锁被占时 main() 还没退出', runner.poll(), None)
    finally:
        lock.release(holder, scene / lock.LOCK_NAME)
    try:
        runner.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        runner.kill()
        runner.communicate(timeout=30)
    check('放锁后 main() 才进入 _parity（对照）', marker.exists(), True)
    check('main() 正常返回', runner.returncode, 0)


def case_cleanup_step_oserror(scene):
    """10. 收尾某步抛 `OSError` 后，后续步骤仍执行且失败被说出来（#378 缺口②）。

    夹具刻意**不建真树**：`cleanup()` 第一步（读登记表）调 `run()` 抛 `OSError`，此时
    目录还在、登记表里没有它（上一轮硬杀的形状）。修复前这里会直接抛出去，后面两次
    `rmtree` 全跳过；修复后要看到目录被删、失败原因与清理命令都打出来。
    """
    print('\n[10] 收尾单步抛错仍继续')
    scratch = scene / 'idlejot-parity-cleanup-error'
    (scratch / 'pristine').mkdir(parents=True)
    (scratch / 'home').mkdir()
    (scratch / 'pristine' / 'leftover.txt').write_text('x', encoding='utf-8')

    def exploding_run(command, cwd, env=None):
        raise OSError('注入：git 不可用')

    out, err = StringIO(), StringIO()
    with patched(run=exploding_run):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            parity._finish_scene(SimpleNamespace(keep=False), scratch, None)
    check('后续步骤仍删掉工作树目录', (scratch / 'pristine').exists(), False)
    check('后续步骤仍删掉现场目录', scratch.exists(), False)
    check('失败原因有日志', '收尾未完成' in err.getvalue(), True)
    check('日志带可复制 remove 命令', 'git worktree remove --force' in err.getvalue(), True)
    check('日志带可复制目录删除命令', 'Remove-Item -LiteralPath' in err.getvalue(), True)


def case_build_failure_no_scene(scene):
    """11. 建树中途抛错不留半截现场（#378 缺口③）。"""
    print('\n[11] 建树中途失败不留半截现场')
    scratch = scene / 'idlejot-parity-build-fail'
    scratch.mkdir()

    def exploding_add(sha, target):
        raise OSError('注入：建树中途失败')

    with patched(add_worktree=exploding_add, make_scratch=lambda: scratch):
        code, out, err = call_parity(SimpleNamespace(shard=None, keep=False), head_sha())
    check('建树抛错返回环境失败码', code, 2)
    check('半截现场已回收', scratch.exists(), False)
    check('抛错原因被说出来', '建树/跑门禁抛错' in err, True)


def case_fail_branch_no_orphan(scene):
    """12. `fail` 分支先摘登记再删目录，不留孤儿登记（#378 缺口④）。"""
    print('\n[12] fail 分支不留孤儿登记')
    scratch = scene / 'idlejot-parity-fail-branch'
    scratch.mkdir()
    real_add = parity.add_worktree

    def add_then_fail(sha, target):
        status, path, reason = real_add(sha, target)
        if status == parity.ADD_OK:
            return 'fail', None, '注入：add 成功但按失败处理'
        return status, path, reason

    with patched(add_worktree=add_then_fail, make_scratch=lambda: scratch):
        code, out, err = call_parity(SimpleNamespace(shard=None, keep=False), head_sha())
    path = scratch / 'pristine'
    try:
        check('fail 分支返回环境失败码', code, 2)
        check('登记已摘掉', parity.is_registered(path), False)
        check('登记表里没有本现场条目', [p for p, _ in scene_paths(str(scratch))], [])
        check('目录已删', path.exists(), False)
        check('现场已删', scratch.exists(), False)
        check('说出建树失败', 'FAIL: 建不起 pristine worktree' in out, True)
    finally:
        forget_or_report(path)


def case_keep_prints_commands(scene):
    """13. `--keep` 打印可复制清理命令，而不只是路径（#378 缺口）。"""
    print('\n[13] --keep 打印清理命令')
    scratch = scene / 'idlejot-parity-keep'
    scratch.mkdir()
    with patched(run_gates=lambda pristine, shard, env: 0, make_scratch=lambda: scratch):
        code, out, err = call_parity(SimpleNamespace(shard=None, keep=True), head_sha())
    path = scratch / 'pristine'
    try:
        check('keep 跑通', code, 0)
        check('keep 打印现场路径', '保留现场' in out, True)
        check('keep 打印 worktree remove 命令', 'git worktree remove --force' in out, True)
        check('keep 打印目录删除命令', 'Remove-Item -LiteralPath' in out, True)
        check('keep 命令含确切现场路径', str(scratch) in out, True)
        report = parity.cleanup(path)
        check('keep 现场可被 cleanup 收掉', report.ok, True)
        check('keep 现场登记已清', parity.is_registered(path), False)
    finally:
        forget_or_report(path)


def case_cleanup_scope(scene):
    """14. 清理只认 `idlejot-parity-` 前缀，绝不碰人工工作树（#378 安全约束）。"""
    print('\n[14] 清理只认 idlejot-parity- 前缀')
    check('本工具现场被认', parity.is_parity_scene(Path('idlejot-parity-abc')), True)
    for foreign in ('.tmp/wt-378', 'tulpa-parity-abc', 'parity-348-abc'):
        check(f'非本工具目录不认：{foreign}', parity.is_parity_scene(Path(foreign)), False)
        raised = None
        try:
            parity.cleanup_commands(Path(foreign))
        except ValueError as error:
            raised = error
        check(f'拒绝为非现场生成清理命令：{foreign}', raised is not None, True)
    commands = parity.cleanup_commands(Path('idlejot-parity-abc'))
    check('本工具现场给出 remove 命令',
          any('git worktree remove --force' in line for line in commands), True)
    check('本工具现场给出目录删除命令',
          any('Remove-Item -LiteralPath' in line for line in commands), True)


def case_forget_registration(scene):
    """15. 孤儿登记逐条自摘：只摘自己那一条，旁人活树一格都不碰（#378 卫生部分）。"""
    print('\n[15] 孤儿登记逐条自摘')
    sha = head_sha()
    orphan = neighbour = None
    try:
        status, orphan, _ = parity.add_worktree(sha, scene / 'forget-orphan')
        check('孤儿树建好', status, parity.ADD_OK)
        shutil.rmtree(orphan)
        check('孤儿登记还在', parity.is_registered(orphan), True)
        status, neighbour, _ = parity.add_worktree(sha, scene / 'forget-neighbour')
        check('旁人树建好', status, parity.ADD_OK)
        reason = forget_registration(orphan)
        check('孤儿登记摘得掉（不 prune 全仓）', reason, None)
        check('孤儿登记已不在表里', parity.is_registered(orphan), False)
        check('旁人活树的登记一格没动', parity.is_registered(neighbour), True)
        check('旁人活树的目录一格没动',
              (Path(neighbour) / 'scripts' / 'gates.json').is_file(), True)
    finally:
        forget_or_report(orphan)
        forget_or_report(neighbour)


def case_registry_snapshot_diff(scene):
    """16. 登记表快照对比：多一条/少一条/条目变化都必须报出差异（#378 回归断言的自检）。"""
    print('\n[16] 登记表快照对比')
    same = (('a', 'b'), ('x', 'y'))
    check('相同快照无差异', registry_diff(same, same), [])
    check('多一条 worktree 有差异',
          bool(registry_diff(same, (('a', 'b', 'c'), ('x', 'y')))), True)
    check('少一条 worktree 有差异',
          bool(registry_diff(same, (('a',), ('x', 'y')))), True)
    check('.git/worktrees 条目变化有差异',
          bool(registry_diff(same, (('a', 'b'), ('x', 'y', 'z')))), True)


def case_reclaim_report(scene):
    """17. `--reclaim` 只报告：四判据必须咬住，且**一条都不删**（#378 回收设计第一档）。"""
    print('\n[17] --reclaim 只报告')
    root = scene / 'reclaim-root'
    root.mkdir()
    good = root / 'idlejot-parity-good'
    good.mkdir()
    orphan = root / 'idlejot-parity-orphan'
    (orphan / 'pristine').mkdir(parents=True)
    live = root / 'idlejot-parity-live'
    (live / 'pristine' / 'scripts').mkdir(parents=True)
    (live / 'pristine' / 'scripts' / 'gates.json').write_text('{}', encoding='utf-8')
    busy = root / 'idlejot-parity-busy'
    busy.mkdir()
    foreign = root / 'tulpa-foreign'
    foreign.mkdir()
    script = root / 'parity-348-foreign'
    script.mkdir()
    registered = {str(orphan / 'pristine'), str(live / 'pristine')}
    usable = lambda path: (Path(path) / 'scripts' / 'gates.json').is_file()  # noqa: E731
    busy_names = {'idlejot-parity-busy'}

    candidates, problems = parity.reclaim_candidates(
        root, registered=registered, usable=usable, busy=busy_names)
    try:
        names = [candidate.scene.name for candidate in candidates]
        check('符合四条判据的假现场进候选', names,
              ['idlejot-parity-good', 'idlejot-parity-orphan'])
        check('活树不进候选', 'idlejot-parity-live' in names, False)
        check('有进程引用不进候选', 'idlejot-parity-busy' in names, False)
        check('tulpa- 前缀不进候选', 'tulpa-foreign' in names, False)
        check('parity-348- 前缀不进候选', 'parity-348-foreign' in names, False)
        check('扫描没有问题', problems, [])
        states = {candidate.scene.name: candidate.registration for candidate in candidates}
        check('孤儿登记标为 prunable', states.get('idlejot-parity-orphan'), 'prunable')
        check('无登记标为 none', states.get('idlejot-parity-good'), 'none')
        # 反向对照：把前缀放宽到 tulpa-* 之后，该目录必须进候选——证明①不是空转。
        widened, _ = parity.reclaim_candidates(
            root, prefixes=('tulpa-',), registered=registered, usable=usable, busy=busy_names)
        check('反向对照：放宽前缀后 tulpa- 目录必进候选',
              [candidate.scene.name for candidate in widened], ['tulpa-foreign'])
        check('无登记只给目录删除命令', parity.reclaim_commands(good),
              [f'Remove-Item -LiteralPath "{good}" -Recurse -Force'])
        orphan_commands = parity.reclaim_commands(orphan, orphan / 'pristine')
        check('孤儿登记先摘登记', 'git worktree remove --force' in orphan_commands[0], True)
        out = StringIO()
        with contextlib.redirect_stdout(out):
            code = parity.reclaim_report(root, registered=registered, usable=usable,
                                         busy=busy_names)
        text = out.getvalue()
        check('报告退出码 0', code, 0)
        check('报告写明未删除任何东西', '未删除任何东西' in text, True)
        check('报告打印目录删除命令', 'Remove-Item -LiteralPath' in text, True)
        check('报告提示 --keep 已知限制', '已知限制' in text, True)
        check('报告后假现场目录仍在（只读）', good.is_dir(), True)
        empty = root / 'empty-root'
        empty.mkdir()
        empty_out = StringIO()
        with contextlib.redirect_stdout(empty_out):
            empty_code = parity.reclaim_report(empty, registered=set(), usable=usable, busy=set())
        check('空根 0 候选也退 0', empty_code, 0)
        check('空根打印 0 个候选', '候选 0 个' in empty_out.getvalue(), True)
    except Exception as error:  # noqa: BLE001 — 用例自身崩了也要报出来，别让 guarded() 独自背
        FAILS.append(f'[17] --reclaim 用例中断 — {type(error).__name__}: {error}')


def guarded(name, case, scene):
    """一组用例崩了也继续跑下一组：崩在哪一组、崩成什么，都要看得见（基线对照要的是全貌）。"""
    try:
        case(scene)
    except Exception as error:  # noqa: BLE001 — 门禁要报出「这一组没跑完」，不是把栈吞掉
        FAILS.append(f'{name}：用例中断 — {type(error).__name__}: {error}')
        print(f'FAIL {name}：用例中断 — {type(error).__name__}: {error}')


def main():
    if os.name != 'nt':
        print('SKIP: parity 是 Windows-only（msvcrt）')
        return 0
    scene = Path(tempfile.mkdtemp(prefix='parity-348-'))
    before = registry_state()
    try:
        prunable_report('跑前')
        for name, case in (('跨进程互斥', case_lock_is_cross_process),
                           ('同进程重入互斥', case_same_process_two_handles),
                           ('等锁串行化', case_lock_serializes_threads),
                           ('只认自己那份', case_registration),
                           ('不连坐', case_prune_spares_others),
                           ('不静默', case_cleanup_not_silent),
                           ('孤儿登记不复用', case_orphan_not_reused),
                           ('main() 持锁接线', case_main_holds_lock),
                           ('收尾不误伤别人的登记', case_unregister_spares_others),
                           ('收尾单步抛错仍继续', case_cleanup_step_oserror),
                           ('建树中途失败不留现场', case_build_failure_no_scene),
                           ('fail 分支不留孤儿登记', case_fail_branch_no_orphan),
                           ('--keep 打印清理命令', case_keep_prints_commands),
                           ('清理只认前缀', case_cleanup_scope),
                           ('孤儿登记逐条自摘', case_forget_registration),
                           ('登记表快照对比', case_registry_snapshot_diff),
                           ('--reclaim 只报告', case_reclaim_report)):
            guarded(name, case, scene)
    finally:
        shutil.rmtree(scene, ignore_errors=True)
        # 回归断言（issue #378 卫生）：跑完的登记表必须与跑前**逐条一致**。
        # 先拍快照再清理：夹具漏了登记时这条要能翻红，而不是被收尾顺手抹平。
        left = scene_paths(str(scene))
        if left:
            FAILS.append(f'夹具留下登记：{[scene_rel(scene, p) for p, _ in left]}')
        for problem in registry_diff(before, registry_state()):
            FAILS.append(problem)
        # 判红已记下，清理只为不把本机越跑越脏；摘不掉就打印可复制命令（不许静默）。
        for path, _ in left:
            forget_or_report(path)
        still = scene_paths(str(scene))
        if still:
            FAILS.append(f'夹具残留摘不掉：{[scene_rel(scene, p) for p, _ in still]}')
            for path, _ in still:
                print(f'FAIL 手工清理命令 — git worktree remove --force "{path}"')
        # WARN 级盘点：本机陈年 prunable 是环境前提，只观测、不判红（#378 C1）。
        prunable_report('跑后')
    if FAILS:
        for line in FAILS:
            print(f'FAIL {line}')
        return 1
    print('PASS parity 现场隔离十七项')
    return 0


if __name__ == '__main__':
    sys.exit(main())
