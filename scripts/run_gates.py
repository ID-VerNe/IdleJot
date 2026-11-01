"""按 `scripts/gates.json` 的清单执行门禁（issue #59 / #60 / #317）。

CI 与本地同一个入口：`.github/workflows/ci.yml` 只调用本脚本，不再手写门禁数组；
清单漂移由 `scripts/check_gate_manifest.py` 钉住。顺序、单门禁参数（如
`check_publication.py --history`）、每条 ci 门禁所属的 CI 分片（`shard`）都在清单里。

    uv run python scripts/run_gates.py ci                  # 全部 ci 门禁，按清单顺序（默认串行）
    uv run python scripts/run_gates.py ci --shard static   # 只跑一个分片（CI matrix 用）
    uv run python scripts/run_gates.py ci --fail-fast      # 本机快速试错：见到红就停
    uv run python scripts/run_gates.py ci --jobs 4         # 本机并行：最多 4 条门禁同时跑
    uv run python scripts/run_gates.py --list              # 档位计数、分片与 ci 顺序

**默认串行，`--jobs` 才并行**（issue #317）。串行是 `AGENTS.md` §2.6「安静窗口 solo
执行」的显式选择，不是能力上限：红门禁归因要用 pristine HEAD 对照判「既有环境问题
还是这次改动引入」，并发下两条门禁互相干扰会让这个判断失真。所以纪律从「永远串行」
收窄成「**归因时串行**」——`--jobs N` 是显式开关，默认值不翻转。

并行档的语义（见 `plan_parallel` / `run_parallel`）：

- **并发单位是一条门禁，不是 CI 分片**。分片是给多机 matrix 摊时间用的：`gates.json`
  里一个分片 11–30 条，本机单进程按分片并发等于串行（`--jobs 10` 跑 `reply` 分片
  实测 162s vs 串行 156s）。CI 的「同片串行、片间并行」只属于 CI matrix，本机不照搬；
- **`EXCLUSIVE` 单独收尾**：会起真实本树进程、靠全机 WMI 认进程的门禁（目前只有
  `check_listener_rehang`）在所有并发门禁跑完之后独占跑，不与任何门禁重叠；
- **结果始终按清单顺序返回**，并行只改完成先后，不改结果表形状（`summary_text` 与
  `GITHUB_STEP_SUMMARY` 都依赖它）；`[n/总]` 的序号也按清单位置编，不按完成先后；
- `--fail-fast` 在并行下只阻止**后续门禁启动**，已在跑的门禁跑完——中途杀掉子门禁会
  留下半写的临时目录，让下一次归因更难。

片内默认全跑并汇总（issue #60：一次红灯要看全貌），`--fail-fast` 才提前收手。
环境里有 `GITHUB_STEP_SUMMARY` 时把逐条结果写进 job 摘要，本机没有该变量时跳过。

用 `sys.executable` 拉起子门禁：外层已由 `uv run --frozen` 进入锁定环境，
逐个再起 uv 只是重复解析同一份环境。
"""
import argparse
import collections
import concurrent.futures
import json
import os
import subprocess
import sys
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

# 并行档里必须独占跑的门禁（issue #317）。它们会起**真实的**本树 `listen.py` 子进程，
# 再靠全机 WMI（`Get-CimInstance Win32_Process`）按命令行认出它、最后 `taskkill /T /F`
# 收树（`check_listener_rehang/budget.py:640-667`、`ownership.py:303-345`、
# `water-group/daemon/wg_up_probe.py:99-138`）。WMI 查询是全机的，另一条门禁同时起/杀
# 本树进程会让它的「前提不成立」断言假红。认名不认档：清单里改个 shard 不该悄悄放开。
EXCLUSIVE = frozenset({'check_listener_rehang'})


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


