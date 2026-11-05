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
    uv run python scripts/check_pr_parity.py --reclaim       # 只报告可回收现场，不删除
    uv run python scripts/check_pr_parity.py --reclaim --yes # 删除档：真删候选现场

`--reclaim`（issue #378 回收设计的第一档）**只读**：扫描 `%TEMP%` 下的
`idlejot-parity-*` 现场，打印候选与可直接粘贴的清理命令，退出码 0，
**一条都不会删**。`--yes`（第二档）才真删：逐条先摘登记再删目录、删不掉要说话并
给出手工命令、动手前对候选再核一次进程引用（扫不到进程表一条都不删）。
另有 parity 正在跑时立即以退出码 2 说明「稍后再试」（不排队），且**不改动**锁与
持有者记录文件（哪怕是死记录）。

`pre-push` 钩子对每个被推送的本地 sha 跑一次；`SKIP_PARITY=1` 显式跳过（钩子里留痕）。
CI 本身不跑本脚本：CI 已经是那个干净环境，且它需要完整 git 历史。

现场隔离（issue #348）：同机两处 parity 同时活着时，其中一边的门禁会整片报
`can't open file '.../pristine/scripts/<gate>.py'`——树在跑的过程中被另一边抽走了。
现在「建树 → 跑门禁 → 收树」整段持 `scripts/check_pr_parity_lock.py` 的跨进程锁，
收尾只 `remove` 自己登记过的那条路径（不再全局 `prune`），收不掉时说进 stderr。
取消全局 `prune` 之后，复用前必须**确认目录还在**：孤儿登记（目录已删、登记还在）按
失败处理并提示 `git worktree prune`，不会把一棵不存在的树当成可复用的现场。

issue #378（非破坏性部分，PR #383）：`taskkill /F` 之类硬杀绕过 `finally`，一次收不掉的现场
会永久留在 `%TEMP%`，八天累积 92 套。「已经走到自己收尾」的路径已加固：每个收尾步骤独立
兜底、建树三步进 `try`、`fail` 分支先摘登记再删目录、`--keep` 与收尾失败都打印可复制的手工
清理命令。`--keep` 现场从 `make_scratch()` 起就带 `.keep` 标记（回收第二档）：保留意图在
启动一刻声明，之后被硬杀（`finally` 不跑）标记也已在盘上，回收据此排除，不会误删排查现场。
删除档是 `--reclaim --yes`：候选判定（前缀 / 非活树 / 无进程引用 / 非 keep）与动手之间
再核一次进程引用，删不掉要说话。**启动期自动扫描/回收仍不做**——「这棵树有没有进程
在用」仍只有 `Win32_Process` CommandLine 启发式（`_pid_alive()` 只校验 holder 里的 pid，
硬杀后门禁子进程可能还活着，#366/#370 家族），自动入口等更强判据定论后另立单。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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


# `--keep` 现场的标记文件（issue #378 回收第二档）：回收靠它把「有意保留的排查现场」
# 与硬杀残留区分开。放在现场目录根上、随目录一起消失——目录已不在的现场本来就不会
# 被扫描到，标记只需要覆盖「目录还在」的那一半。
KEEP_NAME = '.keep'


def write_keep_marker(scene):
    """把现场标记为 `--keep` 保留；写不进去要说出来，不许静默失去回收保护。"""
    marker = Path(scene) / KEEP_NAME
    try:
        marker.write_text('', encoding=LOG_ENCODING)
    except OSError as error:
        print(f'parity: keep 标记写不进去 — {marker}（{error}）', file=sys.stderr)


def is_kept_scene(scene):
    """现场是不是 `--keep` 保留的。读盘失败按「在保留」处理：判不清的一律不删。"""
    try:
        return (Path(scene) / KEEP_NAME).exists()
    except OSError:
        return True


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


# `--reclaim` 只认本工具自己建的现场前缀（issue #378）：`.tmp/wt-*` 是人工工作树、
# `tulpa-*` 与别的目录不是本工具的产物，`parity-348-*` 是隔离门禁的 scratch（另有生命周期）。
RECLAIM_PREFIXES = (SCRATCH_PREFIX,)
RECLAIM_KEEP_NOTE = ('已知限制：带 `.keep` 标记的 `--keep` 现场已被判据④排除；本改动之前'
                     '保留的现场没有标记，仍会出现在候选里，删除前请自行确认（issue #378）')
