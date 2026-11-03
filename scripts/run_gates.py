"""按 `scripts/gates.json` 的清单执行门禁（issue #59 / #60 / #317）。

CI 与本地同一个入口：`.github/workflows/ci.yml` 只调用本脚本，不再手写门禁数组；
清单漂移由 `scripts/check_gate_manifest.py` 钉住。顺序、单门禁参数（如
`check_publication.py --history`）、每条 ci 门禁所属的 CI 分片（`shard`）都在清单里。

    uv run python scripts/run_gates.py ci                  # 全部 ci 门禁，按清单顺序（默认串行）
    uv run python scripts/run_gates.py ci --shard static   # 只跑一个分片（CI matrix 用）
    uv run python scripts/run_gates.py ci --fail-fast      # 本机快速试错：见到红就停
    uv run python scripts/run_gates.py ci --jobs 4         # 本机并行：最多 4 条门禁同时跑
    uv run python scripts/run_gates.py ci --timeout 900    # 单条门禁超 900s 杀整棵进程树
    uv run python scripts/run_gates.py --list              # 档位计数、分片与 ci 顺序

**默认串行，`--jobs` 才并行**（issue #317）。串行是 `AGENTS.md` §2.6「安静窗口 solo
执行」的显式选择，不是能力上限：红门禁归因要用 pristine HEAD 对照判「既有环境问题
还是这次改动引入」，并发下两条门禁互相干扰会让这个判断失真。所以纪律从「永远串行」
收窄成「**归因时串行**」——`--jobs N` 是显式开关，默认值不翻转。

并行档的语义（见 `plan_parallel` / `run_parallel`）：

- **并发单位是一条门禁，不是 CI 分片**。分片是给多机 matrix 摊时间用的：`gates.json`
  里一个分片 11–30 条，本机单进程按分片并发等于串行（`--jobs 10` 跑 `reply` 分片
  实测 162s vs 串行 156s）。CI 的「同片串行、片间并行」只属于 CI matrix，本机不照搬；
- **`EXCLUSIVE` 单独收尾**：会起真实本树进程、靠全机 WMI 认进程的门禁，以及**自己就在
  测量时间**的门禁（`check_run_gates_parallel`：并发环境会改写它的测量结果），在所有
  并发门禁跑完之后独占跑，不与任何门禁重叠；
- **结果始终按清单顺序返回**，并行只改完成先后，不改结果表形状（`summary_text` 与
  `GITHUB_STEP_SUMMARY` 都依赖它）；`[n/总]` 的序号也按清单位置编，不按完成先后；
- `--fail-fast` 在并行下只阻止**后续门禁启动**，已在跑的门禁跑完——中途杀掉子门禁会
  留下半写的临时目录，让下一次归因更难；
- **`--jobs` 钳到 CPU 数**（见 `effective_jobs`）。写 `--jobs 19` 不会真开 19 条：
  每条门禁都是一个子进程（端到端门禁自己还会再起子进程），并发度超过核数只会把
  调度开销吃成纯亏（`static` 19 条并发实测 73.8s，其中独占收尾 66s）。钳制不改变
  「显式开关」的性质，只是不让它变成超配；实际生效值如实打进片头与 `--help`。

片内默认全跑并汇总（issue #60：一次红灯要看全貌），`--fail-fast` 才提前收手。
环境里有 `GITHUB_STEP_SUMMARY` 时把逐条结果写进 job 摘要，本机没有该变量时跳过。

**超时兜底不再靠管道收尾**（issue #366）：门禁的输出合流进临时文件，进程退出即收尾；
`--timeout` 到点用 `taskkill /T /F` 尽力杀整棵进程树（非 Windows 只 `kill` 直接子进程）、
放弃剩余输出。原因见 `_run_gate`：管道要读到 EOF 才算完，孙进程攥着写端时
`subprocess` 的 timeout 只杀直接子进程，随后照样等满孙进程的寿命（`timeout=8` 实测
300s 才返回）。注意这条路径**对所有调用无条件生效**（CI 也走它）：门禁退出后由幸存
孙进程继续写出的尾巴会丢，详见 `AGENTS.md` §5.1。

用 `sys.executable` 拉起子门禁：外层已由 `uv run --frozen` 进入锁定环境，
逐个再起 uv 只是重复解析同一份环境。
"""
import argparse
import collections
import concurrent.futures
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'scripts' / 'gates.json'

