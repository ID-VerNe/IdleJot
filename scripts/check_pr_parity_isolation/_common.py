"""`check_pr_parity_isolation` 包的共享基建（issue #378 删除档拆包，#283/#285 先例）。

从旧 `scripts/check_pr_parity_isolation.py` 整体搬运：断言器、夹具环境、登记表快照与
自摘工具都在这里，`cases_scene.py` / `cases_reclaim.py` 只装用例、`__init__.py` 只装
main()。判据与正文逐字保持（`forget_registration()` 除外：判据收进生产侧
`parity.remove_registration()` 后这里改为委派——门禁的每一次夹具收尾同时也是生产路径
的活测）。
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

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import check_pr_parity as parity  # noqa: E402
import check_pr_parity_lock as lock  # noqa: E402

FAILS = []
LOCK_SCRIPT = SCRIPTS / 'check_pr_parity_lock.py'
PARITY_SCRIPT = SCRIPTS / 'check_pr_parity.py'


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

    判据收进生产侧后这里委派（`parity.remove_registration()`，issue #378 删除档）：
    判断与执行只有一份，门禁的每一次夹具收尾同时也是生产路径的活测。返回 `None` 表示
    摘掉或本来就没有；否则回传原因，由调用方打印可复制命令——删不净不许静默。
    """
    return parity.remove_registration(path)


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


def guarded(name, case, scene):
    """一组用例崩了也继续跑下一组：崩在哪一组、崩成什么，都要看得见（基线对照要的是全貌）。"""
    try:
        case(scene)
    except Exception as error:  # noqa: BLE001 — 门禁要报出「这一组没跑完」，不是把栈吞掉
        FAILS.append(f'{name}：用例中断 — {type(error).__name__}: {error}')
        print(f'FAIL {name}：用例中断 — {type(error).__name__}: {error}')
