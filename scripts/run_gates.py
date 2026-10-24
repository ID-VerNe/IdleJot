"""按 `scripts/gates.json` 的清单执行门禁（issue #59 / #60）。

CI 与本地同一个入口：`.github/workflows/ci.yml` 只调用本脚本，不再手写门禁数组；
清单漂移由 `scripts/check_gate_manifest.py` 钉住。顺序、单门禁参数（如
`check_publication.py --history`）、每条 ci 门禁所属的 CI 分片（`shard`）都在清单里。

    uv run python scripts/run_gates.py ci                  # 全部 ci 门禁，按清单顺序
    uv run python scripts/run_gates.py ci --shard static   # 只跑一个分片（CI matrix 用）
    uv run python scripts/run_gates.py ci --fail-fast      # 本机快速试错：见到红就停
    uv run python scripts/run_gates.py --list              # 档位计数、分片与 ci 顺序

片内默认全跑并汇总（issue #60：一次红灯要看全貌），`--fail-fast` 才提前收手；无论哪种，
片内始终串行，保持 `AGENTS.md` §2.6 的「安静窗口 solo 执行」纪律。
环境里有 `GITHUB_STEP_SUMMARY` 时把逐条结果写进 job 摘要，本机没有该变量时跳过。

用 `sys.executable` 拉起子门禁：外层已由 `uv run --frozen` 进入锁定环境，
逐个再起 uv 只是重复解析同一份环境。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'scripts' / 'gates.json'
BUCKETS = ('ci', 'manual', 'stale', 'dead', 'support')
PASSED, FAILED, SKIPPED = '通过', '失败', '未执行'


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
    return [sys.executable, str(ROOT / 'scripts' / row['name']), *(row.get('args') or [])]


def run_rows(rows, fail_fast):
    """片内逐条串行跑；返回 ([(row, 结果, 秒数)], 总秒数)。"""
    results = []
    total = len(rows)
    started = time.perf_counter()
    for index, row in enumerate(rows, 1):
        tick = time.perf_counter()
        result = subprocess.run(command_of(row), cwd=ROOT)
        elapsed = time.perf_counter() - tick
        status = PASSED if result.returncode == 0 else FAILED
        results.append((row, status, elapsed))
        print(f'[{index}/{total}] {row["name"]} {status} ({elapsed:.1f}s)')
        if status == FAILED and fail_fast:
            results.extend((rest, SKIPPED, 0.0) for rest in rows[index:])
            print(f'  后续 {total - index} 条未执行（--fail-fast）')
            break
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


def run_ci(manifest, shard=None, fail_fast=False):
    rows = shard_rows(manifest, shard)
    label = shard or '全部分片'
    print(f'门禁 {len(rows)} 条，分片 {label}，顺序来自 scripts/gates.json')
    results, elapsed = run_rows(rows, fail_fast)
    write_summary(results, shard or 'all', elapsed)
    failed = [row['name'] for row, status, _ in results if status == FAILED]
    if failed:
        print(f'FAIL: {len(failed)} 条未通过：' + '、'.join(failed))
        return 1
    print(f'PASS: {len(results)} 条门禁全部通过（{elapsed:.1f}s）')
    return 0


def main():
    parser = argparse.ArgumentParser(description='按清单执行门禁（issue #59 / #60）')
    parser.add_argument('group', nargs='?', choices=['ci'], help='要执行的档位（目前只有 ci）')
    parser.add_argument('--shard', help='只跑某个 CI 分片（名字来自 scripts/gates.json 的 shards）')
    parser.add_argument('--fail-fast', action='store_true', help='见到第一个失败就停（默认片内全跑）')
    parser.add_argument('--list', action='store_true', help='打印档位计数、分片与 ci 顺序')
    args = parser.parse_args()
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
    return run_ci(manifest, args.shard, args.fail_fast)


if __name__ == '__main__':
    sys.exit(main())