# 进程表一次性拉全机、过滤在 Python 侧做：查询命令里**不含**特征串，所以 PowerShell
# 自身的 CommandLine 不会命中任何现场（否则每个现场都会被判成「被占用」）。
PROCESS_DUMP = ('[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; '
                '$OutputEncoding = [System.Text.Encoding]::UTF8; '
                '$rows = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | '
                'ForEach-Object { [pscustomobject]@{ pid = [int]$_.ProcessId; '
                'cmd = [string]$_.CommandLine } }); '
                '$rows | ConvertTo-Json -Compress -Depth 3')


def reclaim_commands(scene, registered_pristine=None):
    """候选现场的可粘贴清理命令；非本工具现场拒绝（同 `cleanup_commands()` 的前缀约束）。

    `registered_pristine` 非空时先摘那一条登记——孤儿登记不摘掉，目录删完就留下一条
    指向不存在目录的记录。**只生成命令、不执行**。
    """
    scene = Path(scene)
    if not is_parity_scene(scene):
        raise ValueError(f'拒绝为非 parity 现场生成清理命令：{scene}')
    commands = []
    if registered_pristine is not None:
        commands.append(f'git worktree remove --force "{registered_pristine}"')
    commands.append(f'Remove-Item -LiteralPath "{scene}" -Recurse -Force')
    return commands


def remove_registration(path):
    """把登记表里 `path` 这一条**只这一条**摘掉，绝不 prune 全仓（issue #378 删除档）。

    孤儿登记（目录连同父目录都不在）也适用：`git worktree remove --force` 会按登记表
    找回那条记录（隔离门禁实测过）。返回 `None` 表示已摘或本来就没有；否则回传原因，
    由调用方打印可复制命令——摘不掉不许静默。
    """
    if path is None:
        return None
    path = Path(path)
    if not is_registered(path):
        return None
    try:
        result = run(['git', 'worktree', 'remove', '--force', str(path)], ROOT)
    except OSError as error:
        return str(error)
    if result.returncode == 0 and not is_registered(path):
        return None
    return output_of(result) or f'rc={result.returncode}，登记仍在'


def process_command_lines():
    """一次拉全机进程的 `(pid, CommandLine)`；拿不到返回 `None`（调用方按「有占用」保守处理）。"""
    result = run(['powershell', '-NoProfile', '-NonInteractive', '-Command', PROCESS_DUMP],
                 ROOT, env=dict(os.environ, PYTHONUTF8='1'))
    if result.returncode != 0:
        return None
    text = (result.stdout or '').strip()
    if not text:
        return []
    try:
        rows = json.loads(text)
    except ValueError:
        return None
    if isinstance(rows, dict):
        rows = [rows]
    rows = [row for row in rows if isinstance(row, dict)]
    return [(int(row.get('pid') or 0), str(row.get('cmd') or '')) for row in rows]


def busy_scenes(names):
    """哪些现场正被进程引用：`(命中集合, 不可得原因)`。

    这是判据③「无进程占用证据」的全部实现——按路径特征串（现场目录名）扫 `Win32_Process`
    的 CommandLine；扫不到就**不列候选**（宁可少报，也不给人一条可能删掉在用现场的命令）。
    查询自身按 pid 排除；特征串不在查询命令里（见 `PROCESS_DUMP`），所以不会有自匹配。
    """
    rows = process_command_lines()
    if rows is None:
        return set(), '进程表拿不到（Win32_Process 查询失败），按「有占用」保守处理'
    mine = os.getpid()
    hits = set()
    for name in names:
        token = name.lower()
        for pid, command_line in rows:
            if pid == mine or not command_line:
                continue
            if token in command_line.lower():
                hits.add(name)
                break
    return hits, None


