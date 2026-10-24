"""issue #62 gate: 门禁链改动必须在 PR 正文的 `## 门禁改动` 段逐条交代。

背景：门禁是这套协作里唯一的第三方校验，但被测方可以在同一个 PR 里改门禁脚本，
删弱断言后 CI 只看到绿色。本门禁在 PR 事件上读 diff 与正文：

1. 改动集 = `git diff --name-only <base>...HEAD`，只取门禁执行链：
   `scripts/check_*.py`、`scripts/gates.json`、`scripts/run_gates.py`、
   `.github/workflows/ci.yml`——改清单、执行器或 workflow 与改断言等价；
2. 命中时 PR 正文必须含 `## 门禁改动` 段，且每个被改路径都出现在该段内
   （段外提到不算）；
3. 没有 PR 上下文（本机 parity / `push` 事件没有 base）时打印 SKIP 并通过；
   base 非法、diff 失败等无法判断的情况 fail-closed。

    uv run python scripts/check_gate_change_review.py --base origin/slim --body-file pr.md

CI 由 `.github/workflows/ci.yml` 注入 `PR_BASE_SHA` / `PR_BODY`（仅 pull_request 有值）。
"""
import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECTION_NAME = '门禁改动'
PATTERNS = (
    'scripts/check_*.py',
    'scripts/gates.json',
    'scripts/run_gates.py',
    '.github/workflows/ci.yml',
)
HEADING = re.compile(r'^##[ \t]+门禁改动[ \t\r]*$', re.MULTILINE)
NEXT_HEADING = re.compile(r'^##[ \t]+\S', re.MULTILINE)


def changed_files(base):
    """`base...HEAD`（三点，按 merge-base）改动的文件列表；git 失败时抛 RuntimeError。"""
    result = subprocess.run(
        ['git', 'diff', '--name-only', f'{base}...HEAD'],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding='utf-8',
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f'git diff 退出码 {result.returncode}')
    return [line.strip().replace('\\', '/') for line in result.stdout.splitlines() if line.strip()]


def gate_chain(files):
    """只保留门禁执行链上的文件（顺序按 diff 输出，去重）。"""
    hit = []
    for path in files:
        if any(fnmatch.fnmatch(path, pattern) for pattern in PATTERNS) and path not in hit:
            hit.append(path)
    return hit


def gate_section(body):
    """取 `## 门禁改动` 段正文；没有该段时返回 None。"""
    match = HEADING.search(body)
    if not match:
        return None
    rest = body[match.end():]
    nxt = NEXT_HEADING.search(rest)
    return rest[:nxt.start()] if nxt else rest


def read_body_text(path):
    return Path(path).read_text(encoding='utf-8', errors='replace')


def body_from_args(args):
    """正文优先级：--body-file > PR_BODY_FILE > PR_BODY；都没有时返回 None。"""
    if args.body_file:
        return read_body_text(args.body_file)
    path = os.environ.get('PR_BODY_FILE')
    if path:
        return read_body_text(path)
    return os.environ.get('PR_BODY')


def main():
    parser = argparse.ArgumentParser(description='门禁链改动必须在 PR 正文交代（issue #62）')
    parser.add_argument('--base', help='PR 的 base ref（默认读环境变量 PR_BASE_SHA）')
    parser.add_argument('--body-file', help='PR 正文文件（默认读 PR_BODY_FILE / PR_BODY）')
    args = parser.parse_args()

    base = args.base or os.environ.get('PR_BASE_SHA') or ''
    if not base:
        print('  SKIP 没有 PR base（本机直跑 / push 事件）：门禁改动段只在 PR 上校验')
        print('PASS: 非 PR 上下文，跳过门禁改动段校验。')
        return 0

    try:
        touched = gate_chain(changed_files(base))
    except RuntimeError as error:
        print(f'  FAIL 无法计算 {base}...HEAD 的改动集：{error}')
        print('FAIL: 门禁改动段校验无法完成（fail-closed）。')
        return 1

    body = body_from_args(args)
    section = gate_section(body) if body is not None else None

    if not touched:
        if section is not None:
            print('  ok  本次未触及门禁链；正文带 `## 门禁改动` 段（允许）')
        else:
            print('  ok  本次未触及门禁链，不需要 `## 门禁改动` 段')
        print('PASS: 门禁链无改动。')
        return 0

    if body is None:
        print(f'  FAIL 改动触及门禁链（{len(touched)} 个文件），但拿不到 PR 正文')
        print('     CI 需注入 PR_BODY / PR_BODY_FILE，本机用 --body-file')
        for path in touched:
            print(f'     需要交代：{path}')
        print('FAIL: 门禁改动段缺失（正文不可得）。')
        return 1

    if section is None:
        print(f'  FAIL 改动触及门禁链（{len(touched)} 个文件），但正文没有 `## 门禁改动` 段')
        for path in touched:
            print(f'     需要交代：{path}')
        print('FAIL: 门禁改动段缺失。')
        return 1

    normalized = section.replace('\\', '/').lower()
    missing = [path for path in touched if path.lower() not in normalized]
    if missing:
        print(f'  FAIL `## 门禁改动` 段漏列 {len(missing)} / {len(touched)} 个被改文件')
        for path in missing:
            print(f'     漏列：{path}')
        print('FAIL: 门禁改动段没有逐个交代。')
        return 1

    for path in touched:
        print(f'  ok  已交代：{path}')
    print(f'PASS: {len(touched)} 个门禁链改动都在 `## 门禁改动` 段内。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
