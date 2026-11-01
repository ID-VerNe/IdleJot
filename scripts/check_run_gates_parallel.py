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
   re-sharding the manifest must not be able to open it up.

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
   keeps the same semantics as the local full run.

Wall-clock is a *coarse* sieve and it is judged **relative to the serial baseline
measured in the same run** (issue #347): ``_run()`` times a whole subprocess, so
the absolute constant ``4 x STUB_SLEEP x 0.9`` used to leave interpreter start-up
and thread-pool dispatch out of the budget and turned correct parallel runs red on
this machine (1.08-1.52s against a 1.08s line). Overlap itself is still pinned by
``pairs``; see ``_parallel_fast_enough`` for what the ratio does *not* catch.

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
# 墙钟判据的松紧（issue #347）。两条泳道跑 4 条等长桩，理论时长正好是串行的一半；
# 运行时再打三成余量给负载抖动——越过这条线就不是「波动」了，是「没并行」。
PARALLEL_SLACK = 1.30

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


def _run(root, args, timeout=180):
    """以子进程跑真 `run_gates.py`，返回 `(退出码, 结果表原文, 秒数)`。

    结果表**只写 `$GITHUB_STEP_SUMMARY`**，本机 stdout 上只有 `[n/total] …` 行
    （那是跑完瞬间打的，并行下完成先后天然乱序）。所以这里给每次运行塞一个
    临时 summary 文件、跑完读回来——断言的就是 CI 真正看到的那个契约面。
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
    return proc.returncode, table or ('NO SUMMARY\n' + console), elapsed


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
    # 墙钟只做粗筛，且**相对同一次运行的串行基线**判（issue #347）：绝对常数
    # `4 × STUB_SLEEP × 0.9` 没算进程启动开销，本机稳定误报「not parallel」。
    # 真正的重叠量由上面的 pairs 钉住。
    if not _parallel_fast_enough(serial_elapsed, elapsed):
        failures.append(
            f'--jobs 2: {elapsed:.2f}s vs serial baseline {serial_elapsed:.2f}s -- not parallel '
            f'(expected <= {serial_elapsed / 2 * PARALLEL_SLACK:.2f}s)')
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
    if not exclusive:
        failures.append('EXCLUSIVE is empty -- the listener gate lost its protection')
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
# 本机实测串行基线 6.00s / 3.92s、并行 1.52s，即比值 0.25–0.39；「根本没并行」的
# 比值是 1.0 上下。判据必须把这两侧分开，且分界不能贴着实测的波动带。
_TIMING_CASES = [
    # (串行, 并行, 期望「算并行」)
    (4.00, 1.90, True),    # 4 x 0.30s 桩、真并行：理论半程 + 一点抖动
    (6.00, 1.52, True),    # 本机实测（issue #347 的失败行改成相对判据后应转绿）
    (3.92, 1.52, True),    # 本机 --jobs 1 基线那次
    (4.00, 3.80, False),   # 退化回串行：比值 0.95
    (4.00, 4.00, False),   # 完全没有并行
    (4.00, 2.80, False),   # 省了 30%：半程的 1.4 倍，越过 PARALLEL_SLACK
]


def _self_test_timing():
    """A/B 对照：判据对「真并行 / 退化回串行」两侧都给出正确结论。"""
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
    # 判据完整性自检：阈值一旦被写死成常数（就是 #347 那个 bug），这里必须跟着翻。
    # 两边结论相同时说明判据要么退化成常数、要么和常数同形——都不许算过。
    for serial, parallel in ((6.00, 1.52), (3.20, 1.40)):
        if _parallel_fast_enough(serial, parallel) == (
                parallel <= 4 * STUB_SLEEP * 0.9):
            failures.append(
                f'timing judge degenerates to the absolute constant at '
                f'serial={serial}s parallel={parallel}s -- #347 regression')
    if failures:
        for line in failures:
            print(f'SELF-TEST FAIL {line}')
        return 1
    print('SELF-TEST OK runner parallelism A/B fixtures behaved as designed')
    return 0


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
