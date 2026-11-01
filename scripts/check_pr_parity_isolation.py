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
6. **收尾不误伤别人的登记**：本门禁收尾时若表里还有**现场之外**的 `idlejot-parity-*` 登记，
   `unregister_scene()` 必须拒绝全仓 prune、只把本现场的残留说出来。

本门禁**自己不跑全仓 prune 来造现场**：`cleanup()` 明文不调 `git worktree prune`，因为那是
全仓写操作。用例要的「真树」换一个 scratch 直接建就行；只有收尾时为了摘掉**本门禁自己**
造出来的孤儿登记才可能 prune，且先断言那一刻表里的 `idlejot-parity-*` 条目全在本轮现场里
（`unregister_scene()`），断言不过就不 prune、只把残留说出来。

跑法：`python scripts/check_pr_parity_isolation.py`（本机手跑；它建真 worktree，量级与
`check_pr_parity.py` 同档，所以不在 CI 分片里跑）。
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

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
    """登记表里路径落在 `scene`（`pattern` 通配）内的那些条目——`git worktree list --porcelain` 的逐行产物。

    匹配走**规范化后的精确前缀**，不走 `fnmatch`：`git worktree list` 打的是正斜杠路径
    （`/` 分隔），`Path` 拼出来的是反斜杠，两边都得先 `normcase(normpath())`；
    而 `fnmatch` 在 Windows 上不把分隔符当成可匹配字符：`<根>\a\b` 匹配不上 `<根>\a\*`。
    """
    out = git('worktree', 'list', '--porcelain').stdout.splitlines()
    want_all = pattern == '*'
    wanted = None if want_all else os.path.normcase(os.path.normpath(pattern)) + os.sep
    hits = []
    prefix = 'worktree '
    for index, line in enumerate(out):
        if not line.startswith(prefix):
            continue
        path = line[len(prefix):]
        if not want_all and not os.path.normcase(os.path.normpath(path)).startswith(wanted):
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
    登记。这里 prune 的正当性只来自一个事实：那一刻表里残留的 `idlejot-parity-*` 条目
    全部落在本轮 `mkdtemp(prefix='parity-348-')` 的现场里，而现场目录已经在上一行删掉了。
    所以先断言「现场条目之外没有别的 idlejot-parity-* 登记」，断言不过就不 prune，
    只把残留说出来——别人的现场一格都不碰。
    """
    return _unregister(scene)[0]


def _unregister(scene):
    """`unregister_scene()` 的本体，多回一个 `pruned` 供对照用例断言「这次到底动没动全仓」。"""
    own = scene_paths(str(scene))
    losers = [(path, fields) for path, fields in own if 'prunable' in fields]
    if not losers:
        return [], False
    mine = {os.path.normcase(os.path.normpath(p)) for p, _ in own}
    live = [path for path, _ in scene_paths('*') if 'idlejot-parity-' in path
            and os.path.normcase(os.path.normpath(path)) not in mine]
    if live:
        print(f'WARN 现场之外还有 {len(live)} 条 idlejot-parity-* 登记，为免误伤不 prune；'
              f'本门禁留下 {len(losers)} 条孤儿登记：{[p for p, _ in losers]}')
        return losers, False
    git('worktree', 'prune')
    if scene_paths(str(scene)):
        print(f'WARN 本门禁的孤儿登记 prune 之后仍在：{[p for p, _ in scene_paths(str(scene))]}')
    return [], True


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
    check('建树状态', status, parity.ADD_OK)
    check('登记表认得返回的路径', parity.is_registered(path), True)
    check('返回的就是 pristine 目录', Path(path).name, 'pristine')
    again, same, _ = parity.add_worktree(sha, scratch)
    check('重复调用复用同一棵（不重复 add）', (again, same), ('reuse', Path(path)))
    report = parity.cleanup(path)
    check('收尾 ok', report.ok, True)
    check('收尾后登记已清', parity.is_registered(path), False)
    check('收尾后目录已清', Path(path).exists(), False)


def case_prune_spares_others(scene):
    """5. 收尾不许连坐别人正在用的树（#348 的原现场）。"""
    print('\n[5] 不连坐')
    sha = head_sha()
    status, mine, _ = parity.add_worktree(sha, scene / 'mine')
    check('自己的树建好', status, parity.ADD_OK)
    theirs = scene / 'theirs' / 'pristine'
    added = git('worktree', 'add', '--detach', str(theirs), sha)
    check('别人的树建好', added.returncode, 0)
    try:
        parity.cleanup(mine)
        check('别人的树还在盘上', (theirs / 'scripts' / 'gates.json').is_file(), True)
        check('别人的树还在登记里', parity.is_registered(theirs), True)
    finally:
        git('worktree', 'remove', '--force', str(theirs))


def case_cleanup_not_silent(scene):
    """6. 收得掉就不许报问题；登记被抹掉时必须说出来（不静默）。"""
    print('\n[6] 不静默')
    sha = head_sha()
    status, path, _ = parity.add_worktree(sha, scene / 'gone')
    check('树建好', status, parity.ADD_OK)
    clean = parity.cleanup(path)
    check('正常收尾 ok', clean.ok, True)
    check('正常收尾不报问题（对照）', clean.problems, [])
    check('正常收尾确实 remove 过', clean.removed, True)

    status, path, _ = parity.add_worktree(sha, scene / 'orphan')
    check('第二棵树建好', status, parity.ADD_OK)
    # 把登记抹掉（模拟上一轮硬杀留下的孤儿目录，或别处 prune 过）：此时 remove 走不下去，
    # 收尾必须把「登记表里没有这棵树」记进 problems，而不是静默 ok。
    git('worktree', 'remove', '--force', str(path))
    Path(path).mkdir(parents=True, exist_ok=True)
    (path / 'leftover.txt').write_text('x', encoding='utf-8')
    report = parity.cleanup(path)
    check('孤儿目录如实记账', bool(report.problems), True)
    check('孤儿目录没走 remove 路径', report.removed, False)
    check('孤儿目录仍被删掉', Path(path).exists(), False)