def reclaim_candidates(root=None, *, prefixes=RECLAIM_PREFIXES, registered=None,
                       usable=None, busy=None):
    """扫 `root` 下的候选现场，返回 `(候选, 问题, kept)`——**只读**，不删任何东西。

    候选四条（issue #378 回收设计）：① 前缀只认 `prefixes`（默认 `idlejot-parity-`）；
    ② `pristine` 不是活着的树（登记在且 `_usable()` 为真 → 排除；登记在但目录不在 → 可摘）；
    ③ 没有进程引用（`busy` 注入集合时按集合判定，否则扫 `Win32_Process`）；
    ④ `--keep` 现场：带 `.keep` 标记 → 排除（回传进 `kept`，由报告说出来，不只静默排除）。

    三个可注入参数（`registered` / `usable` / `busy`）让门禁用假现场覆盖判定逻辑，不必
    去真实登记表里造树。
    """
    root = Path(root if root is not None else tempfile.gettempdir())
    problems = []
    candidates = []
    kept = []
    try:
        entries = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError as error:
        return [], [f'现场根读不了：{root}（{error}）'], []
    scenes = [path for path in entries
              if any(path.name.startswith(prefix) for prefix in prefixes)]
    if not scenes:
        return [], [], []  # 没有现场就不必拉进程表（报告模式的常态，也省一次 PowerShell 启动）
    registered = registered_worktrees() if registered is None else registered
    usable = _usable if usable is None else usable
    if busy is None:
        busy, problem = busy_scenes([path.name for path in scenes])
        if problem:
            problems.append(problem)
            return [], problems, []
    for scene in scenes:
        if scene.name in busy:
            continue
        if is_kept_scene(scene):
            kept.append(scene)
            continue
        pristine = scene / 'pristine'
        entry = next((candidate for candidate in registered if _same_path(pristine, candidate)),
                     None)
        state = 'none'
        if entry is not None:
            if usable(entry):
                continue  # 活树：正在被 parity 用（或者跑的就是它）
            state = 'prunable'
        candidates.append(SimpleNamespace(scene=scene, pristine=entry or str(pristine),
                                          registration=state))
    return candidates, problems, kept


def reclaim_apply(candidates, *, registration_remover=None, tree_remover=None):
    """删除档：把候选现场真删掉——先摘登记，再删目录，删不掉要说话（issue #378 第二档）。

    候选由 `reclaim_candidates()` 给出（前缀 / 非活树 / 无进程引用 / 非 keep 已过），
    这里动手前对每条再独立复核一次前缀与 keep 标记——「候选列表」与「删除循环」之间
    隔着输出与等待，上游判据被改坏也不许在这条循环里误删：复核不过一律跳过并记账。

    `registration_remover` / `tree_remover` 两个注入位供门禁造假现场，默认走真实的
    `remove_registration()` 与 `_remove_tree()`。返回 `(删净条数, 问题清单)`。
    """
    registration_remover = registration_remover or remove_registration
    tree_remover = tree_remover or _remove_tree
    problems = []
    removed = 0
    total = len(candidates)
    for index, candidate in enumerate(candidates, 1):
        scene = Path(candidate.scene)
        label = f'[{index}/{total}]'
        if not is_parity_scene(scene):
            problems.append(f'{label} 候选不是本工具现场，跳过：{scene}')
            print(f'parity: {label} 拒绝非 parity 现场不删：{scene}', file=sys.stderr)
            continue
        if is_kept_scene(scene):
            problems.append(f'{label} keep 现场带 {KEEP_NAME} 标记，跳过：{scene}')
            print(f'parity: {label} 跳过 keep 现场（带 {KEEP_NAME} 标记）：{scene}',
                  file=sys.stderr)
            continue
        registration_gone = True
        if candidate.registration == 'prunable':
            reason = registration_remover(candidate.pristine)
            if reason is not None:
                registration_gone = False
                problems.append(f'{label} 登记摘不掉 — {scene}：{reason}')
                print(f'parity: {label} 登记摘不掉 — {reason}'
                      f'（手工：git worktree remove --force "{candidate.pristine}"）',
                      file=sys.stderr)
        report = Report(scene)
        try:
            tree_remover(report, scene, '现场目录')
        except OSError as error:
            report.problems.append(f'删除抛错：{error}')
        for problem in report.problems:
            problems.append(f'{label} {problem}')
            print(f'parity: {label} {problem}', file=sys.stderr)
        if _exists(scene):
            # 删不掉不许静默：把可粘贴的手工命令原样交出来（issue #378 缺口⑤同口径）。
            for command in reclaim_commands(scene, candidate.pristine
                                            if candidate.registration == 'prunable' else None):
                print(f'parity: {label} 手工清理命令 — {command}', file=sys.stderr)
            continue
        if not registration_gone:
            continue
        removed += 1
        print(f'parity: {label} 已回收 {scene}')
    return removed, problems


