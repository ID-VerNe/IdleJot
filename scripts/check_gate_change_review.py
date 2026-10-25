"""issue #62 gate: 门禁链改动必须在 PR 正文的 `## 门禁改动` 段逐条交代。

背景：门禁是这套协作里唯一的第三方校验，但被测方可以在同一个 PR 里改门禁脚本，
删弱断言后 CI 只看到绿色。本门禁在 PR 事件上读 diff 与正文：

1. 改动集 = `git diff --name-only <base>...HEAD`，只取门禁执行链：
   `scripts/check_*.py`、`scripts/gates.json`、`scripts/run_gates.py`、
   `.github/workflows/ci.yml`，外加 `.githooks/` 下全部钩子（issue #107：
   pre-push / commit-msg 是门禁执行入口，改钩子与改断言等价；披露链自此与
   `check_repo_settings.py` 的 `gate_paths()` 同口径）——改清单、执行器、
   workflow 或钩子与改断言等价；
2. 命中时 PR 正文必须含 `## 门禁改动` 段，且每个被改路径都出现在该段内
   （段外提到不算）；
3. 没有 PR 上下文（本机 parity / `push` 事件没有 base）时打印 SKIP 并通过；
   base 非法、diff 失败等无法判断的情况 fail-closed。

    uv run python scripts/check_gate_change_review.py --base origin/slim --body-file pr.md

CI 由 `.github/workflows/ci.yml` 注入 `PR_BASE_SHA` / `PR_BODY`（仅 pull_request 有值）。

入口处先跑 `self_test()`（issue #107 的离线夹具：PATTERNS 命中范围 + `check()`
判定的回归，不碰 git / 网络 / 文件），把「钩子改动必须披露」钉在每次执行里。
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
    '.githooks/*',  # issue #107：钩子是门禁执行入口；fnmatch 的 * 跨 /，嵌套路径一并覆盖
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


def check(touched, body):
    """核心判定：门禁链命中文件 + PR 正文 → (退出码, 输出行)。

    body 为 None 表示正文不可得（与空串区分：空串是「有正文、没有段」）。
    纯函数，供 main() 与 self_test() 共用（issue #107）。
    """
    lines = []
    section = gate_section(body) if body is not None else None

    if not touched:
        if section is not None:
            lines.append('  ok  本次未触及门禁链；正文带 `## 门禁改动` 段（允许）')
        else:
            lines.append('  ok  本次未触及门禁链，不需要 `## 门禁改动` 段')
        return 0, lines + ['PASS: 门禁链无改动。']

    if body is None:
        lines.append(f'  FAIL 改动触及门禁链（{len(touched)} 个文件），但拿不到 PR 正文')
        lines.append('     CI 需注入 PR_BODY / PR_BODY_FILE，本机用 --body-file')
        for path in touched:
            lines.append(f'     需要交代：{path}')
        return 1, lines + ['FAIL: 门禁改动段缺失（正文不可得）。']

    if section is None:
        lines.append(f'  FAIL 改动触及门禁链（{len(touched)} 个文件），但正文没有 `## 门禁改动` 段')
        for path in touched:
            lines.append(f'     需要交代：{path}')
        return 1, lines + ['FAIL: 门禁改动段缺失。']

    normalized = section.replace('\\', '/').lower()
    missing = [path for path in touched if path.lower() not in normalized]
    if missing:
        lines.append(f'  FAIL `## 门禁改动` 段漏列 {len(missing)} / {len(touched)} 个被改文件')
        for path in missing:
            lines.append(f'     漏列：{path}')
        return 1, lines + ['FAIL: 门禁改动段没有逐个交代。']

    for path in touched:
        lines.append(f'  ok  已交代：{path}')
    return 0, lines + [f'PASS: {len(touched)} 个门禁链改动都在 `## 门禁改动` 段内。']


def self_test():
    """入口处离线自检（issue #107）：PATTERNS 命中范围 + check() 判定的回归夹具。

    不碰 git / 网络 / 文件，全部用内存里的合成输入。夹具成对设计：每条
    「坏形态必须红」都配同形「好形态必须绿」（#106：排除型断言没有对照组
    就是假绿），并配旧四类对照，证明扩容没有挤掉原行为。
    """
    failures = []

    def expect(name, got, want):
        if got != want:
            failures.append(f'{name}：期望 {want!r}，实得 {got!r}')

    def expect_rc(name, touched, body, want_rc, signature=None):
        code, out = check(touched, body)
        expect(f'{name} rc', code, want_rc)
        if signature is not None and not any(signature in line for line in out):
            failures.append(f'{name}：输出里没有签名「{signature}」（实得 {out}）')

    # gate_chain()：PATTERNS 命中范围，保序去重
    expect('gate_chain 单个钩子', gate_chain(['.githooks/pre-push']),
           ['.githooks/pre-push'])
    expect('gate_chain 两个钩子',
           gate_chain(['.githooks/commit-msg', '.githooks/pre-push']),
           ['.githooks/commit-msg', '.githooks/pre-push'])
    expect('gate_chain 嵌套路径', gate_chain(['.githooks/nested/deep/hook']),
           ['.githooks/nested/deep/hook'])
    expect('gate_chain 旧四类不回归',
           gate_chain(['scripts/check_x.py', 'scripts/gates.json',
                       'scripts/run_gates.py', '.github/workflows/ci.yml']),
           ['scripts/check_x.py', 'scripts/gates.json',
            'scripts/run_gates.py', '.github/workflows/ci.yml'])
    expect('gate_chain 非门禁文件不命中', gate_chain(['README.md', 'doc/x.md']), [])
    expect('gate_chain 混合保序去重',
           gate_chain(['README.md', '.githooks/pre-push', 'scripts/check_a.py',
                       '.githooks/pre-push']),
           ['.githooks/pre-push', 'scripts/check_a.py'])

    section_ok = ('## 门禁改动\n\n- `.githooks/pre-push`：未合入的演示改动，'
                  '已撤回。\n')
    section_partial = '## 门禁改动\n\n- 只交代了别的文件。\n'
    no_section = '## 现象\n\n没有门禁改动段。\n'

    # check()：坏形态红、同形好形态绿
    expect_rc('钩子-only + 正文不可得', ['.githooks/pre-push'], None, 1,
              signature='.githooks/pre-push')
    expect_rc('钩子-only + 无段', ['.githooks/pre-push'], no_section, 1,
              signature='门禁改动段缺失')
    expect_rc('钩子-only + 段内漏列', ['.githooks/pre-push'], section_partial, 1,
              signature='漏列')
    expect_rc('钩子-only + 段内交代', ['.githooks/pre-push'], section_ok, 0)
    expect_rc('非门禁文件 + 无段', [], no_section, 0)
    expect_rc('旧四类 + 无段（扩容不挤掉原行为）', ['scripts/check_x.py'], no_section, 1)

    if failures:
        for line in failures:
            print(f'SELF-TEST FAIL {line}')
        raise AssertionError(f'self_test 失败 {len(failures)} 项（issue #107 夹具）')
    print('SELF-TEST OK PATTERNS 命中范围（含 .githooks/）与 check() 判定的回归夹具全部符合预期')


def main():
    self_test()

    parser = argparse.ArgumentParser(
        description='门禁链改动必须在 PR 正文交代（issue #62 / #107）')
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

    code, lines = check(touched, body_from_args(args))
    for line in lines:
        print(line)
    return code


if __name__ == '__main__':
    sys.exit(main())
