"""Runner parallel gate: ``--jobs N`` really overlaps, and never opens up what must stay alone (issue #317).

Background. ``scripts/run_gates.py`` ran every gate one at a time; a local full
run was minutes of pure waiting even though most gates are independent, so the
runner grew a ``--jobs N`` switch. Two things about it are easy to get wrong and
no other gate would notice:

1. **Overlap has to be real, not declared.** The first design parallelised per
   CI *shard*; since ``gates.json`` puts 11-30 gates in each shard that was a
   no-op locally (measured: ``reply --jobs 10`` 162s vs 156s serial). A runner
   that quietly falls back to serial still prints a green summary, so the only
   honest check is wall-clock overlap of two gates that start together.
2. **``EXCLUSIVE`` must not be widened by accident.** ``check_listener_rehang``
   starts a real in-tree ``listen.py``, recognises it through machine-wide WMI,
   then tears the tree down with ``taskkill /T /F``
   (``scripts/check_listener_rehang/budget.py:640-667``,
   ``water-group/daemon/wg_up_probe.py:99-138``). Running anything alongside it
   makes its "no listener yet" premise false. Membership is by gate *name*, so
   re-sharding the manifest must not be able to open it up. The **required**
   members are a literal list in this file (``REQUIRED_EXCLUSIVE``), never
   derived from the set under test: #353's ``37700b7`` dropped
   ``check_run_gates_parallel`` and the fixture, built from
   ``run_gates.EXCLUSIVE``, shrank with it (#369).

Everything here is driven through the **real CLI** against a temp tree of stub
gates (``scripts/gates.json`` + ``scripts/stub_*.py``), so the wire contract --
rows, ``[n/total]`` numbering, summary, exit code -- is what is under test:

1. serial (default and ``--jobs 1``) is strictly one at a time, in manifest order;
2. ``--jobs 2`` makes two gates that share a shard genuinely overlap, while
   results still come back in **manifest** order, not completion order;
3. every name in ``EXCLUSIVE`` never overlaps another gate and starts only after
   every parallel lane has finished;
4. ``--fail-fast`` under parallelism stops *starting* further gates but lets
   gates already in flight run to completion (killing them would leave
   half-written fixtures for the next attribution run);
5. ``--jobs`` also works on a subset (``--shard``), so CI's per-shard invocation
   keeps the same semantics as the local full run;
6. ``--timeout`` bounds a **single** gate (issue #366): a gate that leaves a
   grandchild holding its stdout still returns promptly (the output comes back
   from a temp file, not a pipe awaiting EOF), and a gate that hangs past the
   limit is failed by tearing down its **whole process tree**;
7. the bytes written back are normalized to universal newlines (a child's
   ``\r\n`` must not become ``\r\r\n`` in CI logs), and the tail that a
   *surviving* grandchild writes **after** the gate exits is dropped -- the
   file-backed capture has no EOF, that is the #366 trade-off.

Wall-clock is a *coarse* sieve and it is judged **relative to the serial baseline
measured in the same run** (issue #347): ``_run()`` times a whole subprocess, so
the absolute constant ``4 x STUB_SLEEP x 0.9`` used to leave interpreter start-up
and thread-pool dispatch out of the budget and turned correct parallel runs red on
this machine (1.08-1.52s against a 1.08s line).

The ratio is still only an indirect signal, and on a loaded machine it is a bad
one: two lanes want two cores, so when the box is saturated the second wave gets
half a core and ``--jobs 2`` saves 25-35% instead of 50% (measured 0.65-0.75,
issue #339). Relaxing the check to "any faster than serial" fixes that false red
but removes the check entirely — a de-parallelised runner still wins tens of
milliseconds of scheduling jitter. So the two signals are split by *what they
prove* (``_judge_parallel``): ``pairs`` proving overlap is enough on its own,
and the ratio only decides when ``pairs`` found nothing. A run slower than its
own serial baseline is red either way.

This gate is itself in ``run_gates.EXCLUSIVE`` (issue #347): it measures parallel
timing, so running it inside the very pool it measures would measure the pool's
scheduling instead of the implementation.

    .venv/Scripts/python.exe scripts/check_run_gates_parallel.py
    .venv/Scripts/python.exe scripts/check_run_gates_parallel.py --self-test
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
RUNNER = SCRIPTS / 'run_gates.py'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_fixture_support as fx  # noqa: E402  (canonical: shared fixtures first)
import run_gates  # noqa: E402  (only for EXCLUSIVE; the run itself is a subprocess)

# 重叠判定容差：两个门禁「首尾相接」和「重叠几十毫秒」不该被当成同一件事。
# 桩门禁各睡 0.30s，重叠时重合量在 0.2s 量级、串行时正好 0.0s。
OVERLAP_SLACK = 0.05
# 桩门禁睡多久。够长到让调度抖动淹没不掉重叠，又短到整套用例几秒跑完。
STUB_SLEEP = 0.30
# issue #366 的两个时间预算：都远小于桩留的 300s 孙进程寿命，也远小于 `_run` 的
# 外层 timeout——修复前那两种形状都要等满孙进程，修复后应在数秒内返回。
HOLD_BUDGET = 20.0
TIMEOUT_BUDGET = 20.0
# 墙钟判据的松紧（issue #347）。两条泳道跑 4 条等长桩，理论时长正好是串行的一半；
# 运行时再打三成余量给负载抖动——越过这条线就不是「波动」了，是「没并行」。
PARALLEL_SLACK = 1.30
# `EXCLUSIVE` 的**必需成员**（issue #369）。这是独立字面量，**不许**从
# `run_gates.EXCLUSIVE` 派生：派生出来的期望值会跟着被测集合一起收窄，成员被删时夹具与
# 断言同时变小、永远对得上——#353 的 `37700b7` 把 `check_run_gates_parallel` 删出集合那次
# 就是这么绿过去的（当年的夹具直接 `sorted(run_gates.EXCLUSIVE)`，断言只判「非空」）。
# 加/删成员时三处一起改：这里、`run_gates.EXCLUSIVE` 定义处、`gates.json` 该条的 `why`。
REQUIRED_EXCLUSIVE = frozenset({'check_listener_rehang', 'check_run_gates_parallel'})
# 片头那一行（`run_gates.run_ci` 打进控制台的那句）。捕获组取**实际生效的并发度**：
# 断言钉的是这个数字，不是「输出里出现过 并行」。
BANNER_RE = re.compile(r'门禁\s+\d+\s+条.*?(?:并行\s+(\d+)|(串行))')

_STUB_TEMPLATE = '''\
import sys, time
from pathlib import Path

# 每次执行在 `tag` 名下记一行 `标签 起 止`，供父门禁算重叠；`tag` 以 fail 开头则退出 1。
# 标签必须写死在桩里、不能拿文件名推：独占用例里可能有好几个桩同时叫
# `check_listener_rehang.py`（一个是 EXCLUSIVE 位置的桩，另外几个是泳道上的对照桩），
# 按文件名推会把它们记成同一个标签，把「泳道在跑」误读成「独占泄漏」。
name = Path(__file__).resolve().stem
tag = {tag!r}
traces = Path(__file__).resolve().parent / 'traces'
traces.mkdir(exist_ok=True)
started = time.time()
time.sleep({sleep})
(traces / (tag + '.log')).open('a', encoding='utf-8').write(
    '%s %.6f %.6f\\n' % (tag, started, time.time()))
sys.exit(1 if tag.split('_')[0] == 'fail' else 0)
'''

# issue #366 的两棵桩树：都派生一个睡 300s 的孙进程，特征串写进它的命令行——用例按
# 特征串认残留，不按进程名通配，也不碰别的会话。区别只在桩自己退不退出：
# `_HOLD_STUB` 打完一行就退出（孙进程继续攥着 stdout），`_SLOW_STUB` 自己睡 300s。
_HOLD_STUB = '''\
import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", {marker!r}])
print("HOLD_STUB_DONE")
sys.exit(0)
'''

_SLOW_STUB = '''\
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", {marker!r}])
time.sleep(300)
'''

# 行尾字节契约（PR #370 复核实测）：门禁子进程的 `TextIOWrapper` 在 Windows 上写出
# `\r\n`，runner 必须归一后只落一次行尾；原样再经 `sys.stdout` 会变成 `\r\r\n`。
_EOL_STUB = '''\
import sys
print("EOL-1")
print("EOL-2")
print("EOL-3")
sys.exit(0)
'''


def _build_tree(root, names, shard='only'):
    """造一棵桩树：`scripts/gates.json` 列出 `names`，每个名字一个桩门禁文件。

    桩的日志标签默认就是文件名，用在「一个文件名只出现一次」的树里。**文件名要在
    清单里重复出现时，必须写 `(文件名, 标签)` 显式给每个位置起不同标签**：桩文件是
    按文件名落盘的，同名位置共用同一份内容（最后一次写入的标签），于是两个位置跑出来
    的轨迹标签一样——那种树里分不出是哪个位置跑的，断言只能全部落在结果表的行号上
    （见独占用例）。这里对此显式拒绝，免得又写出一棵「看着分开、其实分不开」的树。
    """
    scripts = root / 'scripts'
    (scripts / 'traces').mkdir(parents=True, exist_ok=True)
    entries = [(name, name) if isinstance(name, str) else tuple(name) for name in names]
    repeated = {name for name, _ in entries if [n for n, _ in entries].count(name) > 1}
    for name, tag in entries:
        if name in repeated and tag == name:
            raise ValueError(f'{name} appears twice but has no explicit tag')
        (scripts / (name + '.py')).write_text(
            _STUB_TEMPLATE.format(sleep=STUB_SLEEP, tag=tag), encoding='utf-8')
    manifest = {
        'version': 1,
        'note': 'check_run_gates_parallel fixture',
        'shards': [{'name': shard, 'why': 'fixture'}],
        'ci': [{'name': name + '.py', 'shard': shard, 'why': 'fixture'} for name, _ in entries],
        'manual': [], 'stale': [], 'dead': [], 'support': [],
    }
    (scripts / 'gates.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return root


def _run(root, args, timeout=180, console_only=False):
    """以子进程跑真 `run_gates.py`，返回 `(退出码, 结果表原文, 秒数)`。

    结果表**只写 `$GITHUB_STEP_SUMMARY`**，本机 stdout 上只有 `[n/total] …` 行
    （那是跑完瞬间打的，并行下完成先后天然乱序）。所以这里给每次运行塞一个
    临时 summary 文件、跑完读回来——断言的就是 CI 真正看到的那个契约面。

    `console_only=True` 时改回 `(退出码, 控制台原文, 秒数)`：片头（`并行 N` / `串行`）
    是打到控制台的，summary 里没有；要断言它就取这一份（issue #317）。
    """
    runner = root / 'scripts' / 'run_gates.py'
    runner.write_text(RUNNER.read_text(encoding='utf-8'), encoding='utf-8')
    summary = root / 'step-summary.md'
    summary.unlink(missing_ok=True)
    env = dict(os.environ)
    env['GITHUB_STEP_SUMMARY'] = str(summary)
    env.pop('PYTHONPATH', None)
    tick = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(runner), *args],
        cwd=root, env=env, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=timeout)
    elapsed = time.perf_counter() - tick
    console = (proc.stdout or '') + (proc.stderr or '')
    table = summary.read_text(encoding='utf-8') if summary.is_file() else ''
    body = console if console_only else (table or ('NO SUMMARY\n' + console))
    return proc.returncode, body, elapsed


def _banner_line(console):
    """片头那行（`并行 N` 或 `串行`），找不到返回 `None`。"""
    for line in console.splitlines():
        if BANNER_RE.search(line):
            return line.strip()
    return None


def _banner_width(console):
    """片头报的并发度：`并行 N` 取 N，`串行` 取 1，没片头返回 `None`。

    用捕获组取数字而不是判子串：`"并行 2" in text` 挡不住「片头写 并行 999、
    事后补一行 并行 2」这种输出，那种输出对读者与坏实现同害（#151 的老坑）。
    """
    line = _banner_line(console)
    if line is None:
        return None
    match = BANNER_RE.search(line)
    return int(match.group(1)) if match.group(1) else 1


def _spans(root):
    """读回桩门禁记录的 `(标签, 起, 止)`，按开始时刻排序。"""
    spans = []
    for trace in sorted((root / 'scripts' / 'traces').glob('*.log')):
        for line in trace.read_text(encoding='utf-8').splitlines():
            if line.strip():
                label, start, end = line.split()
                spans.append((label, float(start), float(end)))
    return sorted(spans, key=lambda row: row[1])


def _overlaps(spans):
    """所有互相重叠的标签对；串行执行（含独占收尾）必须为空。"""
    pairs = []
    for index, (left, lstart, lend) in enumerate(spans):
        for right, rstart, rend in spans[index + 1:]:
            if min(lend, rend) - max(lstart, rstart) > OVERLAP_SLACK:
                pairs.append((left, right))
    return pairs


def _clear_traces(root):
    for trace in (root / 'scripts' / 'traces').glob('*.log'):
        trace.unlink()


def _marker_pids(marker):
    """按命令行特征串认这个用例自己起的进程（桩自身与桩派生的孙进程都算）。

    不限定进程名（`python.exe` / `python3.x.exe` / `pythonw.exe` 都可能），只排除
    查询自身那条 `powershell`；特征串每次运行唯一，本机同时跑着别的门禁也不会误伤。
    """
    command = ("Get-CimInstance Win32_Process | "
               f"Where-Object {{ $_.ProcessId -ne $PID -and "
               f"$_.CommandLine -like '*{marker}*' }} | "
               "ForEach-Object { $_.ProcessId }")
    out = subprocess.run(['powershell', '-NoProfile', '-Command', command],
                         capture_output=True, text=True, timeout=60).stdout
    return [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]


def _kill_pids(pids):
    """收掉 `_marker_pids` 认出来的残留（用例自己起的，失败路径也要清干净）。

    `/T` 是兜底：本机 venv 的 `python.exe` 是重定向器，按 PID 单杀会留下它起的那层
    真实解释器；树杀把两层一起收掉。
    """
    for pid in pids:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                       capture_output=True, timeout=30)


def _parallel_fast_enough(serial_elapsed, parallel_elapsed):
    """并行那一趟是否真的比串行基线省了时间（issue #347）。

    判据**相对同一次运行的串行基线**，不用绝对常数。`_run()` 计的是整次子进程墙钟，
    里面包着「解释器启动 + 导入 run_gates 全部依赖 + 线程池调度 + 每个子桩解释器启动」
    这些与被测语义无关的固定开销：本机实测光是起 `run_gates.py --list` 就 ~0.16s，
    每个桩子解释器 0.36–0.38s（不是 `STUB_SLEEP` 的 0.30s）。原来的绝对阈值
    `4 × STUB_SLEEP × 0.9 = 1.08s` 把这堆开销一分不算，本机落点 1.08–1.52s 正骑在
    线上，于是「真的并行了」被判成「没并行」（issue 里连 5 次 5 红）。

    分母取 `--jobs 1` 那一趟：同样是 4 条桩、同样的解释器启动与调度开销，机器快慢
    与空载/满载的差异在相除时被约掉，只剩「并行有没有真的把两波叠成一波」。

    **它是粗筛，不是判据本身**。本机满载（同机既有 parity/门禁在跑）实测比值落在
    0.65–0.75（#339 记录 0.65/0.72，本 PR 复现 0.75）：两条泳道各要一个核，机器被占满
    时第二波只能分到半份 CPU 时间，于是「并行了但没省一半」。那种情况**不是实现的问题**，
    所以调用点用 `pairs`（两条泳道在时间轴上真的重合过）兜底，只在 `pairs` 也没抓到
    重叠时才把墙钟当证据——见 `scan()` 里 `--jobs 2` 那一段。

    边界（在 docstring 里声明，不声称「任何错都红」）：一个「串行跑完 4 条、再空等
    到正好半程」的实现能耗掉这里的额度。它拦不住，但 `pairs` 会——两条泳道从未
    同时在场。两者组合起来才钉住并行性，本函数只做粗筛。
    """
    if serial_elapsed <= 0:
        return False
    return parallel_elapsed <= serial_elapsed / 2 * PARALLEL_SLACK


def _summary_rows(out):
    """抽结果表（`| # | 门禁 | 结果 | 耗时 |`）里按行号排好的 `(行号, 名字, 结果)`。

    **不能拿 `[n/total] …` 的打印顺序当结果顺序**：那一行是门禁跑完的瞬间打的，
    并行下完成先后天然乱序（这正是并行的意思），而契约要求的是**结果表**按清单
    顺序。拿打印顺序断言，会把一个正确的并行实现判成红的。

    行号（= 清单位置）一起抽出来：同一条门禁可以在清单里出现两次（独占用例里专门
    安排了一个同名桩），只看名字分不开是哪一行跑掉了。
    """
    rows = []
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped.startswith('|'):
            continue
        cells = [cell.strip() for cell in stripped.strip('|').split('|')]
        if len(cells) != 4 or not cells[0].isdigit():
            continue
        token = cells[1].strip('`').split(' ')[0]
        rows.append((int(cells[0]), token[:-3] if token.endswith('.py') else token, cells[2]))
    return rows


def _judge_parallel(serial_elapsed, parallel_elapsed, pairs):
    """`--jobs 2` 这一趟算不算「真的并行了」——返回 `(是否通过, 备注, 失败原因)`。

    两条证据，**分工不同、不许互相替代**（issue #347 / #339）：

    * `pairs`：两条泳道在时间轴上真的重合过。这是**直接**证据，抓到就是并行；
    * 墙钟比值：最大的一笔间接证据，只在 `pairs` 什么都没抓到时才拿来定性。

    为什么不能让墙钟单独判红：机器被别的门禁占满时，第二波泳道只分到半份 CPU，
    `--jobs 2` 只省 25-35%（本机满载实测 0.75，满负荷记录 0.65/0.72），于是真并行
    被判成没并行——#339 当年就是撞上这个才把判据放宽成 `elapsed < serial_elapsed`。
    但那条放宽等于取消防线：退化成一条泳道时，`--jobs 2` 照样比 `--jobs 1` 快几十
    毫秒的调度抖动。把「谁作证」分开，两头的错都堵上。

    反过来也不许让 `pairs` 吃掉墙钟的额度：`pairs` 有重叠但墙钟**慢过串行基线**，
    那说明两次执行根本没并行到该有的程度（或者重叠判定本身坏了），仍判红。

    已知边界：`pairs` 抓到重叠时，这里不再对「省了多少时间」设上界，一个「并行但
    串行化写 trace」的实现能过。`pairs` 才是那条契约的主人，本函数只补它看不出的
    「时间上一分没省」。
    """
    notes, failures = [], []
    ratio = parallel_elapsed / serial_elapsed if serial_elapsed else 0.0
    if parallel_elapsed >= serial_elapsed:
        failures.append(
            f'--jobs 2: {parallel_elapsed:.2f}s vs serial baseline {serial_elapsed:.2f}s '
            f'-- no faster than serial at all')
    elif not _parallel_fast_enough(serial_elapsed, parallel_elapsed):
        if pairs:
            notes.append(
                f'--jobs 2 {parallel_elapsed:.2f}s is {ratio:.2f}x serial despite overlapped '
                f'{pairs} -- machine was saturated, overlap is the evidence here')
        else:
            failures.append(
                f'--jobs 2: {parallel_elapsed:.2f}s vs serial baseline {serial_elapsed:.2f}s '
                f'-- not parallel (expected <= '
                f'{serial_elapsed / 2 * PARALLEL_SLACK:.2f}s)')
    return not failures, notes, failures


def _exclusive_membership_failures(exclusive):
    """`EXCLUSIVE` 是否保住了全部必需成员；返回 failure 列表（issue #369）。

    判据只读传进来的集合，不读夹具、不起子进程——所以 `--self-test` 可以纯数据喂它。

    「集合非空」不够：清空与「只剩一个」都是成员缺席，且都要**指名**报出缺了谁。
    #369 的漏检就是这两级台阶：夹具集合同步收窄（对不上也看不出来）、断言只判非空
    （收窄后照样绿）。期望值来自本文件的 `REQUIRED_EXCLUSIVE` 字面量，不从这里再派生。
    """
    missing = sorted(REQUIRED_EXCLUSIVE - set(exclusive))
    if not missing:
        return []
    return [
        f'EXCLUSIVE lost required member(s) {missing} (have {sorted(exclusive)}): they change '
        f'the readings of the pool they would run in -- see run_gates.EXCLUSIVE and #369'
    ]


def scan(root):
    """跑完全部用例，返回 `(failures, notes)`。"""
    failures, notes = [], []
    base = root / 'runner-parallel'
    base.mkdir(parents=True, exist_ok=True)
    order_expected = ['stub_a', 'stub_x', 'stub_b', 'stub_c']

    # --- 1. 串行（默认）严格按清单顺序、零重叠 ---
    tree = _build_tree(base / 'serial', order_expected)
    code, out, elapsed = _run(tree, ['ci'])
    if code != 0:
        failures.append(f'serial default: must pass, exit={code}\n{out}')
    serial_pairs = _overlaps(_spans(tree))
    if serial_pairs:
        failures.append(f'serial default: gates overlapped {serial_pairs}')
    if [name for _, name, _ in _summary_rows(out)] != order_expected:
        failures.append(f'serial default: order drifted {_summary_rows(out)}')
    if elapsed < 4 * STUB_SLEEP * 0.9:
        failures.append(
            f'serial default: {elapsed:.2f}s for 4 x {STUB_SLEEP}s -- not serial')
    notes.append(f'serial default {elapsed:.2f}s (4 x {STUB_SLEEP}s)')

    _clear_traces(tree)
    code, out, serial_elapsed = _run(tree, ['ci', '--jobs', '1'])
    if code != 0:
        failures.append(f'--jobs 1: must pass, exit={code}\n{out}')
    if _overlaps(_spans(tree)):
        failures.append(f'--jobs 1: overlapped {_overlaps(_spans(tree))}')
    # 这一趟同时充当 `--jobs 2` 的分母（issue #347）：同样 4 条桩、同样包着解释器启动
    # 与线程池开销，只是没有并行——相除就只剩「并行省了多少」。
    # 分母自己得先站得住：桩要是没睡够（被替换成空实现、或机器把 sleep 吞了），后面那个
    # 比值就是个拿废基线做除数的数，两边一起变小反而更容易「通过」。
    if serial_elapsed < 4 * STUB_SLEEP * 0.9:
        failures.append(
            f'--jobs 1: {serial_elapsed:.2f}s for 4 x {STUB_SLEEP}s -- stubs did not sleep '
            f'(the parallel ratio below would divide by a bogus baseline)')
    notes.append(f'--jobs 1 {serial_elapsed:.2f}s (baseline)')

    # --- 2. --jobs 2 真重叠（同分片的两条也要并起来），结果表仍按清单顺序 ---
    _clear_traces(tree)
    code, out, elapsed = _run(tree, ['ci', '--jobs', '2'])
    pairs = _overlaps(_spans(tree))
    if code != 0:
        failures.append(f'--jobs 2: must pass, exit={code}\n{out}')
    if not pairs:
        failures.append('--jobs 2: no two gates overlapped -- parallelism is a no-op')
    if [name for _, name, _ in _summary_rows(out)] != order_expected:
        failures.append(f'--jobs 2: results not in manifest order {_summary_rows(out)}')
    # 墙钟只做粗筛，且**相对同一次运行的串行基线**判（issue #347）。重叠量由上面的
    # `pairs` 钉住，这里只回答「有没有省下接近一半的时间」。
    #
    # 别退回绝对常数：`4 × STUB_SLEEP × 0.9` 只算桩 sleep、不算进程启动，而 `_run()`
    # 记的是整次子进程墙钟（解释器启动 + 导入依赖 + 线程池调度 + 4 个子桩解释器起跑），
    # 本机固定开销约 0.5s → 落点 1.08-1.52s 正骑在 1.08s 那条线上，真并行也判红。
    # 也别退回「只要比串行快就算过」（issue #339 当时的放宽）：两条泳道退化成一条时，
    # `--jobs 2` 照样比 `--jobs 1` 快几十毫秒的调度抖动，那条线等于没有。
    # 两条证据怎么分工见 `_judge_parallel`。
    _, judged_notes, judged_failures = _judge_parallel(serial_elapsed, elapsed, pairs)
    notes.extend(judged_notes)
    failures.extend(judged_failures)
    notes.append(f'--jobs 2 {elapsed:.2f}s, overlapped {pairs}, '
                 f'{elapsed / serial_elapsed if serial_elapsed else 0:.2f}x of serial')

    # --- 3. EXCLUSIVE 独占收尾 ---
    # 桩门禁必须与 `EXCLUSIVE` 同名才会被摘出来，所以这条只能拿 `EXCLUSIVE` 里的真名
    # 当桩名。清单里因此出现两个同名条目（一个该进独占、一个该留在泳道），而桩文件是
    # 按文件名落盘的、两处共用同一份内容——轨迹标签分不出是谁跑的那一趟。于是：
    #
    # * 归属断言全部落在**结果表的行号**上（行号 = 清单位置，与 `[n/total]` 同一套编号）；
    # * 轨迹只用来算时间：独占那趟起跑时，所有泳道是否真的都已经收摊。
    exclusive = sorted(run_gates.EXCLUSIVE)
    # 必需成员用**独立字面量**判（issue #369）：下面这棵树是照着 `EXCLUSIVE` 搭的，集合一
    # 收窄树就跟着收窄、夹具永远不会错配——所以「非空」证明不了什么，必须换成「必需的
    # 那几个还在」。别把这条断言改回 `if not exclusive:`，那正是 #369 漏检的形状。
    failures.extend(_exclusive_membership_failures(exclusive))
    lanes = ['stub_lane_a', 'stub_lane_b']
    tree = _build_tree(
        base / 'exclusive',
        lanes + [(name, f'stub_dup_{index}') for index, name in enumerate(exclusive)])
    # 再把同名条目追加进清单**末尾**：`_build_tree` 只按文件名落盘一次，末尾这几条的
    # 文件已经被写过（就是上面那份桩），清单却多出几个位置——正是「同名不同位置」。
    manifest_path = tree / 'scripts' / 'gates.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['ci'] += [{'name': name + '.py', 'shard': 'only', 'why': 'fixture'}
                       for name in exclusive]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding='utf-8')
    rows_in_manifest = [row['name'][:-3] for row in manifest['ci']]
    total = len(rows_in_manifest)
    code, out, elapsed = _run(tree, ['ci', '--jobs', str(total)])
    spans = _spans(tree)
    if code != 0:
        failures.append(f'exclusive: must pass, exit={code}\n{out}')
    rows = _summary_rows(out)
    if [index for index, _, _ in rows] != list(range(1, total + 1)):
        failures.append(f'exclusive: result rows are not 1..{total}: {rows}')
    # 归属：前三行（两条泳道 + 同名条目的第一趟）不许出现独占名单里的名字——它们都是
    # 并发条目，哪怕其中一个跟独占同名；尾巴必须正好是清单末尾那几个独占位置。
    lead = total - len(exclusive)
    if [name for _, name, _ in rows[:lead]] != rows_in_manifest[:lead]:
        failures.append(f'exclusive: the lane rows drifted from the manifest {rows[:lead]}')
    if [name for _, name, _ in rows[lead:]] != rows_in_manifest[lead:]:
        failures.append(f'exclusive: the tail rows drifted from the manifest {rows[lead:]}')
    if any(name not in exclusive for name in rows_in_manifest[lead:]):
        failures.append(f'exclusive: the manifest tail is not EXCLUSIVE {rows_in_manifest[lead:]}')
    # 这条夹具本身就是「按名字摘独占」的回归钉子：`EXCLUSIVE` 里存的是短名，而清单
    # 这一行写的是 `check_listener_rehang.py`。拿原始 `row['name']` 去比会一条都摘不出来
    # （于是两趟同名都落进泳道、没有独占那趟），下面按收摊时刻切分必然红。
    # 计时：泳道必须真并起来（否则下面的「独占没跟谁重叠」什么都不说明），
    # 且同名条目要真的跑成两趟——一趟在泳道收摊前起跑，一趟在收摊后起跑。
    lane_spans = [span for span in spans if span[0].startswith('stub_lane')]
    if len(lane_spans) != len(lanes):
        failures.append(f'exclusive: lanes did not all run {[s[0] for s in spans]}')
    if not _overlaps(lane_spans):
        failures.append(f'exclusive: the lanes themselves did not overlap {lane_spans}')
    lane_end = max((end for _, _, end in lane_spans), default=None)
    dup_spans = sorted((span for span in spans if span[0].startswith('stub_dup_')),
                       key=lambda span: span[2])
    # 两趟同名怎么分：**最后一趟的起跑时刻**必须晚于泳道收摊——那才是「独占在泳道之后
    # 单独跑」。前一趟不能要求它跟泳道重叠：它是排队条目，`--jobs` 大于泳道数时不会立刻
    # 起跑，泳道先收一条它才上，起跑时刻甚至可能晚于泳道收摊。按「结束」切更不行——
    # 独占那趟结束得更晚，泳道那趟也可能在另一条泳道收摊之后才收尾，两者混成一堆。
    if len(dup_spans) != len(exclusive) * 2:
        failures.append(
            f'exclusive: the same-name rows should run twice each, got {dup_spans}')
    last = dup_spans[len(dup_spans) - len(exclusive):]
    first = dup_spans[:len(dup_spans) - len(exclusive)]
    if lane_end and any(start <= lane_end for _, start, _ in last):
        failures.append(
            f'exclusive: the EXCLUSIVE row(s) started before the lanes ended '
            f'{dup_spans} (lanes ended {lane_end})')
    for label, start, _end in last:
        for other, ostart, oend in lane_spans:
            if min(start, oend) > ostart and start < oend - OVERLAP_SLACK:
                failures.append(f'exclusive: {label} overlapped {other} -- EXCLUSIVE leaked')
    notes.append(
        f'exclusive {elapsed:.2f}s, {len(exclusive)} EXCLUSIVE row(s) ran solo as rows '
        f'{lead + 1}..{total} after {len(lane_spans)} lanes '
        f'({len(first)} same-name run(s) went with the lanes)')

    # --- 4. --fail-fast 只停「还没起的」，在飞的跑完 ---
    tree = _build_tree(base / 'failfast', ['fail_first', 'stub_slow', 'stub_next', 'stub_last'])
    code, out, _elapsed = _run(tree, ['ci', '--jobs', '2', '--fail-fast'])
    spans = _spans(tree)
    ran = {label for label, _, _ in spans}
    if code == 0:
        failures.append(f'--fail-fast: red gate did not fail the run\n{out}')
    if 'fail_first' not in ran:
        failures.append('--fail-fast: the failing gate did not run')
    queued = {'stub_next', 'stub_last'} - ran
    if not queued:
        failures.append('--fail-fast: queued gates all ran anyway -- fail-fast is a no-op')
    if 'stub_slow' in ran:
        duration = next((end - start for label, start, end in spans if label == 'stub_slow'), 0)
        if duration < STUB_SLEEP * 0.9:
            failures.append(
                f'--fail-fast: in-flight gate was cut short ({duration:.2f}s < {STUB_SLEEP}s)')
    notes.append(f'--fail-fast ran {sorted(ran)}, skipped {sorted(queued)}')

    # --- 5. --shard 子集上也认 --jobs ---
    tree = _build_tree(base / 'shard', ['stub_a', 'stub_b', 'stub_c', 'stub_d'])
    code, out, elapsed = _run(tree, ['ci', '--shard', 'only', '--jobs', '4'])
    if code != 0:
        failures.append(f'--shard --jobs: must pass, exit={code}\n{out}')
    if not _overlaps(_spans(tree)):
        failures.append('--shard --jobs 4: no overlap -- --jobs ignored on a subset')
    if [name for _, name, _ in _summary_rows(out)] != ['stub_a', 'stub_b', 'stub_c', 'stub_d']:
        failures.append(f'--shard --jobs: order drifted {_summary_rows(out)}')
    notes.append(f'--shard only --jobs 4 {elapsed:.2f}s')

    # --- 6. --jobs 钳到 CPU 数（issue #317 追评）---
    # 超配不报错、只是不生效；但「钳了几条」必须在**界面契约**上看得见：几核机、
    # 每条门禁一个子进程，二者差得越远，写 `--jobs 999` 的人越该知道实际跑了几条。
    tree = _build_tree(base / 'clamp', ['stub_a', 'stub_b'])
    code, banner, _elapsed = _run(tree, ['ci', '--jobs', '999'], console_only=True)
    effective = run_gates.effective_jobs(999)
    cores = os.cpu_count() or 0
    if code != 0:
        failures.append(f'--jobs 999: must still pass, exit={code}\n{banner}')
    if not (1 <= effective <= max(cores, 1)):
        failures.append(f'--jobs 999: effective {effective} is outside the machine ({cores} cores)')
    if effective != min(999, cores or 999):
        failures.append(f'--jobs 999: effective {effective} is not min(999, {cores} cores)')
    # 钉的是**片头那一行的实际生效值**，不是「输出里出现过 并行」——后者在
    # `--jobs 999` 报成 `并行 2` 时照样成立。
    if _banner_width(banner) != effective:
        failures.append(
            f'--jobs 999: the banner must report the effective width {effective}, got '
            f'{_banner_width(banner)!r} (banner line: {_banner_line(banner)!r})')
    # 对照组：不许把「钳制」说成「按你要的跑」。片头写 999 就是假话。
    if f'999' in (_banner_line(banner) or ''):
        failures.append(
            f'--jobs 999: banner echoed the requested width instead of the effective one: '
            f'{_banner_line(banner)!r}')
    # 对照组 2：串行档（默认）不许被钳制逻辑改成并行——`effective_jobs(1) == 1`。
    code, banner, _elapsed = _run(tree, ['ci', '--jobs', '1'], console_only=True)
    if code != 0:
        failures.append(f'--jobs 1: must still pass, exit={code}\n{banner}')
    if _banner_width(banner) != 1:
        failures.append(
            f'--jobs 1: serial must stay serial, banner said {_banner_line(banner)!r}')
    if run_gates.effective_jobs(0) != 0:
        failures.append(
            f'effective_jobs(0) = {run_gates.effective_jobs(0)} -- a non-positive width is '
            f'the caller\'s value, not something to clamp up')
    notes.append(
        f'--jobs 999 clamped to 并行 {effective} ({cores} cores); '
        f'--jobs 1 stayed 串行; effective_jobs(0)=0')

    # --- 7. 超时兜底：孙进程攥着 stdout 不拖等待，到点杀整棵进程树（issue #366）---
    # 形状与 issue 的复现命令一致：桩派生一个睡 300s 的孙进程后立刻退出，孙进程继承
    # 桩的 stdout。修复前 `_run()` 的外层 timeout 要等满孙进程寿命才拿到异常（本机
    # 实测 timeout=8 要 300s），修复后 runner 只等桩退出、输出照样回到调用方。
    # 特征串同时写进桩的文件名（= 桩自己的命令行）和孙进程 argv：`_marker_pids`
    # 因此覆盖「直接子进程 + 孙进程」两层，而不是只证明孙进程不在（复核 3）。
    marker = f'rg366-hold-{os.getpid()}-{time.time_ns()}'
    name = f'zombie_hold_{marker}'
    tree = _build_tree(base / 'hold', [name])
    (tree / 'scripts' / f'{name}.py').write_text(
        _HOLD_STUB.format(marker=marker), encoding='utf-8')
    try:
        code, console, elapsed = _run(tree, ['ci'], timeout=60, console_only=True)
        held = _marker_pids(marker)
        if code != 0:
            failures.append(f'held stdout: must pass, exit={code}\n{console}')
        if elapsed > HOLD_BUDGET:
            failures.append(
                f'held stdout: runner took {elapsed:.2f}s to return (> {HOLD_BUDGET}s) '
                f'-- it waited for the grandchild holding the pipe (#366)')
        # 「不再等 EOF」不许被实现成「丢输出」：桩打的那行必须还在控制台里。
        if 'HOLD_STUB_DONE' not in console:
            failures.append(f'held stdout: the gate output was dropped\n{console}')
        # 孙进程此刻必须还活着：否则这棵树根本没搭出「攥着 stdout」的形状，
        # 上面的耗时断言什么也证明不了（本机 venv 的 python.exe 是重定向器，
        # 一次 spawn 可能落成两个 OS 进程，所以只断言「至少一个」）。
        if not held:
            failures.append('held stdout: the stub left no live grandchild -- scenario not built')
        summary = (tree / 'step-summary.md').read_text(encoding='utf-8')
        if _summary_rows(summary) != [(1, name, '通过')]:
            failures.append(f'held stdout: result row drifted {_summary_rows(summary)}')
        notes.append(f'held stdout: returned in {elapsed:.2f}s while {len(held)} '
                     f'grandchild process(es) still held the handle')
    finally:
        _kill_pids(_marker_pids(marker))

    # 到点树杀：桩自己睡 300s，同时留一个睡 300s 的孙进程。`--timeout 2` 必须在数秒
    # 内返回、该条记失败，并且两个进程都不再存在——只杀直接子进程会留下孙进程。
    marker = f'rg366-kill-{os.getpid()}-{time.time_ns()}'
    name = f'zombie_slow_{marker}'
    tree = _build_tree(base / 'kill', [name])
    (tree / 'scripts' / f'{name}.py').write_text(
        _SLOW_STUB.format(marker=marker), encoding='utf-8')
    try:
        code, console, elapsed = _run(tree, ['ci', '--timeout', '2'], timeout=60,
                                       console_only=True)
        leftovers = _marker_pids(marker)
        # 超时记失败，但 runner 的总退出码与其它失败一致为 1（124 只是内部标记，
        # 见 `run_gates.TIMEOUT_RC`；复核 2 的文案/实现对齐）。
        if code != 1:
            failures.append(
                f'--timeout 2: runner must exit 1 like any other failure, got {code}\n{console}')
        if elapsed > TIMEOUT_BUDGET:
            failures.append(
                f'--timeout 2: runner took {elapsed:.2f}s to return (> {TIMEOUT_BUDGET}s)')
        if '超时' not in console:
            failures.append(f'--timeout 2: the timeout was not reported\n{console}')
        summary = (tree / 'step-summary.md').read_text(encoding='utf-8')
        if _summary_rows(summary) != [(1, name, '失败')]:
            failures.append(f'--timeout 2: result row drifted {_summary_rows(summary)}')
        if leftovers:
            failures.append(
                f'--timeout 2: {len(leftovers)} process(es) survived the tree kill -- the '
                f'stub itself or its grandchild is still alive (pids {leftovers})')
        notes.append(f'--timeout 2: failed after {elapsed:.2f}s, tree fully reaped')
    finally:
        _kill_pids(_marker_pids(marker))

    # 行尾字节：门禁子进程的 TextIOWrapper 在 Windows 上写出 `\r\n`，runner 必须归一
    # 后只落一次行尾；否则 CI 日志每条门禁输出后凭空多一条空记录（复核实测 `\r\r\n`）。
    tree = _build_tree(base / 'eol', ['eol_stub'])
    (tree / 'scripts' / 'eol_stub.py').write_text(_EOL_STUB, encoding='utf-8')
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env['GITHUB_STEP_SUMMARY'] = str(tree / 'step-summary.md')
    # 与 `_run` 同源：把本门禁用的 runner 拷进夹具树的 scripts/，清单才来自夹具树
    # （直接跑仓库里那份会读到真实 `gates.json`，那不是这组用例要的）。
    (tree / 'scripts' / 'run_gates.py').write_text(
        RUNNER.read_text(encoding='utf-8'), encoding='utf-8')
    raw = subprocess.run(
        [sys.executable, str(tree / 'scripts' / 'run_gates.py'), 'ci'],
        cwd=tree, env=env, capture_output=True, timeout=60).stdout
    sep = os.linesep.encode()
    if b'\r\r\n' in raw:
        failures.append("eol: runner double-translated CRLF (found b'\\r\\r\\n' in raw stdout)")
    for line in (b'EOL-1', b'EOL-2', b'EOL-3'):
        if raw.count(line) != 1:
            failures.append(f'eol: {line!r} appears {raw.count(line)} time(s) in raw stdout')
    if (b'EOL-1' + sep + b'EOL-2' + sep + b'EOL-3' + sep) not in raw:
        failures.append(f'eol: line endings drifted; head of raw stdout: {raw[:120]!r}')
    notes.append('eol: 3 gate lines intact, double-CRLF='
                 f'{raw.count(bytes([13, 13, 10]))}')
    return failures, notes


def _self_test():
    """A/B 对照：把一个必然交叠的树喂给 `_overlaps`，确认判据抓得住。"""
    failures = []
    with fx.temp_dir() as work:
        failures.extend(scan(work)[0])
    if failures:
        for line in failures:
            print(f'SELF-TEST FAIL {line}')
        return 1
    # 反例对照：判据必须真的能认出重叠，否则上面那些「零重叠」都是空话。
    overlapping = [('a', 0.0, 1.0), ('b', 0.5, 1.5), ('c', 2.0, 3.0)]
    if _overlaps(overlapping) != [('a', 'b')]:
        print(f'SELF-TEST FAIL overlap detection is blind: {_overlaps(overlapping)}')
        return 1
    touching = [('a', 0.0, 1.0), ('b', 1.0, 2.0)]
    if _overlaps(touching):
        print(f'SELF-TEST FAIL back-to-back runs counted as overlap: {_overlaps(touching)}')
        return 1
    return _self_test_timing()


# 墙钟判据的数值对照（issue #347）：不跑子进程，直接喂 `(串行, 并行)` 两组数。
# 分界在串行基线的一半再放三成；实测两侧：真并行 0.53-0.62x，机器满载 0.65-0.75x，
# 退化回串行 0.95-1.0x。判据必须在「真并行」与「退化」之间分开，且不把满载当退化。
_TIMING_CASES = [
    # (串行, 并行, 期望「算并行」)
    (4.00, 1.90, True),    # 4 x 0.30s 桩、真并行：理论半程 + 一点抖动
    (2.07, 1.19, True),    # 本机空载实测（issue #347 的失败行改成相对判据后应转绿）
    (3.92, 1.52, True),    # 本机 --jobs 1 基线那次
    (4.00, 2.80, False),   # 省了 30%：半程的 1.4 倍，越过 PARALLEL_SLACK
    (4.00, 3.80, False),   # 退化回串行：比值 0.95
    (4.00, 4.00, False),   # 完全没有并行
]

# 组合语义对照（issue #347 × #339）：`pairs` 与墙钟谁作证。**逐分支**覆盖，
# 每个「该红」都配一个「不该红」，否则改出个恒红/恒绿都能骗过自检。
# (串行, 并行, pairs, 期望通过, 说明)
_JUDGE_CASES = [
    (4.00, 1.90, [('a', 'b')], True, '真并行 + 抓到重叠'),
    (2.07, 1.19, [('a', 'b')], True, '本机空载实测点'),
    # 满载：比值 0.75 判不过墙钟，但两条泳道确实重合过 → 不许判红（#339 的现场）
    (2.36, 1.78, [('a', 'b')], True, '满载 0.75x，靠重叠作证'),
    (1.70, 1.11, [('a', 'b')], True, '#339 记录的满载点 0.65x'),
    (1.70, 1.22, [('a', 'b')], True, '#339 记录的满载点 0.72x'),
    # 没抓到重叠、墙钟也没省时间 → 必须红（这是墙钟唯一还能当证据的场合）
    (4.00, 3.80, [], False, '退化回串行且无重叠'),
    (4.18, 3.93, [], False, '满载且无重叠：两条证据都没有'),
    # 抓到重叠、但墙钟慢过串行基线 → 仍然红（不许让 pairs 吃掉墙钟的额度）
    (2.00, 2.00, [('a', 'b')], False, '重叠但一点也不比串行快'),
    (2.00, 2.40, [('a', 'b')], False, '重叠但比串行还慢'),
]


def _self_test_timing():
    """A/B 对照：判据对「真并行 / 退化回串行」两侧都给出正确结论，且组合语义逐分支对。

    末尾另跑两组**离线**对照（都不跑子进程）：`effective_jobs` 的钳制边界（#317）与
    `EXCLUSIVE` 的必需成员（#369）——后者就是要拦住「被测集合自己当期望值」的自指。
    """
    failures = []
    for serial, parallel, expected in _TIMING_CASES:
        got = _parallel_fast_enough(serial, parallel)
        if got != expected:
            failures.append(
                f'timing judge wrong for serial={serial}s parallel={parallel}s: '
                f'got {got}, expected {expected}')
    # 除零护栏：基线为 0 时不许当成「并行」放行。
    if _parallel_fast_enough(0.0, 0.001):
        failures.append('timing judge approved a run with a zero serial baseline')
    # 组合语义：pairs 与墙钟的分工。每条都单独断言，红在哪一支一眼可见。
    for serial, parallel, pairs, expected, label in _JUDGE_CASES:
        passed, _notes, _failures = _judge_parallel(serial, parallel, pairs)
        if passed != expected:
            failures.append(
                f'judge wrong for serial={serial}s parallel={parallel}s pairs={pairs or "none"} '
                f'({label}): got {passed}, expected {expected}')
    # 判据完整性自检：阈值一旦被写死成常数（就是 #347 那个 bug），这里必须跟着翻。
    # 两边结论相同时说明判据要么退化成常数、要么和常数同形——都不许算过。
    for serial, parallel in ((6.00, 1.52), (3.20, 1.40)):
        if _parallel_fast_enough(serial, parallel) == (
                parallel <= 4 * STUB_SLEEP * 0.9):
            failures.append(
                f'timing judge degenerates to the absolute constant at '
                f'serial={serial}s parallel={parallel}s -- #347 regression')
    # 组合语义不许退化成「只看 one 条证据」：把 pairs 恒为空 / 恒非空各试一遍，
    # 结论必须随之一分为二，否则说明函数根本没在用 pairs（或根本没在用墙钟）。
    with_pairs = _judge_parallel(2.36, 1.78, [('a', 'b')])[0]
    without_pairs = _judge_parallel(2.36, 1.78, [])[0]
    if with_pairs == without_pairs:
        failures.append(
            'judge ignores pairs: same verdict with and without overlap '
            f'({with_pairs}) -- it is reading only one of the two signals')
    slow_with_pairs = _judge_parallel(2.00, 2.40, [('a', 'b')])[0]
    if slow_with_pairs:
        failures.append(
            'judge ignores wall clock: a run slower than the serial baseline passed '
            'just because two gates overlapped -- #339-style blanket relaxation is back')
    # 钳制判据与墙钟判据各管一路，两边都跑完再统一报——中间提前 return 会让
    # 「墙钟坏了」把钳制的问题一起盖住，得改两次才发现第二处。必需成员断言同理（#369）。
    failures.extend(_self_test_clamp())
    failures.extend(_self_test_exclusive_membership())
    if failures:
        for line in failures:
            print(f'SELF-TEST FAIL {line}')
        return 1
    print('SELF-TEST OK runner parallelism A/B fixtures behaved as designed')
    return 0


def _self_test_clamp():
    """`effective_jobs` 的数值对照（issue #317 追评）：钳制只许作用在既有的显式开关上。

    三条边界各配一个反例，免得改出个「恒返回 cpu_count」（会把 `--jobs 1` 擅自变成
    并行）或「恒返回 requested」（钳制名存实亡）都能骗过自检。
    """
    failures = []
    cores = os.cpu_count() or 1
    cases = [
        (1, 1),                     # 默认/显式串行：原样，绝不放大
        (0, 0),                     # 非法值也原样返回，由 main() 报错，这里不替它兜
        (999, min(999, cores)),     # 超配：钳到核数
        (2, min(2, cores)),         # 正常并行档
    ]
    for requested, expected in cases:
        got = run_gates.effective_jobs(requested)
        if got != expected:
            failures.append(
                f'effective_jobs({requested}) = {got}, expected {expected} (machine has {cores})')
    # 判据完整性：钳制必须真的依赖核数——改成恒返回 requested 时，超配那一条要翻。
    if run_gates.effective_jobs(10 ** 6) - run_gates.effective_jobs(2) == 10 ** 6 - 2:
        failures.append('effective_jobs ignores the core count: clamping is a no-op')
    # 也不许反过来把串行放大：恒返回 cpu_count 的实现会在这里翻红。
    if run_gates.effective_jobs(1) != 1:
        failures.append(
            f'effective_jobs(1) = {run_gates.effective_jobs(1)}: --jobs 1 must stay serial')
    return failures


def _self_test_exclusive_membership():
    """必需成员断言的 A/B 对照（issue #369）：完整集合必须过，缺一个必须翻红。

    反例**逐成员**做，不只清空整个集合：只拿「空集全红」当对照的话，一个「只认
    `check_listener_rehang`、对另一个成员失明」的半成品判据照样能骗过自检——那正是
    #353 之后的现状（集合里剩下的那个还在，缺的那个没人查）。清空那一枪另打一次，钉住
    「退回 `if not exclusive`」这种改法：非空不再等于通过。
    """
    failures = []
    if _exclusive_membership_failures(REQUIRED_EXCLUSIVE):
        failures.append(
            f'membership judge rejects the required set itself: '
            f'{_exclusive_membership_failures(REQUIRED_EXCLUSIVE)}')
    for member in sorted(REQUIRED_EXCLUSIVE):
        narrowed = REQUIRED_EXCLUSIVE - {member}
        if not _exclusive_membership_failures(narrowed):
            failures.append(
                f'membership judge is blind: dropping {member} (leaving {sorted(narrowed)}) '
                f'still passed -- it cannot catch the #369 silent narrowing')
    if not _exclusive_membership_failures(frozenset()):
        failures.append(
            'membership judge passes an empty EXCLUSIVE -- non-empty is not the contract')
    return failures


def run(root):
    failures, notes = scan(root)
    for line in notes:
        print(f'NOTE {line}')
    for line in failures:
        print(f'FAIL {line}')
    if failures:
        return 1
    print(f'OK runner parallelism: {len(notes)} scenarios behaved as designed '
          f'(EXCLUSIVE={sorted(run_gates.EXCLUSIVE)})')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--self-test', action='store_true',
                        help='只在夹具树上跑一遍，并做重叠判据的 A/B 对照')
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test()
    with fx.temp_dir() as work:
        return run(work)


if __name__ == '__main__':
    sys.exit(main())
