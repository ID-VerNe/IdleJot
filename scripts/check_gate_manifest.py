"""issue #59 gate: 门禁清单只有一个事实来源（`scripts/gates.json`）。

背景：磁盘 88 个 `scripts/check_*.py`、`ci.yml` 61 条数组、AGENTS §5.1 37 个引用，
三套口径已经漂移（issue #59）。本节把「哪个脚本算什么」收进清单，并断言：

1. 每个条目 `name` 形如 `check_*.py`、全清单无重名（一项只归一档）；
2. 磁盘 `scripts/check_*.py` 集合 == `ci` / `manual` / `stale` / `dead` / `support` 并集；
3. 每个条目在磁盘存在（`dead` 在删除前也必须存在，删完即出列）；
4. `manual` / `stale` / `dead` / `support` 必须写 `why`，`stale` 还必须带跟进 `issue` 号
   ——「人工跑的」和「红的」都要留下理由，不许把门禁藏进没有解释的档位；
5. `AGENTS.md` §5.1 的生成段与清单渲染结果逐字节相等（顺序即 CI 执行顺序）；
6. 分片（issue #60）：`shards` 非空且每个都写了 `why`，每条 `ci` 门禁的 `shard` 都在
   `shards` 里，每片至少一条门禁，且 `.github/workflows/ci.yml` 的 `matrix.shard`
   与清单完全一致——分片只加在清单里会让 CI 静默漏跑，必须拦住；
7. 生成段末尾的计数句覆盖 `BUCKETS` 里每个非 `ci` 档位且数量与清单一致
   （issue #90）——渲染与 AGENTS.md 逐字节相等时，生成器硬编码档位会让两边
   一起错，只有拿 `BUCKETS` 反查文本才能发现漏报；
8. 档位集合与清单一致（issue #90）：`BUCKETS` 是手写元组，正是 issue #90 的病根
   （手写档位表漂移）。拿清单里的档位键反查它，清单多一档或少一档都红。

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
WORKFLOW = ROOT / '.github' / 'workflows' / 'ci.yml'
BEGIN = '<!-- gates:ci:begin -->'
END = '<!-- gates:ci:end -->'
BUCKETS = ('ci', 'manual', 'stale', 'dead', 'support')
REASONED = ('manual', 'stale', 'dead', 'support')
NAME = re.compile(r'^check_[a-z0-9_]+\.py$')
SHARD_NAME = re.compile(r'^[a-z][a-z0-9_]*$')
MATRIX = re.compile(r'shard:\s*\[([^\]]*)\]')
COUNT_RE = re.compile(r'`([a-z_]+)`\s*(\d+)')
CI_COUNT_RE = re.compile(r'^共 (\d+) 个 CI 门禁')
# 清单里的非档位键（其余 list 型顶层键都算档位）。
META_KEYS = ('shards',)


def load_manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def bucket(manifest, name):
    rows = manifest.get(name)
    return rows if isinstance(rows, list) else []


def shard_names(manifest):
    return [row['name'] for row in bucket(manifest, 'shards') if isinstance(row, dict)]


def shard_problems(manifest):
    """`shards` 自身的形状：非空、唯一、小写 slug、必须写 why。"""
    rows = manifest.get('shards')
    if not isinstance(rows, list) or not rows:
        return ['shards: 必须是非空数组（ci 每条门禁都要归一个分片）'], []
    problems = []
    names = []
    for index, row in enumerate(rows, 1):
        where = f'shards[{index}]'
        if not isinstance(row, dict):
            problems.append(f'{where}: 必须是对象')
            continue
        name = row.get('name')
        if not isinstance(name, str) or not SHARD_NAME.match(name):
            problems.append(f'{where}: name 必须是小写 slug：{name!r}')
            continue
        if name in names:
            problems.append(f'{where}: 分片 {name} 重复')
        names.append(name)
        if not str(row.get('why') or '').strip():
            problems.append(f'{where}: 分片 {name} 缺少 why')
    return problems, names


def schema_problems(manifest, shards):
    """条目自身的形状问题：名字、重复、理由、跟进号、分片归属。"""
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
    used = {shard: 0 for shard in shards}
    for row in bucket(manifest, 'ci'):
        shard = row.get('shard')
        if shard not in used:
            problems.append(f'ci: {row.get("name")} 的 shard 不在 shards 里：{shard!r}')
        else:
            used[shard] += 1
    for shard in shards:
        if not used[shard]:
            problems.append(f'shards: {shard} 没有任何 ci 门禁（空分片会让 CI 起一个空 job）')
    return problems, seen


def workflow_problems(shards):
    """`ci.yml` 的 `matrix.shard` 与清单分片必须逐一对应（issue #60）。"""
    if not WORKFLOW.is_file():
        return [f'缺少 {WORKFLOW.relative_to(ROOT)}']
    matches = MATRIX.findall(WORKFLOW.read_text(encoding='utf-8'))
    if len(matches) != 1:
        return [f'{WORKFLOW.relative_to(ROOT)} 必须恰好有一处 `shard: [...]` 矩阵（找到 {len(matches)} 处）']
    live = [item.strip() for item in matches[0].split(',') if item.strip()]
    if sorted(live) != sorted(shards):
        return [f'ci.yml 的 matrix.shard 与清单分片不一致：yml {live} / 清单 {shards}']
    return []


