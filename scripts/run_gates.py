"""按 `scripts/gates.json` 的 ci 顺序执行门禁（issue #59）。

CI 与本地同一个入口：`.github/workflows/ci.yml` 只调用本脚本，不再手写门禁数组；
清单漂移由 `scripts/check_gate_manifest.py` 钉住。顺序、单门禁参数（如
`check_publication.py --history`）都在清单里，不在 YAML 里。

    uv run python scripts/run_gates.py ci       # 全部 ci 门禁，失败即停
    uv run python scripts/run_gates.py --list   # 档位计数与 ci 顺序

用 `sys.executable` 拉起子门禁：外层已由 `uv run --frozen` 进入锁定环境，
逐个再起 uv 只是重复解析同一份环境。
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'scripts' / 'gates.json'
BUCKETS = ('ci', 'manual', 'stale', 'dead', 'support')


def load_manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def counts(manifest):
    return '、'.join(f'{name} {len(manifest.get(name) or [])}' for name in BUCKETS)


def list_gates(manifest):
    print(f'清单：scripts/gates.json（{counts(manifest)}）')
    for index, row in enumerate(manifest['ci'], 1):
        args = ''.join(' ' + arg for arg in row.get('args') or [])
        print(f'{index:>3}. {row["name"]}{args} — {row["why"]}')
    return 0


def run_ci(manifest):
    gates = manifest['ci']
    total = len(gates)
    print(f'ci 门禁 {total} 个，顺序来自 scripts/gates.json')
    failures = []
    started = time.perf_counter()
    for index, row in enumerate(gates, 1):
        name = row['name']
        command = [sys.executable, str(ROOT / 'scripts' / name), *(row.get('args') or [])]
        tick = time.perf_counter()
        result = subprocess.run(command, cwd=ROOT)
        elapsed = time.perf_counter() - tick
        if result.returncode != 0:
            print(f'[{index}/{total}] {name} FAILED ({elapsed:.1f}s)')
            failures.append(name)
            break
        print(f'[{index}/{total}] {name} ok ({elapsed:.1f}s)')
    total_elapsed = time.perf_counter() - started
    if failures:
        print(f'FAIL: {failures[0]} 未通过；后续门禁未执行（fail-fast，见 issue #60）')
        return 1
    print(f'PASS: {total} 个 ci 门禁全部通过（{total_elapsed:.1f}s）')
    return 0


def main():
    parser = argparse.ArgumentParser(description='按清单执行门禁（issue #59）')
    parser.add_argument('group', nargs='?', choices=['ci'], help='要执行的档位（目前只有 ci）')
    parser.add_argument('--list', action='store_true', help='打印档位计数与 ci 顺序')
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
    return run_ci(manifest)


if __name__ == '__main__':
    sys.exit(main())
