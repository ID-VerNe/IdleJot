"""issue #61 gate: 「本地绿」必须在 CI 等价条件下复现。

背景：PR #53 正文写着本地同时通过 `check_now_clock`，CI 却连红三次在同一条断言上。
根因是本地 worktree 继承了开发机状态——`~/.water-group` 私有库、`WATER_GROUP_HOME` /
`IDLEJOT_WG_*` 环境变量、被 ignore 的 `water-group/config.json` 与 `tools/`——而 CI 是
干净 checkout。本脚本把推送前的本地复跑挪到等价条件：

1. `git worktree add --detach <临时目录> <ref>`：只有该 ref 的已跟踪文件；
2. 子进程环境剥掉全部 `IDLEJOT_WG_*` / `TULPA_WG_*` 与 `WATER_GROUP_HOME` / `PYTHONPATH`
   （前缀匹配，以后新增的开关自动排除），`WATER_GROUP_HOME` 指到临时空目录，
   `PYTHONUTF8=1` / `GRADIO_ANALYTICS_ENABLED=False` 与 CI 一致；
3. 用同一份清单执行器（`scripts/run_gates.py`，issue #59/#60）在 pristine 树里跑：
   默认全部 ci 门禁，`--shard` 只跑一片；
4. 退出码：0 全绿；1 有门禁红；2 环境问题（worktree 建不起来 / 未知分片 / 清单缺失）。

    uv run python scripts/check_pr_parity.py                 # 当前 HEAD 全量
    uv run python scripts/check_pr_parity.py --shard static  # 只跑一片
    uv run python scripts/check_pr_parity.py --ref <sha> --keep

`pre-push` 钩子对每个被推送的本地 sha 跑一次；`SKIP_PARITY=1` 显式跳过（钩子里留痕）。
CI 本身不跑本脚本：CI 已经是那个干净环境，且它需要完整 git 历史。

现场隔离（issue #348）：同机两处 parity 同时活着时，其中一边的门禁会整片报
`can't open file '.../pristine/scripts/<gate>.py'`——树在跑的过程中被另一边抽走了。
现在「建树 → 跑门禁 → 收树」整段持 `scripts/check_pr_parity_lock.py` 的跨进程锁，
收尾只 `remove` 自己登记过的那条路径（不再全局 `prune`），收不掉时说进 stderr。
取消全局 `prune` 之后，复用前必须**确认目录还在**：孤儿登记（目录已删、登记还在）按
失败处理并提示 `git worktree prune`，不会把一棵不存在的树当成可复用的现场。

issue #378（非破坏性部分）：`taskkill /F` 之类硬杀绕过 `finally`，一次收不掉的现场会永久
留在 `%TEMP%`，八天累积 92 套。本次只加固「已经走到自己收尾」这条路径：每个收尾步骤独立
兜底、建树三步进 `try`、`fail` 分支先摘登记再删目录、`--keep` 与收尾失败都打印可复制的手工
清理命令。**不做启动期自动扫描/回收**——自动回收仍缺两样：`check_pr_parity_lock.py` 的
`_pid_alive()` 只校验 holder 里记录的 pid，没有「这棵树有没有进程在用」的判据（硬杀后
锁会释放，但门禁子进程可能还活着，#366/#370 家族）；`--keep` 现场与硬杀残留没有标记可
区分，直接回收会误删有意保留的排查现场。这两样补齐之前，扫描回收不得上线。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import check_pr_parity_lock as lock

ROOT = Path(__file__).resolve().parents[1]
ISOLATED_NAMES = ('WATER_GROUP_HOME', 'PYTHONPATH')
ISOLATED_PREFIXES = ('IDLEJOT_WG_', 'TULPA_WG_')
LOG_ENCODING = 'utf-8'
ADD_OK = 'ok'
# 本工具自己建的现场目录前缀（`mkdtemp(prefix=...)`）。任何清理/回收只认这个前缀，
# 绝不碰 `.tmp/wt-*`、`tulpa-*` 或别的目录（issue #378 安全约束）。
SCRATCH_PREFIX = 'idlejot-parity-'

# 本脚本常由 `.githooks/pre-push` 拉起，而钩子所在的父进程未必有控制台（`pythonw`、
# 编辑器内的任务、CI 的作业包装），它拉起的 `git` 与门禁执行器都是控制台程序——裸调会给
# 每条新开一扇黑框（issue #353，承接 #39 / #319）。抑制只在一处拼写，所以用运行时树的
# helper 而不是本地再抄一份标志。
#
# 但**不能硬依赖**它：本文件也被用来在裁剪安装里跑，那里没有运行时树。抑制是加固，
# 拿不到就照常跑，而不是拒绝启动（同 `run_gates.py` 的取舍）。
sys.path.insert(0, str(ROOT / 'water-group' / 'daemon'))
try:
    import winconsole  # noqa: E402  -- 位置依赖上面这行 sys.path
except ImportError:  # 裁剪安装里没有运行时树：退回「直接跑」，不假装加固
    winconsole = None


def run(command, cwd, env=None):
    """跑一条**要读输出**的短命令（`git rev-parse` / `worktree add|remove`）。

    走 `run_hidden` 而不是裸 `subprocess.run`：`git` 是控制台程序，且这里本来就要
    `capture_output`，不存在 issue #91 那个「输出直通丢失」的前提。
    """
    if winconsole is None:
        return subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                              text=True, encoding=LOG_ENCODING)
    return winconsole.run_hidden(command, cwd=cwd, env=env, capture_output=True,
                                 text=True, encoding=LOG_ENCODING)


def output_of(result):
    return (result.stderr or result.stdout or '').strip()


def make_scratch():
    """建本轮的现场目录：名字带固定前缀，任何清理只认这个前缀（issue #378）。"""
    return Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))