# 本脚本常在无控制台的父进程下跑（`pythonw`、编辑器内的任务、CI 的作业包装），而它拉起的
# 每条门禁都是控制台程序——这正是"桌面闪一下黑框"的形状（issue #353，承接 #39 / #319）。
# 抑制只在一个地方拼写，所以这里 import 运行时树里的 helper，而不是本地再抄一份标志。
#
# 但**不能硬依赖**它：`check_run_gates_parallel` 会在临时目录里搭一棵只含 `scripts/` 的
# 假树来跑本文件（夹具刻意不带运行时树），硬 import 会让那个门禁当场 `ModuleNotFoundError`。
# 跑门禁是本职，抑制是加固——拿不到 helper 就照常跑（那种情形下没有窗口要抑制也无妨），
# 而不是拒绝启动。
sys.path.insert(0, str(ROOT / 'water-group' / 'daemon'))
try:
    import winconsole  # noqa: E402  -- 位置依赖上面这行 sys.path
except ImportError:  # 夹具树/裁剪安装里没有运行时树：退回「直接跑」，不假装加固
    winconsole = None
BUCKETS = ('ci', 'manual', 'stale', 'dead', 'support')
PASSED, FAILED, SKIPPED = '通过', '失败', '未执行'
# 门禁到点被杀时的**内部**标记（issue #366）。不把 124 当 runner 的进程退出码：
# `run_ci` 只要有失败就统一返回 1，把「超时」和「门禁自己退出 1」记成同一个总账，
# 免得和 coreutils `timeout` 的约定混为一谈、让下游误判。
TIMEOUT_RC = 124

# 并行档里必须独占跑的门禁（issue #317）。两类理由，别混成一条：
#
# * `check_listener_rehang`：会起**真实的**本树 `listen.py` 子进程，再靠全机 WMI
#   （`Get-CimInstance Win32_Process`）按命令行认出它、最后 `taskkill /T /F`
#   收树（`check_listener_rehang/budget.py:640-667`、`ownership.py:303-345`、
#   `water-group/daemon/wg_up_probe.py:99-138`）。WMI 查询是全机的，另一条门禁同时起/杀
#   本树进程会让它的「前提不成立」断言假红。
# * `check_run_gates_parallel`（issue #347）：它测的就是「并行有没有真的省时间」——
#   丢进线程池里跑，泳道被别的门禁占着，桩门禁的起跑时刻和墙钟都跟着变，量出来的是
#   调度环境而不是被测实现。与 `check_listener_rehang` 同一类：**并发环境本身会改变
#   测量结果**。它也起真 `run_gates.py` 子进程（自己的临时树），跑的期间会让同池门禁
#   的时间读数整体偏移。
#
# 认名不认档：清单里改个 shard 不该悄悄放开。
EXCLUSIVE = frozenset({'check_listener_rehang', 'check_run_gates_parallel'})
#
# 墓碑（issue #369）：这个成员曾被 #353 的 `37700b7` 在**已合并的** #358（`cbb59d2`）
# 之上连同上面那两段注释一起回退——#353 的改动面是窗口抑制（`winconsole` /
# `run_passthrough`），与 `EXCLUSIVE` 无关；据父提交与改动面判断，更像是「合并/冲突处理
# 带入」，而非有意收窄（git 只能证明父提交和删掉的那几行，动机无从实证，故留余地）。
# 所以删成员前先看另外两处自述：`check_run_gates_parallel.py` 的模块 docstring 写着
# 「This gate is itself in run_gates.EXCLUSIVE」、`scripts/gates.json` 里该条的 `why`
# 写着「`check_run_gates_parallel` 自身在测计时」。三处自述要么一起改，要么都不动；
# 只删成员会让门禁在自己的泳道里量到调度环境，读数在 #339 / #347 的伪红伪绿之间漂。
# 另外 `check_run_gates_parallel.py` 的 `REQUIRED_EXCLUSIVE` 会直接在这个成员缺席时翻红
# （那是独立字面量，不从本集合派生，所以删这里不会再「两边一起收窄、永远对得上」）。


def load_manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def counts(manifest):
    return '、'.join(f'{name} {len(manifest.get(name) or [])}' for name in BUCKETS)