def reclaim_report(root=None, *, apply=False, **kwargs):
    """`--reclaim` 的输出：`apply=False` 只报告（一条都不删），`apply=True` 真删候选。

    删除档在动手前对候选**再核一次**进程引用（`busy` 未注入时真扫一次 `Win32_Process`）：
    候选判定与动手之间隔着输出与等待，新出现的引用要来得及把现场从刀口下捞回来；
    扫不到进程表 → 一条都不删（保守，退出码 1）。退出码：0 全部候选删净（或 0 候选），
    1 有问题。
    """
    target = Path(root if root is not None else tempfile.gettempdir())
    candidates, problems, kept = reclaim_candidates(root, **kwargs)
    action = '删除候选现场' if apply else '不删除任何东西'
    print(f'parity: --reclaim{" --yes" if apply else ""}（扫描 {target}，{action}）')
    for problem in problems:
        print(f'WARN: {problem}', file=sys.stderr)
    for scene in kept:
        print(f'parity: 跳过 keep 现场（带 {KEEP_NAME} 标记）— {scene}')
    if not candidates:
        print('parity: 候选 0 个（没有可回收的 parity 现场）')
        return 0
    if not apply:
        print(f'parity: 候选 {len(candidates)} 个（只打印命令，未删除任何东西）')
        for index, candidate in enumerate(candidates, 1):
            label = ('孤儿登记（登记在表里、目录已不在）' if candidate.registration == 'prunable'
                     else '无 worktree 登记')
            print(f'parity: [{index}/{len(candidates)}] {candidate.scene} — {label}')
            for command in reclaim_commands(candidate.scene, candidate.pristine
                                            if candidate.registration == 'prunable' else None):
                print(f'parity: 清理命令 — {command}')
        print(f'parity: {RECLAIM_KEEP_NOTE}')
        return 0
    print(f'parity: 候选 {len(candidates)} 个（--yes，开始删除）')
    if 'busy' not in kwargs:
        fresh, problem = busy_scenes([candidate.scene.name for candidate in candidates])
        if problem is not None:
            print(f'WARN: {problem}（一条都不删）', file=sys.stderr)
            return 1
        for candidate in candidates:
            if candidate.scene.name in fresh:
                print(f'parity: 跳过（删除前复查发现有进程引用）— {candidate.scene}')
        candidates = [candidate for candidate in candidates
                      if candidate.scene.name not in fresh]
    removed, problems = reclaim_apply(candidates)
    if problems:
        return 1
    print(f'parity: 已回收 {removed}/{len(candidates)} 个候选现场')
    print(f'parity: {RECLAIM_KEEP_NOTE}')
    return 0


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
    parser.add_argument('--reclaim', action='store_true',
                        help='扫描可回收的 parity 现场并打印清理命令（只读）；与 --yes 连用时'
                             '真删（锁被占时退出码 2）')
    parser.add_argument('--yes', action='store_true',
                        help='只与 --reclaim 连用：真删候选现场（默认只报告不删除）')
    parser.add_argument('--root',
                        help='--reclaim 的扫描根（默认系统临时目录；供回归用例注入隔离目录，'
                             '候选仍只认 idlejot-parity- 前缀）')
    args = parser.parse_args()
    if args.yes and not args.reclaim:
        parser.error('--yes 只与 --reclaim 连用')

    if args.reclaim:
        # 报告与删除都要持锁：无锁扫描会与正在建树/收尾的 parity 抢同一份登记表（issue #348）。
        # 但**不排队**——回收不是跑门禁，拿不到就让人稍后再试，不阻塞很久。
        path = lock.lock_path()
        fd = lock.acquire(path)
        if fd is None:
            # 只说「锁被占」：这一档一条都不删，连「死记录顺手清掉」也不行——旧写法调
            # `lock.recorded_holder()`，它会对 pid 已死的记录 `unlink()`（复审实测
            # `stale_holder_before=True → after=False`）。持有者细节因此不再打印。
            print('parity: 另一个 parity 正在跑，稍后再试')
            return 2
        try:
            return reclaim_report(args.root, apply=args.yes)
        finally:
            lock.release(fd, path)

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
        if args.keep:
            # keep 标记越早落地越好（issue #378 删除档）：`--keep` 的意图在启动一刻就
            # 声明，进程随后被硬杀（`finally` 不跑）标记也已在盘上，回收据此排除现场。
            write_keep_marker(scratch)
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
