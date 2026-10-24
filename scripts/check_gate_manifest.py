"""issue #59 gate: 门禁清单只有一个事实来源（`scripts/gates.json`）。

背景：磁盘 88 个 `scripts/check_*.py`、`ci.yml` 61 条数组、AGENTS §5.1 37 个引用，
三套口径已经漂移（issue #59）。本节把「哪个脚本算什么」收进清单，并断言：

1. 每个条目 `name` 形如 `check_*.py`、全清单无重名（一项只归一档）；
2. 磁盘 `scripts/check_*.py` 集合 == `ci` / `manual` / `stale` / `dead` / `support` 并集；
3. 每个条目在磁盘存在（`dead` 在删除前也必须存在，删完即出列）；
4. `manual` / `stale` / `dead` / `support` 必须写 `why`，`stale` 还必须带跟进 `issue` 号
   ——「人工跑的」和「红的」都要留下理由，不许把门禁藏进没有解释的档位；
5. `AGENTS.md` §5.1 的生成段与清单渲染结果逐字节相等（顺序即 CI 执行顺序）。

维护者模式 `--write` 重写 AGENTS.md §5.1 生成段；门禁本身只读。

    uv run python scripts/check_gate_manifest.py
    uv run python scripts/check_gate_manifest.py --write
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'scripts' / 'gates.json'
AGENTS = ROOT / 'AGENTS.md'
BEGIN = '<!-- gates:ci:begin -->'
END = '<!-- gates:ci:end -->'
BUCKETS = ('ci', 'manual', 'stale', 'dead', 'support')
REASONED = ('manual', 'stale', 'dead', 'support')
NAME = re.compile(r'^check_[a-z0-9_]+\.py$')


def load_manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def bucket(manifest, name):
    rows = manifest.get(name)
    return rows if isinstance(rows, list) else []


def schema_problems(manifest):
    """条目自身的形状问题：名字、重复、理由、跟进号。"""
    problems = []
    seen = {}
    for name in BUCKETS:
        rows = manifest.get(name)
        if not isinstance(rows, list):
            problems.append(f'{name}: 必须是数组')
            continue
        for index, row in enumerate(rows, 1):
            where = f'{name}[{index}]'
            if not isinstance(row, dict):
                problems.append(f'{where}: 必须是对象')
                continue
            script = row.get('name')
            if not isinstance(script, str) or not NAME.match(script):
                problems.append(f'{where}: name 不是 check_*.py：{script!r}')
                continue
            if script in seen:
                problems.append(f'{where}: {script} 已在 {seen[script]} 里（一项只能归一档）')
            else:
                seen[script] = name
            if name in REASONED and not str(row.get('why') or '').strip():
                problems.append(f'{where}: {script} 缺少 why（{name} 档必须写清理由）')
            if name == 'stale' and not isinstance(row.get('issue'), int):
                problems.append(f'{where}: {script} 是 stale，必须带跟进 issue 号')
            args = row.get('args')
            if args is not None and not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
                problems.append(f'{where}: args 必须是字符串数组')
    return problems, seen


def render(manifest):
    """把 ci 档渲染成 AGENTS §5.1 的生成段（含标记行）。"""
    ci = bucket(manifest, 'ci')
    lines = [
        BEGIN,
        '> 本节由 `scripts/check_gate_manifest.py --write` 从 `scripts/gates.json` 生成，**不要手改**；顺序即 CI 执行顺序。',
        '',
        '| # | 门禁 | 防什么 |',
        '|---|---|---|',
    ]
    for index, row in enumerate(ci, 1):
        args = ''.join(' ' + arg for arg in row.get('args') or [])
        why = str(row['why']).replace('|', '｜').replace('\n', ' ')
        lines.append(f'| {index} | `{row["name"]}{args}` | {why} |')
    others = '、'.join(f'`{name}` {len(bucket(manifest, name))}' for name in ('manual', 'stale', 'support'))
    lines += ['', f'共 {len(ci)} 个 CI 门禁；{others} 见 `scripts/gates.json` 的对应档位（理由写在那里）。', END]
    return '\n'.join(lines)


def swap_block(text, block):
    """把 AGENTS 里 BEGIN..END 之间的内容换成 block；标记不唯一或缺失时返回 None。"""
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        return None
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return f'{head}{block}{tail}'


def agents_problem(text, block):
    """生成段与清单不一致（或标记缺失／顺序颠倒）时返回原因，否则 None。"""
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        return 'AGENTS.md 缺少唯一的 gates:ci 标记（先手工补上 BEGIN/END 再跑 --write）'
    start = text.find(BEGIN)
    end = text.find(END)
    if start > end:
        return 'AGENTS.md 的 gates:ci 标记顺序颠倒'
    live = text[start:end + len(END)]
    if live != block:
        return 'AGENTS.md §5.1 生成段与清单不一致（跑 --write 重写）'
    return None


def main():
    parser = argparse.ArgumentParser(description='门禁清单一致性门禁（issue #59）')
    parser.add_argument('--write', action='store_true', help='重写 AGENTS.md §5.1 生成段')
    args = parser.parse_args()
    if not MANIFEST.is_file():
        print(f'FAIL: 缺少清单 {MANIFEST.relative_to(ROOT)}')
        return 1

    manifest = load_manifest()
    problems, seen = schema_problems(manifest)
    if problems:
        for item in problems:
            print(f'  FAIL {item}')
        print(f'FAIL: {len(problems)} 处清单形状问题')
        return 1
    print(f'  ok  形状：{len(seen)} 个条目、五档齐全、无重名')

    disk = {path.name for path in (ROOT / 'scripts').glob('check_*.py')}
    listed = set(seen)
    missing = sorted(disk - listed)
    extra = sorted(listed - disk)
    if missing or extra:
        for name in missing:
            print(f'  FAIL 磁盘有但清单没有：{name}')
        for name in extra:
            print(f'  FAIL 清单有但磁盘没有：{name}')
        print('FAIL: 磁盘与清单并集不一致')
        return 1
    print(f'  ok  磁盘 {len(disk)} 个 check_*.py 全部有归属')

    block = render(manifest)
    text = AGENTS.read_text(encoding='utf-8')
    if args.write:
        swapped = swap_block(text, block)
        if swapped is None:
            print('FAIL: AGENTS.md 缺少唯一的 gates:ci 标记')
            return 1
        if swapped != text:
            AGENTS.write_text(swapped, encoding='utf-8')
            print('  ok  已重写 AGENTS.md §5.1 生成段')
        else:
            print('  ok  AGENTS.md §5.1 已是清单渲染结果（幂等）')
        return 0

    problem = agents_problem(text, block)
    if problem:
        print(f'  FAIL {problem}')
        print('FAIL: AGENTS §5.1 与清单漂移')
        return 1
    counts = '、'.join(f'{name} {len(bucket(manifest, name))}' for name in BUCKETS)
    print(f'  ok  AGENTS §5.1 与清单一致（{counts}）')
    print('PASS: 门禁清单是唯一事实来源。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