def is_parity_scene(path):
    """现场目录是不是本工具自己建的——只认 `idlejot-parity-` 前缀。

    自动回收（另立 issue）只能对这里返回 True 的目录动手：`.tmp/wt-*` 是人工工作树，
    `tulpa-*` 与其他目录也不是本工具的现场，误删等于抹掉别人未提交的工作。
    """
    return Path(path).name.startswith(SCRATCH_PREFIX)


def cleanup_commands(scene):
    """给本现场生成可复制的手工清理命令；非本工具的目录一概拒绝。

    只生成命令、不执行：`--keep` 与收尾失败时给人一条能直接粘贴的退路。拒绝非前缀
    目录是 issue #378 的安全约束——不能因为调用方传错一个路径就把人工工作树删了。
    """
    scene = Path(scene)
    if not is_parity_scene(scene):
        raise ValueError(f'拒绝为非 parity 现场生成清理命令：{scene}')
    pristine = scene / 'pristine'
    return [
        f'git worktree remove --force "{pristine}"',
        f'Remove-Item -LiteralPath "{scene}" -Recurse -Force',
    ]


def resolve_ref(ref):
    """把 ref 解析成 commit sha；解析不了返回 None。"""
    result = run(['git', 'rev-parse', '--verify', f'{ref}^{{commit}}'], ROOT)
    return result.stdout.strip() if result.returncode == 0 else None


def registered_worktrees():
    """本仓在 git 登记表里的 worktree 路径集合（保留 git 的写法）。"""
    result = run(['git', 'worktree', 'list', '--porcelain'], ROOT)
    if result.returncode != 0:
        return set()
    return {line[len('worktree '):] for line in result.stdout.splitlines()
            if line.startswith('worktree ')}


def _same_path(left, right):
    """两段路径是否指同一个位置：`normcase` 抹平大小写与斜杠，`normpath` 抹平 `..` 与尾分隔符。"""
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def is_registered(path):
    """`path` 是否还挂在 git 的 worktree 登记表上（按规范化路径比，容忍斜杠差异）。"""
    return any(_same_path(path, entry) for entry in registered_worktrees())