def shard_names(manifest):
    return [row['name'] for row in manifest.get('shards') or []]


def shard_counts(manifest):
    tally = dict.fromkeys(shard_names(manifest), 0)
    for row in manifest['ci']:
        tally[row['shard']] = tally.get(row['shard'], 0) + 1
    return '、'.join(f'{name} {count}' for name, count in tally.items())


def shard_rows(manifest, shard=None):
    return [row for row in manifest['ci'] if shard is None or row.get('shard') == shard]


def list_gates(manifest):
    print(f'清单：scripts/gates.json（{counts(manifest)}）')
    print(f'CI 分片：{shard_counts(manifest)}')
    current = None
    for index, row in enumerate(manifest['ci'], 1):
        if row.get('shard') != current:
            current = row.get('shard')
            print(f'[{current}]')
        args = ''.join(' ' + arg for arg in row.get('args') or [])
        print(f'{index:>3}. {row["name"]}{args} — {row["why"]}')
    return 0


def command_of(row):
    name = row['name']
    if name.endswith('.py'):
        # File-as-script：清单里 80 个文件门禁仍走 path-as-script（cwd=ROOT 时
        # sys.path[0] 已是 scripts/，`import check_fixture_support` 能直接解析）。
        return [sys.executable, str(ROOT / 'scripts' / name), *(row.get('args') or [])]
    # Package gate：`name` 不带 `.py` 即一个 `scripts/` 下的包目录，用 `-m` 调起
    # 其 `__main__.py`（issue #169 PF-2）。`-m` 会把 `cwd=ROOT` 加进子进程
    # `sys.path[0]`，`scripts` 作为命名空间包即解析——无需父进程动 sys.path。
    return [sys.executable, '-m', f'scripts.{name}', *(row.get('args') or [])]


def gate_name(row):
    """清单条目的门禁名：去掉 `.py`（文件门禁）后与包门禁同形，`EXCLUSIVE` 按它比对。"""
    name = row['name']
    return name[:-3] if name.endswith('.py') else name


def plan_parallel(rows):
    """把清单切成 `(并发条目, 独占条目)`，两者都是 `[(清单位置, row), ...]`（issue #317）。

    并发条目保持清单顺序，`EXCLUSIVE` 里那些条目被摘出来，**在所有并发条目收尾之后**
    单独串行跑。摘的是名单不是位置：清单里改个 `shard` 不该悄悄放开独占。

    比对用 `gate_name`：清单里同一个门禁可能写成 `x.py`（文件）也可能写成 `x`（包），
    而 `EXCLUSIVE` 只写短名——拿原始 `row['name']` 比，文件形态的条目会整条漏掉独占。
    """
    lanes, exclusive = [], []
    for position, row in enumerate(rows):
        (exclusive if gate_name(row) in EXCLUSIVE else lanes).append((position, row))
    return lanes, exclusive


def effective_jobs(requested):
    """把 `--jobs` 收进这台机器真能用的并发度：`min(requested, cpu_count)`（issue #317）。

    每条门禁都是一个独立子进程，端到端门禁（`reply` 分片那一片）自己还会再起子进程，
    所以「同时跑几条」受核数约束，超出的部分只是排队等核、外加线程池的调度开销。
    `--jobs 19` 在 8 核机上实测 73.8s，而其中独占收尾一条就 66s——并行那部分本来就
    薄，再超配纯亏。

    边界：`requested <= 1` 原样返回（串行不开池，不在这里钳）；`cpu_count()` 拿不到
    （极少数平台/受限容器返回 `None` 或 0）时回落 `requested`，宁可照用户说的跑，
    也不在这里编一个核数出来。

    **它不改变「显式开关」的性质**：默认仍是 1，钳制只作用于 `>= 2`。
    """
    if requested <= 1:
        return requested
    cores = os.cpu_count() or 0
    return min(requested, cores) if cores >= 1 else requested