def render(manifest):
    """把 ci 档渲染成 AGENTS §5.1 的生成段（含标记行）。"""
    ci = bucket(manifest, 'ci')
    names = shard_names(manifest)
    tally = {shard: 0 for shard in names}
    for row in ci:
        tally[row['shard']] = tally.get(row['shard'], 0) + 1
    lines = [
        BEGIN,
        '> 本节由 `scripts/check_gate_manifest.py --write` 从 `scripts/gates.json` 生成，**不要手改**；顺序即本机全量执行顺序，`分片` 列即 CI matrix 分片（同片串行、片间并行）。',
        '',
        '| # | 分片 | 门禁 | 防什么 |',
        '|---|---|---|---|',
    ]
    for index, row in enumerate(ci, 1):
        args = ''.join(' ' + arg for arg in row.get('args') or [])
        why = str(row['why']).replace('|', '｜').replace('\n', ' ')
        lines.append(f'| {index} | `{row["shard"]}` | `{row["name"]}{args}` | {why} |')
    shard_text = '、'.join(f'`{shard}` {tally.get(shard, 0)}' for shard in names)
    # 档位计数句必须覆盖 BUCKETS 里每个非 ci 档位（issue #90）：
    # 这里从 BUCKETS 派生而不是手写元组，以后加档位不会漏报。
    others = '、'.join(
        f'`{name}` {len(bucket(manifest, name))}' for name in BUCKETS if name != 'ci'
    )
    lines += [
        '',
        f'共 {len(ci)} 个 CI 门禁，分 {len(names)} 个分片（{shard_text}）；{others} 见 `scripts/gates.json` 的对应档位（理由写在那里）。',
        END,
    ]
    return '\n'.join(lines)


def bucket_key_problems(manifest):
    """清单里的档位键必须都在 `BUCKETS` 里（issue #90）。

    `BUCKETS` 是本文件里的手写元组，正是 issue #90 的病根（手写档位表漂移）。
    如果它少了一个档位，`render()` 与计数句断言会**同时**不再要求该档，
    文档随之少一档而门禁仍绿。清单是唯一事实来源（issue #59），所以反过来
    拿清单的档位键钉死 `BUCKETS`：清单有而 `BUCKETS` 没有 → 红。

    退休一个档位要同时改两处（清单删键 + `BUCKETS` 删项），这是刻意的：
    档位集合变动必须是显式动作，不能靠漏改手写元组发生。
    """
    listed = {
        key for key, rows in manifest.items()
        if isinstance(rows, list) and key not in META_KEYS
    }
    return [
        f'{name}: 清单里有这个档位，但 BUCKETS 里没有（档位集合必须两处一致）'
        for name in sorted(listed - set(BUCKETS))
    ]


def tally_problems(manifest, block):
    """生成段末尾的计数句是否覆盖 BUCKETS 里每个非 ci 档位、数量对不对（issue #90）。

    为什么需要它：渲染结果与 AGENTS.md 是逐字节相等的，生成器一旦硬编码档位
    （漏掉 `dead`），文档会被一起改错而门禁全绿。只有拿 `BUCKETS` 反查文本
    才能发现漏报。数量与清单实际条数比对，不写死具体数字（清单会变）。
    """
    problems = []
    tally_lines = [line for line in block.splitlines() if line.startswith('共 ')]
    if len(tally_lines) != 1:
        return [f'生成段计数句应恰好一行，实际 {len(tally_lines)} 行']
    line = tally_lines[0]
    ci_match = CI_COUNT_RE.match(line)
    if not ci_match:
        return ['生成段计数句格式不对（应以「共 N 个 CI 门禁」开头）']
    ci_total = len(bucket(manifest, 'ci'))
    if int(ci_match.group(1)) != ci_total:
        problems.append(f'计数句的 CI 门禁数写成 {ci_match.group(1)}，清单实际 {ci_total}')
    if '；' not in line:
        return problems + ['生成段计数句缺少「；」分隔的档位计数部分']
    # 档位计数在分号之后（分号之前是分片名，不在 BUCKETS 里）
    tail = line.split('；', 1)[1]
    counts = {name: int(number) for name, number in COUNT_RE.findall(tail)}
    for name in BUCKETS:
        if name == 'ci':
            continue
        if name not in counts:
            problems.append(f'计数句漏报 `{name}` 档')
        elif counts[name] != len(bucket(manifest, name)):
            problems.append(
                f'计数句 `{name}` 档写成 {counts[name]}，清单实际 {len(bucket(manifest, name))}'
            )
    for name in counts:
        if name == 'ci':
            problems.append('计数句在分号后又列了一次 `ci` 档（ci 只在前半句出现）')
        elif name not in BUCKETS:
            problems.append(f'计数句出现清单里没有的档位 `{name}`')
    return problems


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