def _usable(entry):
    """登记表里这条路径此刻真的还是一棵 checkout（目录在、`scripts/` 在）。

    只看登记不看目录，正是 issue #348 复核的第 ② 条：本 PR 取消了全局 `prune`，于是
    上一轮硬杀留下的孤儿登记（目录已被删、登记还在）会被 `add_worktree` 当成「可以复用」，
    随后 `shard_problem` 报 `该 ref 缺少 scripts/gates.json` 并退出 2——一个由修复本身
    引入的新失败。复用前先摸一下盘。
    """
    base = Path(entry)
    return base.is_dir() and (base / 'scripts' / 'gates.json').is_file()


def add_worktree(sha, scratch):
    """在 `scratch/pristine` 建 detached worktree。

    返回 `(状态, 路径, 原因)`：`ADD_OK` 新建成；`'reuse'` 该路径已在登记表里**且目录还在**
    （上一轮硬杀留下的活树），直接复用而不重复 `add`；`'fail'` 建不起来或登记是孤儿
    （原因在第三项）。

    返回值里的路径是**登记表里的写法**：`git` 会把 `C:\\a\\b` 之类的多余分隔符规范化，
    拿请求时的字符串去比对登记表会假阴性，后续收尾必须用这里的路径（issue #348）。
    """
    path = scratch / 'pristine'
    for entry in registered_worktrees():
        if not _same_path(path, entry):
            continue
        if _usable(entry):
            return 'reuse', Path(entry), None
        # 登记在、目录不在：不能复用（否则等于被指向一棵不存在的树），也不能悄悄
        # 就地覆盖（那条登记要么是别人的，要么得先 prune 掉）。说清让谁来收拾。
        return 'fail', None, (f'{entry} 登记在 git 的 worktree 表里，但目录已不在（孤儿登记）；'
                              '先 `git worktree prune` 再重跑')
    result = run(['git', 'worktree', 'add', '--detach', str(path), sha], ROOT)
    if result.returncode != 0:
        return 'fail', None, output_of(result)
    for entry in registered_worktrees():
        if _same_path(path, entry):
            return ADD_OK, Path(entry), None
    # add 成功却查不到登记：不猜，按失败处理（收尾会再去核一次）。
    return 'fail', None, f'git worktree add 成功但登记表里没有 {path}'


def _exists(path):
    """目录在不在；摸盘失败按「还在」记，宁可多说一句也不静默成功。"""
    try:
        return Path(path).exists()
    except OSError:
        return True


def _remove_tree(report, target, label):
    """删一个目录并记账；`rmtree(ignore_errors=True)` 删不掉时不许静默成功。"""
    try:
        if not _exists(target):
            return
        shutil.rmtree(target, ignore_errors=True)
        if _exists(target):
            report.problems.append(f'{label}删不掉：{target}')
    except OSError as error:
        report.problems.append(f'{label}删除抛错：{error}')