def run_one(row, printed, total):
    """跑一条门禁，返回 `(结果, 秒数)`；串行与并行共用这一份。

    `printed` 是这条门禁在本次执行里的序号，**按清单顺序**编，不按完成先后——
    并行下两条同时收尾也不会打出两个相同的 `[3/92]`。

    子进程走 `winconsole.run_passthrough`：门禁全部是控制台程序，这个脚本又常常没有
    控制台，裸 `subprocess.run` 会给每条门禁新开一个窗口（issue #353 用窗口钩子在真实
    门禁链上抓到 `git.exe` 标题的控制台窗口）。

    为什么不是 `run_hidden`：那个函数把**窗口**关掉，代价是子进程拿到的是一扇新的、
    `CREATE_NO_WINDOW` 控制台，「继承我的 stdout」就指向那扇窗而不是本进程的管道——
    调用方没有控制台时逐条门禁的输出全部丢掉（issue #91 的原形）。门禁的输出正是给人
    看的（`[n/94] … 通过`、失败时的 traceback），所以这里要的是「关窗 + 直通」，
    `run_passthrough` 就是为此存在的：两路合流进管道、UTF-8 解码、整块写回本进程 stdout。
    """
    tick = time.perf_counter()
    # 没有 helper 的夹具树里退回裸调用：那里没有运行时树，也就没有窗口问题的前提。
    if winconsole is None:
        result = subprocess.run(command_of(row), cwd=ROOT)
        code = result.returncode
    else:
        code = winconsole.run_passthrough(command_of(row), cwd=ROOT)
    elapsed = time.perf_counter() - tick
    status = PASSED if code == 0 else FAILED
    print(f'[{printed}/{total}] {row["name"]} {status} ({elapsed:.1f}s)', flush=True)
    return status, elapsed


def run_serial(rows, fail_fast):
    """逐条串行跑；返回 `{清单位置: (结果, 秒数)}` 与是否被 `--fail-fast` 截断。"""
    outcome, cut = {}, False
    total = len(rows)
    for position, row in enumerate(rows):
        outcome[position] = run_one(row, position + 1, total)
        if outcome[position][0] == FAILED and fail_fast:
            cut = True
            print(f'  后续 {total - position - 1} 条未执行（--fail-fast）')
            break
    return outcome, cut


def run_parallel(rows, fail_fast, jobs):
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
                    running[pool.submit(run_one, row, position + 1, total)] = position
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
        outcome[position] = run_one(row, position + 1, total)
        if fail_fast and outcome[position][0] == FAILED:
            cut = True

    if cut and len(outcome) < total:
        print(f'  余下 {total - len(outcome)} 条未执行（--fail-fast）')
    return outcome, cut


def run_rows(rows, fail_fast, jobs=1):
    """执行入口：`jobs <= 1` 走串行（与改动前同形），否则按门禁并发。

    返回 `([(row, 结果, 秒数)], 总秒数)`，条目顺序**始终是清单顺序**——并行只改
    完成先后，不改结果表形状（`summary_text` 与 `GITHUB_STEP_SUMMARY` 都依赖它）。
    """
    total = len(rows)
    started = time.perf_counter()
    if jobs <= 1:
        outcome, _ = run_serial(rows, fail_fast)
    else:
        outcome, _ = run_parallel(rows, fail_fast, jobs)
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


def run_ci(manifest, shard=None, fail_fast=False, jobs=1):
    rows = shard_rows(manifest, shard)
    label = shard or '全部分片'
    mode = '串行' if jobs <= 1 else f'并行 {jobs}'
    print(f'门禁 {len(rows)} 条，分片 {label}，{mode}，顺序来自 scripts/gates.json')
    results, elapsed = run_rows(rows, fail_fast, jobs)
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
                        help='最多 N 条门禁同时跑（默认 1 = 串行；归因红门禁时保持 1）')
    args = parser.parse_args()
    if args.jobs < 1:
        print(f'FAIL: --jobs 必须 >= 1，收到 {args.jobs}')
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
    return run_ci(manifest, args.shard, args.fail_fast, args.jobs)


if __name__ == '__main__':
    sys.exit(main())