def tally_self_test(manifest):
    """自检：把计数句里每个非 ci 档位逐个抠掉，断言检查器都会红（issue #90）。

    没有这一条，新增的断言可能写成「怎么跑都绿」；这一条保证它真能钉住
    原症状（生成器少列一档 → 报错）。

    逐档做而不是只锚 `dead`：档位集合以后可能变（退休一个档位），
    锚死单个档位名会误红。扣的是分号**之后**那段（分号前是分片名）。
    """
    block = render(manifest)
    lines = block.splitlines()
    index = next((i for i, line in enumerate(lines) if line.startswith('共 ')), None)
    if index is None:
        return ['自检失败：渲染结果里找不到计数句']
    line = lines[index]
    if '；' not in line:
        return ['自检失败：渲染结果的计数句缺少「；」分隔的档位计数部分']
    head, tail = line.split('；', 1)
    problems = []
    for name in BUCKETS:
        if name == 'ci':
            continue
        token = f'`{name}` '
        if token not in tail:
            problems.append(f'自检失败：渲染结果的计数句里没有 {name} 档')
            continue
        broken_tail = re.sub(rf'`{re.escape(name)}`\s*\d+', '', tail, count=1)
        broken = '\n'.join(lines[:index] + [head + '；' + broken_tail] + lines[index + 1:])
        found = tally_problems(manifest, broken)
        if not found:
            problems.append(f'自检失败：计数句少列 {name} 档时检查器没有报错')
        elif not any(name in item for item in found):
            problems.append(f'自检失败：少列 {name} 档时报错没提到它：{found}')
    return problems


def main():
    parser = argparse.ArgumentParser(description='门禁清单一致性门禁（issue #59 / #60 / #90）')
    parser.add_argument('--write', action='store_true', help='重写 AGENTS.md §5.1 生成段')
    parser.add_argument('--self-test', action='store_true', help='只跑计数句断言的自检')
    args = parser.parse_args()
    if not MANIFEST.is_file():
        print(f'FAIL: 缺少清单 {MANIFEST.relative_to(ROOT)}')
        return 1

    manifest = load_manifest()

    shards_shape, shards = shard_problems(manifest)
    problems, seen = schema_problems(manifest, shards)
    problems = shards_shape + problems
    if problems:
        for item in problems:
            print(f'  FAIL {item}')
        print(f'FAIL: {len(problems)} 处清单形状问题')
        return 1
    print(f'  ok  形状：{len(seen)} 个条目、五档齐全、无重名')
    print(f'  ok  分片：{len(shards)} 个（{"、".join(shards)}），每个 ci 条目都有归属')

    # 档位集合必须两处一致（issue #90）：清单是唯一事实来源，
    # BUCKETS 是手写元组，少一档会让 render 与断言同时不再要求该档。
    bucket_problems = bucket_key_problems(manifest)
    if bucket_problems:
        for item in bucket_problems:
            print(f'  FAIL {item}')
        print(f'FAIL: {len(bucket_problems)} 处档位集合不一致')
        return 1
    print(f'  ok  档位集合：清单与 BUCKETS 一致（{"、".join(BUCKETS)}）')

    drift = workflow_problems(shards)
    if drift:
        for item in drift:
            print(f'  FAIL {item}')
        print('FAIL: ci.yml 分片矩阵与清单不一致')
        return 1
    print('  ok  ci.yml 的 matrix.shard 与清单一致')

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

    # 计数句断言先自检：证明「少列一档时它会红」，否则这条断言可能怎么跑都绿。
    # 放在形状校验之后：render() 要求清单条目带 shard，畸形清单先由形状断言报错。
    self_problems = tally_self_test(manifest)
    for item in self_problems:
        print(f'  FAIL {item}')
    if self_problems:
        print(f'FAIL: {len(self_problems)} 处计数句自检问题')
        return 1
    print('  ok  计数句自检：少列一档时断言会红')

    if args.self_test:
        print('PASS: 计数句断言的自检通过。')
        return 0

    block = render(manifest)
    # 计数句必须覆盖 BUCKETS 里每个非 ci 档位（issue #90）：
    # 渲染结果与 AGENTS.md 逐字节相等时，生成器少列一档会让两边一起错。
    # 放在最前面：--write 也不许把坏块写进 AGENTS.md。
    tally = tally_problems(manifest, block)
    if tally:
        for item in tally:
            print(f'  FAIL 计数句：{item}')
        print('FAIL: 计数句漏报或写错档位')
        return 1
    counts = '、'.join(f'{name} {len(bucket(manifest, name))}' for name in BUCKETS)
    print(f'  ok  计数句覆盖全部非 ci 档位（{counts}）')

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
    print(f'  ok  AGENTS §5.1 与清单一致（{counts}）')
    print('PASS: 门禁清单是唯一事实来源。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