def cleanup(path):
    """收掉**自己的**那棵树：先核对登记，再 `remove --force`，最后删临时目录。

    只认 `path` 这一条（issue #348 的现场就是一边收尾顺手动了另一边）。刻意**不调**
    `git worktree prune`：那是全仓范围的写操作，会把别处「目录已不在」的登记一并清掉，
    正是「抽别人的树」最容易扩散的那一步；孤儿登记留给 `git worktree list` 自己报，
    或由人工 `git worktree prune` 处理。

    每个步骤独立 `try/except`（issue #378 缺口②）：`run()` 抛 `OSError` 时后面的目录
    删除仍会执行，失败逐条写进 `Report.problems`，不静默跳过。调用方（`_finish_scene()`）
    负责把原因与可复制的手工清理命令打印出来。
    注：调用方传进来的必须是 `add_worktree` 返回的那条路径——它是登记表里的写法。
    """
    report = Report(path)
    path = Path(path)
    scratch = path.parent
    try:
        registered = is_registered(path)
    except OSError as error:
        registered = False
        report.problems.append(f'读不到 worktree 登记表：{error}')
    if registered:
        try:
            result = run(['git', 'worktree', 'remove', '--force', str(path)], ROOT)
        except OSError as error:
            report.problems.append(f'git worktree remove 抛错：{error}')
        else:
            report.removed = result.returncode == 0
            if not report.removed:
                report.problems.append(f'git worktree remove 失败：{output_of(result) or "（无输出）"}')
    elif _exists(path):
        # 登记已经没了，这棵树不再是「git 的 worktree」。不能静默：正常路径下它此刻
        # 一定还在登记里，不在了就说明有人动了手脚（上一轮硬杀、或别处 prune 过）。
        report.problems.append('登记表里没有这棵树，按目录清理')
    _remove_tree(report, path, '目录')
    _remove_tree(report, scratch, '临时目录')
    report.ok = not report.problems and not _exists(path) and not _exists(scratch)
    return report


class Report:
    """收尾结果：`ok` 与逐条问题分开，调用方不必猜。"""

    def __init__(self, path):
        self.path = Path(path)
        self.removed = False
        self.problems = []
        self.ok = False


def clean_env(home):
    """剥掉私有库与环境开关，只留 CI 同款变量（前缀匹配，新开关自动生效）。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ISOLATED_NAMES and not key.startswith(ISOLATED_PREFIXES)
    }
    env['WATER_GROUP_HOME'] = str(home)
    env['PYTHONUTF8'] = '1'
    env['GRADIO_ANALYTICS_ENABLED'] = 'False'
    return env


def shard_problem(pristine, shard):
    """校验分片名用的是**被跑 ref 自己那份**清单；合法返回 None。"""
    manifest = pristine / 'scripts' / 'gates.json'
    executor = pristine / 'scripts' / 'run_gates.py'
    if not manifest.is_file() or not executor.is_file():
        return f'{shard or "所有分片"}：该 ref 缺少 scripts/gates.json 或 scripts/run_gates.py'
    if shard is None:
        return None
    names = [row.get('name') for row in json.loads(manifest.read_text(encoding=LOG_ENCODING)).get('shards') or []]
    if shard not in names:
        return f'未知分片 {shard!r}；该 ref 的合法值：' + '、'.join(names)
    return None


def parity_command(pristine, shard):
    command = [sys.executable, str(pristine / 'scripts' / 'run_gates.py'), 'ci']
    return command + (['--shard', shard] if shard else [])


def run_gates(pristine, shard, env):
    """跑门禁执行器，返回退出码。

    这一条和 `run` 的区别是**输出必须直通**：94 条门禁的逐条结果就是给人看的，钩子里
    也只隔着这一层。所以不能走 `run_hidden`——`CREATE_NO_WINDOW` 会把子进程交给自己那扇
    新控制台，父进程没有控制台时「继承我的 stdout」就指错了地方，门禁全部变成哑的
    （issue #91 的形状）。`run_passthrough` 正是为此存在：两路合流回管道、UTF-8 解码、
    整块写回本进程 stdout，窗口仍然不开。

    拿不到 helper 时退回裸调用：那种情形没有运行时树，也就没有窗口可抑制。
    """
    command = parity_command(pristine, shard)
    if winconsole is None:
        return subprocess.run(command, cwd=pristine, env=env).returncode
    return winconsole.run_passthrough(command, cwd=pristine, env=env)


def main():
    parser = argparse.ArgumentParser(description='CI 等价条件复跑（issue #61）')
    parser.add_argument('--ref', default='HEAD', help='要复跑的 ref（默认 HEAD）')
    parser.add_argument('--shard', help='只跑某个分片（名字取自该 ref 的 scripts/gates.json）')
    parser.add_argument('--keep', action='store_true', help='保留临时 worktree 与空数据根，打印路径供排查')
    args = parser.parse_args()

    sha = resolve_ref(args.ref)
    if sha is None:
        print(f'FAIL: 解析不了 ref {args.ref!r}（先 fetch 或用完整 sha）')
        return 2

    # 建树 → 跑门禁 → 收树 整段持锁：同一台机器上任何时刻只该有一份 parity 在跑
    # （issue #348：并发的两边会互相 `remove` + `prune` 掉对方正在跑的树）。
    path = lock.lock_path()
    fd = lock.acquire(path, blocking=True,
                      on_wait=lambda holder: print(lock.describe_wait(holder), file=sys.stderr))
    if fd is None:
        # 等满没拿到：宁可报失败也不无锁开跑——无锁跑正是 #348 要消灭的东西。
        print('FAIL: 拿不到 parity 锁（等满仍未空出）；若持有者已被硬杀，'
              f'清掉 {path.with_name(lock.HOLDER_NAME)} 里的死记录后重试')
        return 2
    try:
        lock.write_holder(args.ref if args.ref != 'HEAD' else sha)
        return _parity(args, sha)
    finally:
        lock.release(fd, path)


def _finish_scene(args, scratch, pristine):
    """本轮现场的唯一收尾出口：保留现场或收掉它，失败必须说出来（issue #378）。"""
    if scratch is None:
        return
    target = pristine if pristine is not None else scratch / 'pristine'
    home = scratch / 'home'
    if args.keep:
        print(f'保留现场：worktree={target} home={home}')
        for command in cleanup_commands(scratch):
            print(f'parity: 手工清理命令 — {command}')
        return
    report = cleanup(target)
    if report.ok:
        return
    # 收尾失败不改跑门禁的结论，但必须说出来：静默残留正是 #348 的现场。
    for problem in report.problems:
        print(f'parity: 收尾未完成 — {problem}', file=sys.stderr)
    print(f'parity: 残留现场 {scratch}（请手工清理）', file=sys.stderr)
    for command in cleanup_commands(scratch):
        print(f'parity: 手工清理命令 — {command}', file=sys.stderr)