def _spawn(args, cwd, sink):
    """起一条门禁子进程，stdout/stderr 合流进 `sink`（issue #366）。

    窗口抑制仍走 `winconsole.popen_hidden`：门禁全部是控制台程序，这个脚本又常常没有
    控制台，裸 `subprocess.Popen` 会给每条门禁新开一个窗口（issue #353 用窗口钩子在真实
    门禁链上抓到 `git.exe` 标题的控制台窗口）。没有 helper 的夹具树里退回裸调用：那里
    没有运行时树，也就没有窗口问题的前提。
    """
    kwargs = dict(cwd=cwd, stdout=sink, stderr=subprocess.STDOUT)
    if winconsole is None:
        return subprocess.Popen(args, **kwargs)
    return winconsole.popen_hidden(args, **kwargs)


def _kill_tree(proc):
    """杀掉 `proc` 的**整棵进程树**（issue #366）。

    `Popen.kill()` 只作用于直接子进程：门禁派生出的孙进程（`supervisor.py` 拉起的后端、
    `listen.py` 的 worker、桩自己起的短命进程）会活下来继续攥着句柄。Windows 用
    `taskkill /F /T`，其它平台退化为 `kill`——本仓的 runner 只在 Windows 上跑，这里不
    追求跨平台的树杀语义。杀完再兜一次 `proc.kill()`：taskkill 本身失败时至少收掉直接
    子进程，不会把整次执行挂在一个已经判死的门禁上。
    """
    if os.name != 'nt':
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    else:
        command = ['taskkill', '/F', '/T', '/PID', str(proc.pid)]
        try:
            if winconsole is None:
                subprocess.run(command, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10)
            else:
                # taskkill 的输出没人要，`run_hidden` 正是为这类调用留的（见 #91）。
                winconsole.run_hidden(command, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _run_gate(args, cwd, timeout):
    """跑一条门禁，返回 `(退出码, 是否超时, 秒数)`（issue #366）。

    输出先落本进程持有的临时文件：进程退出后读回并整块写回本进程 stdout，#91 的
    「门禁输出必须回到调用方」照旧（失败时的 traceback 也在这条路径上）。

    **为什么不再用管道**：管道要读到 EOF 才算收尾，而孙进程继承写端还活着时 EOF 要等
    它自然退出；`subprocess` 的 timeout 又只杀直接子进程，所以「`timeout=8`」实测在
    300s 才返回。普通文件没有 EOF 语义：进程退出就能收尾，孙进程攥着文件句柄也拖不住
    任何等待。放弃它之后写进来的尾巴读不到——这是本 issue 明确接受的代价，宁可少一截
    尾巴，也不让一条门禁把整次执行挂住。

    `timeout` 为空时不设超时。到点杀整棵树后最多再等 5s 收尸，之后无论进程是否真退，
    都按超时记账（内部标记 `TIMEOUT_RC`；runner 的总退出码仍与其它失败一致为 1，见
    `run_ci`），绝不回到无界等待。`taskkill` 本身失败或非 Windows 平台（那里只 `kill`
    直接子进程）时可能留下孙进程：这里只保证「不再无界等待」，不宣称一定清干净。
    """
    started = time.perf_counter()
    timed_out = False
    with tempfile.TemporaryFile(mode='w+b') as sink:
        proc = _spawn(args, cwd, sink)
        try:
            proc.wait(timeout=timeout or None)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        sink.seek(0)
        text = sink.read().decode('utf-8', 'replace')
    # 子进程的 `TextIOWrapper` 在 Windows 上把行尾写成 `\r\n`；原样交给本进程的
    # `sys.stdout` 会再翻译一次，落成 `\r\r\n`（CI 日志里每条门禁输出后多一条空记录，
    # 复核在 PR #370 实测过）。先按 universal newlines 归一，行尾由 `sys.stdout` 按平台
    # 决定——#91 的「整块写回、成对行数不变」因此仍是 1:1。
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if text and sys.stdout is not None:
        if not text.endswith('\n'):
            text += '\n'
        sys.stdout.write(text)
        sys.stdout.flush()
    code = TIMEOUT_RC if timed_out else proc.returncode
    return code, timed_out, time.perf_counter() - started


def run_one(row, printed, total, timeout=None):
    """跑一条门禁，返回 `(结果, 秒数)`；串行与并行共用这一份。

    `printed` 是这条门禁在本次执行里的序号，**按清单顺序**编，不按完成先后——
    并行下两条同时收尾也不会打出两个相同的 `[3/92]`。

    执行路径见 `_run_gate`：输出合流进临时文件（不靠管道 EOF 判结束），`timeout` 到点
    杀整棵进程树（issue #366）。窗口抑制仍由 `winconsole.popen_hidden` 负责（#353）。
    """
    code, timed_out, elapsed = _run_gate(command_of(row), ROOT, timeout)
    status = PASSED if code == 0 else FAILED
    note = '，超时已杀整棵进程树' if timed_out else ''
    print(f'[{printed}/{total}] {row["name"]} {status} ({elapsed:.1f}s{note})', flush=True)
    return status, elapsed


def run_serial(rows, fail_fast, timeout=None):
    """逐条串行跑；返回 `{清单位置: (结果, 秒数)}` 与是否被 `--fail-fast` 截断。"""
    outcome, cut = {}, False
    total = len(rows)
    for position, row in enumerate(rows):
        outcome[position] = run_one(row, position + 1, total, timeout)
        if outcome[position][0] == FAILED and fail_fast:
            cut = True
            print(f'  后续 {total - position - 1} 条未执行（--fail-fast）')
            break
    return outcome, cut


def run_parallel(rows, fail_fast, jobs, timeout=None):
    """并发跑；返回 `{清单位置: (结果, 秒数)}` 与是否被 `--fail-fast` 截断（issue #317）。

    并发单位是**一条门禁**，不是 CI 分片：CI 的分片是给多机 matrix 摊时间用的，
    本机单进程里按分片并发等于串行（`reply` 一个分片就 30 条），`--jobs` 会是空开关。
    片内串行的保证只属于 CI matrix（同片串行、片间并行），本机不受它约束。

    `EXCLUSIVE` 在**线程池收摊之后**单独串行跑，不交给池子排队：池子里排着等于和并发
    条目同池竞争，`--jobs` 大于并发条目数时它会被立刻取走、与还在跑的门禁重叠，独占就
    名存实亡（`check_run_gates_parallel.py` 的独占用例钉住这一条）。

    `--fail-fast` 只拦住**还没启动**的条目，在途的跑完——中途杀掉在跑的子门禁会留下
    半写的临时目录，让下一次归因更难。

    没收尾的条目不出现在返回值里，由 `run_rows` 统一记成 `未执行`。
    """
    lanes, exclusive = plan_parallel(rows)
    total = len(rows)
    outcome, cut = {}, False

    if lanes:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            running = {}
            pending = collections.deque(lanes)
            while pending and not cut:
                while pending and len(running) < jobs and not cut:
                    position, row = pending.popleft()
                    running[pool.submit(run_one, row, position + 1, total, timeout)] = position
                if not running:
                    break
                finished, _ = concurrent.futures.wait(
                    running, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in finished:
                    position = running.pop(future)
                    outcome[position] = future.result()
                if fail_fast and any(status == FAILED for status, _ in outcome.values()):
                    cut = True
            # 在途的跑完再走：不取消 future，只等它们收尾。
            for future in list(running):
                position = running.pop(future)
                outcome[position] = future.result()

    for position, row in exclusive:
        if cut:
            break
        outcome[position] = run_one(row, position + 1, total, timeout)
        if fail_fast and outcome[position][0] == FAILED:
            cut = True

    if cut and len(outcome) < total:
        print(f'  余下 {total - len(outcome)} 条未执行（--fail-fast）')
    return outcome, cut


def run_rows(rows, fail_fast, jobs=1, timeout=None):
    """执行入口：`jobs <= 1` 走串行（与改动前同形），否则按门禁并发。

    返回 `([(row, 结果, 秒数)], 总秒数)`，条目顺序**始终是清单顺序**——并行只改
    完成先后，不改结果表形状（`summary_text` 与 `GITHUB_STEP_SUMMARY` 都依赖它）。
    `timeout` 是**单条门禁**的超时（issue #366），串行与并行都生效。
    """
    total = len(rows)
    started = time.perf_counter()
    if jobs <= 1:
        outcome, _ = run_serial(rows, fail_fast, timeout)
    else:
        outcome, _ = run_parallel(rows, fail_fast, jobs, timeout)
    results = [(row, *outcome.get(position, (SKIPPED, 0.0)))
               for position, row in enumerate(rows)]
    return results, time.perf_counter() - started


def summary_text(results, shard, elapsed):
    """分片结果表：逐条结果 + 通过/失败/未执行计数 + 耗时。"""
    passed = sum(1 for _, status, _ in results if status == PASSED)
    failed = [row['name'] for row, status, _ in results if status == FAILED]
    skipped = sum(1 for _, status, _ in results if status == SKIPPED)
    lines = [
        f'### 门禁分片 `{shard}`（{len(results)} 条）',
        '',
        f'{PASSED} {passed} · {FAILED} {len(failed)} · {SKIPPED} {skipped} · 耗时 {elapsed:.1f}s',
        '',
    ]
    if failed:
        lines += ['失败：' + '、'.join(f'`{name}`' for name in failed), '']
    # 行号 = 清单位置，与 stdout 的 `[n/total]` 同一套编号；同一条门禁重复出现在清单里时
    # （`check_publication.py` 就出现过带/不带 `--history` 两条），光看名字分不开是哪一条。
    lines += ['| # | 门禁 | 结果 | 耗时 |', '|---|---|---|---|']
    for index, (row, status, seconds) in enumerate(results, 1):
        args = ''.join(' ' + arg for arg in row.get('args') or [])
        lines.append(f'| {index} | `{row["name"]}{args}` | {status} | {seconds:.1f}s |')
    return '\n'.join(lines) + '\n'


def write_summary(results, shard, elapsed):
    """CI 里把结果追加进 `$GITHUB_STEP_SUMMARY`；本机没有该变量时静默跳过。"""
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(summary_text(results, shard, elapsed))


def run_ci(manifest, shard=None, fail_fast=False, jobs=1, timeout=None):
    rows = shard_rows(manifest, shard)
    label = shard or '全部分片'
    jobs = effective_jobs(jobs)
    mode = '串行' if jobs <= 1 else f'并行 {jobs}'
    print(f'门禁 {len(rows)} 条，分片 {label}，{mode}，顺序来自 scripts/gates.json')
    results, elapsed = run_rows(rows, fail_fast, jobs, timeout)
    write_summary(results, shard or 'all', elapsed)
    failed = [row['name'] for row, status, _ in results if status == FAILED]
    if failed:
        print(f'FAIL: {len(failed)} 条未通过：' + '、'.join(failed))
        return 1
    print(f'PASS: {len(results)} 条门禁全部通过（{elapsed:.1f}s）')
    return 0


def main():
    parser = argparse.ArgumentParser(description='按清单执行门禁（issue #59 / #60 / #317）')
    parser.add_argument('group', nargs='?', choices=['ci'], help='要执行的档位（目前只有 ci）')
    parser.add_argument('--shard', help='只跑某个 CI 分片（名字来自 scripts/gates.json 的 shards）')
    parser.add_argument('--fail-fast', action='store_true', help='见到第一个失败就停（默认片内全跑）')
    parser.add_argument('--list', action='store_true', help='打印档位计数、分片与 ci 顺序')
    parser.add_argument('--jobs', type=int, default=1, metavar='N',
                        help='最多 N 条门禁同时跑（默认 1 = 串行；归因红门禁时保持 1；'
                             'N 超过本机 CPU 数时按 CPU 数跑，片头打印实际生效值）')
    parser.add_argument('--timeout', type=float, default=None, metavar='SECONDS',
                        help='单条门禁的超时秒数（默认不设；Windows 下到点 taskkill /T '
                             '尽力杀整棵进程树，该条记失败，runner 退出码仍为 1，见 issue #366）')
    args = parser.parse_args()
    if args.jobs < 1:
        print(f'FAIL: --jobs 必须 >= 1，收到 {args.jobs}')
        return 2
    if args.timeout is not None and args.timeout <= 0:
        print(f'FAIL: --timeout 必须 > 0（不设超时请省略该开关），收到 {args.timeout}')
        return 2
    if not MANIFEST.is_file():
        print(f'FAIL: 缺少清单 {MANIFEST.relative_to(ROOT)}')
        return 1
    manifest = load_manifest()
    if args.list:
        return list_gates(manifest)
    if not args.group:
        parser.print_help()
        return 2
    if args.shard and args.shard not in shard_names(manifest):
        print(f'FAIL: 未知分片 {args.shard!r}；合法值：' + '、'.join(shard_names(manifest)))
        return 2
    return run_ci(manifest, args.shard, args.fail_fast, args.jobs, args.timeout)


if __name__ == '__main__':
    sys.exit(main())