def case_orphan_not_reused(scene):
    """7. 登记在、目录不在的孤儿不许被当成可复用现场（砍掉全局 prune 之后的新坑）。"""
    print('\n[7] 孤儿登记不复用')
    sha = head_sha()
    scratch = scene / 'orphan-reuse'
    status, path, _ = parity.add_worktree(sha, scratch)
    check('先建一棵真树', status, parity.ADD_OK)
    # 只拆目录、留登记：正是上一轮被硬杀（或别处 prune 掉目录）之后的现场。
    shutil.rmtree(path)
    check('目录已拆掉但登记还在', parity.is_registered(path), True)

    again, reused, reason = parity.add_worktree(sha, scratch)
    check('孤儿登记不许 reuse', again, 'fail')
    check('孤儿不给路径', reused, None)
    check('孤儿原因点名 prune', 'prune' in (reason or ''), True)
    # 对照：登记与目录都在时才必须 reuse（否则上面那条 fail 什么都不证明）。
    # **不跑 `git worktree prune` 来造这棵对照树**——那是全仓写操作，正是 `cleanup()`
    # 明文不做的事（`check_pr_parity.py:cleanup` docstring），在门禁里跑一遍等于把
    # 别人的孤儿登记顺手清掉。换一个 scratch 直接建第二棵真树即可。
    other = scene / 'orphan-reuse-control'
    status, path, _ = parity.add_worktree(sha, other)
    check('对照树建成', status, parity.ADD_OK)
    third, same, _ = parity.add_worktree(sha, other)
    check('真树仍在时才 reuse（对照）', (third, same), ('reuse', Path(path)))
    parity.cleanup(path)


def case_unregister_spares_others(scene):
    """9. 收尾的孤儿清理只动本现场：现场之外还有别的 `idlejot-parity-*` 登记时必须不 prune。"""
    print('\n[9] 收尾不误伤别人的登记')
    sha = head_sha()
    # 本现场的孤儿：建一棵再只拆目录（登记留在表里）。
    status, own_path, _ = parity.add_worktree(sha, scene / 'own-orphan')
    check('本现场的树建好', status, parity.ADD_OK)
    shutil.rmtree(own_path)
    check('本现场的孤儿登记还在', parity.is_registered(own_path), True)
    # 现场之外的「别人」：换一个不在 `scene` 下的前缀，假装是另一处 parity 留下的登记。
    stranger_root = Path(tempfile.mkdtemp(prefix='idlejot-parity-stranger-'))
    stranger = stranger_root / 'pristine'
    try:
        added = git('worktree', 'add', '--detach', str(stranger), sha)
        check('别人的树建好', added.returncode, 0)
        shutil.rmtree(stranger)  # 只拆目录，让它的登记也变成 prunable
        check('别人的孤儿登记还在', parity.is_registered(stranger), True)

        losers, pruned = _unregister(scene)
        check('现场之外有人时不动全仓 prune', pruned, False)
        check('如实报出本现场的孤儿',
              sorted(Path(p).parent.name for p, _ in losers), ['orphan-reuse', 'own-orphan'])
        check('别人的孤儿登记原样留着', parity.is_registered(stranger), True)

        # 摘掉「别人」之后，同一份现场就该能收干净——否则上面那条 WARN 只是「永远不收」。
        # 摘的时候只删那条登记文件、不跑 `git worktree prune`：prune 是这次对照的**被测对象**，
        # 在测试自己手里跑一遍，第 336 行就再也证明不了什么（它会被这一步提前满足）。
        gitdir = Path(git('rev-parse', '--git-common-dir').stdout.strip()) / 'worktrees'
        for entry in gitdir.iterdir():
            if entry.is_dir() and (entry / 'gitdir').is_file():
                target = (entry / 'gitdir').read_text(encoding='utf-8').strip()
                if os.path.normcase(os.path.normpath(target)) == \
                        os.path.normcase(os.path.normpath(str(stranger / '.git'))):
                    shutil.rmtree(entry)
        check('别人的登记已摘', parity.is_registered(stranger), False)
        losers, pruned = _unregister(scene)
        check('没有旁人时才 prune（对照）', pruned, True)
        check('本现场的孤儿登记已清', scene_paths(str(scene)), [])
    finally:
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
    try:
        for name, case in (('跨进程互斥', case_lock_is_cross_process),
                           ('同进程重入互斥', case_same_process_two_handles),
                           ('等锁串行化', case_lock_serializes_threads),
                           ('只认自己那份', case_registration),
                           ('不连坐', case_prune_spares_others),
                           ('不静默', case_cleanup_not_silent),
                           ('孤儿登记不复用', case_orphan_not_reused),
                           ('main() 持锁接线', case_main_holds_lock),
                           ('收尾不误伤别人的登记', case_unregister_spares_others)):
            guarded(name, case, scene)
    finally:
        shutil.rmtree(scene, ignore_errors=True)
        unregister_scene(scene)
    if FAILS:
        for line in FAILS:
            print(f'FAIL {line}')
        return 1
    print('PASS parity 现场隔离九项')
    return 0


if __name__ == '__main__':
    sys.exit(main())