def _parity(args, sha):
    scratch = None
    pristine = None
    try:
        # 建树三步进 try（issue #378 缺口③）：中途抛错也要走到 finally 的收尾，
        # 否则 `mkdtemp` 已经落地的半截现场没人管。
        scratch = make_scratch()
        home = scratch / 'home'
        home.mkdir()
        status, pristine, reason = add_worktree(sha, scratch)
        if status == 'fail':
            # 不在这里直接 rmtree：交给 finally 的 cleanup() 先摘登记再删目录，
            # 直接抹目录会造出「登记在、目录不在」的孤儿登记（issue #378 缺口④）。
            print(f'FAIL: 建不起 pristine worktree（{reason}）')
            return 2
        if status == 'reuse':
            print(f'parity: 复用了已登记的 worktree（{pristine}）')

        problem = shard_problem(pristine, args.shard)
        if problem:
            print(f'FAIL: {problem}')
            return 2
        print(f'parity: ref {sha} → {pristine}')
        print(f'parity: WATER_GROUP_HOME={home}（空数据根，无 config.json / 私有库）')
        code = run_gates(pristine, args.shard, clean_env(home))
        if code == 0:
            label = f'分片 {args.shard}' if args.shard else '全部分片'
            print(f'PASS: parity 通过（{label}，干净 checkout + 空数据根）')
            return 0
        print(f'FAIL: parity 未通过（{Path(str(pristine / "scripts" / "run_gates.py")).name} 退出 {code}）')
        return 1 if code == 1 else 2
    except OSError as error:
        print(f'parity: 建树/跑门禁抛错 — {error}', file=sys.stderr)
        return 2
    finally:
        _finish_scene(args, scratch, pristine)


if __name__ == '__main__':
    sys.exit(main())
