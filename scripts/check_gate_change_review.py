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
3. 段内必须有一条**独立复核结论**（issue #324）：一行以 `独立复核` 开头，且同行含
   `APPROVE` / `APPROVED` / `REQUEST_CHANGES`。只有 `<PENDING>` / 空白 / `待复核`
   一律红。这是**闸门**：复核对一个 PR 必然晚于它的创建，所以红灯的语义是「复核还没贴」，
   作者补完结论后重跑即绿——它不是「CI 坏了」；
4. 自审判据（issue #324）：`## 门禁改动` 段的实质内容必须在复核行**之前**，
   复核行排到段末。先写结论后补证据是自审的形状；
5. 没有 PR 上下文（本机 parity / 无 base）时打印 SKIP 并通过；**显式声明**
   `GATE_CHANGES=1` / `--gate-changes` 时不再 SKIP：有 base（push 事件的
   `before`）就算改动集——合并进 `slim` 的 push 天然落在这里，改动已过 PR
   闸门，照常放行并留痕；**没有 base 则改动集不可得，fail-closed**
   （`base...HEAD` 在无 base 时退化成空集，直接推上来绕过 PR 的口子只能靠这条
   守住）。base 非法、diff 失败等无法判断的情况同样 fail-closed。

    uv run python scripts/check_gate_change_review.py --base origin/slim --body-file pr.md
    uv run python scripts/check_gate_change_review.py --gate-changes --body-file pr.md

CI 由 `.github/workflows/ci.yml` 注入 `PR_BASE_SHA` / `PR_NUMBER`（仅 pull_request
有值）、`GATE_CHANGES`（push 事件为 `1`），以及 `PR_BASE_SHA` 的 push 回退值
`github.event.before`——push 也有 base 可算，所以合并后的 push 不会因为「读不到正文」
被误判成红。

**重跑读的是当下正文（#338）**：`github.event.pull_request.body` 是**事件快照**，
`gh run rerun` 重放同一个事件，改完正文再重跑读到的仍是旧正文——作者补了 `## 门禁改动`
段与复核结论，重跑照样红，只能空推一个 commit 骗新事件。所以正文的最终来源改成**现取**：
`GET /repos/<owner>/<repo>/pulls/<n>`（`pull-requests: read`，与 `created_at` 同一次
调用）。正文优先级 `--body-file` > `PR_BODY_FILE` > **按 `PR_NUMBER` 现取** > `PR_BODY`
（后者只留给本机调试）。现取失败时**不**退回事件快照：改动触及门禁链就照旧红，并打印
`正文取不到：<原因>`——拿旧正文冒充当下正文会让「已改好」与「没改」在日志里同形。
PR 正文被清空（`body` 为 null）按「有正文、没有段」处理，与「取不到正文」分开报。

**时点边界（#324）**：闸门上界是 `APPROVED_AT`。上界未到、`base` 无从得知（`--gate-changes`
显式声明）或基线早于上界时，这条 PR 属于「闸门建立之前」，复核行按 `<PENDING>`
视作，只打印提示不放行也不判红——闸门只管之后开的 PR。`gate_applies(base, now=None)`
把「现在」收成可注入参数，上界未到这件事因此可测（#338 顺带补的夹具）。CI 按
`pull_request.number` 现取 PR 创建时间（`GH_TOKEN` 只读 `pull_requests`，取不到时退回基线判据）。

入口处先跑 `self_test()`（issue #107 的离线夹具：PATTERNS 命中范围 + `check()`
判定的回归，不碰 git / 网络 / 文件），把「钩子改动必须披露」钉在每次执行里。
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
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
NEXT_HEADING = re.compile(r'^#{1,2}[ \t]+\S', re.MULTILINE)

# issue #324：独立复核闸门的生效上界。基线早于该时刻（或基线不可得）的 PR
# 属于「闸门建立之前」，复核行只提示不判红——本 PR 自身也是其中之一。
APPROVED_AT = '2026-11-02T00:00:00Z'
# 复核结论行：一行以「独立复核」开头（允许 markdown 列表符 / 加粗），同行带结论。
# 用 re.escape 拼接，不在源码里留裸 `**`（避免与 glob 抢眼）。
REVIEW_LINE = re.compile(r'^\s*(?:[-*+]\s+)?' + re.escape('*') + r'{0,2}\s*独立复核')
# 长的在前：APPROVE 是 APPROVED 的前缀，短串先判会把 APPROVED 认成 APPROVE。
VERDICTS = ('REQUEST_CHANGES', 'APPROVED', 'APPROVE')
DIVIDER = re.compile(r'^\s*(?:-{3,}|\*{3,}|_{3,})\s*$')
# PR 正文的收尾链接行（`Refs #324` / `Closes #1, #2`）：不带判据，可与分隔线同等看待。
FOOTER_LINE = re.compile(r'^\s*(?:Refs|Closes|Fixes)\b', re.IGNORECASE)


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
    """取 `## 门禁改动` 段的直接内容（issue #324）。

    `## 门禁改动` 段到下一个**同级或更高级**标题（`##` / `#`）为止；段内的 `###`
    子标题仍算本段——引用块（`> **职责边界**`）、`###` 小节之后跟着的
    `Refs #NN` 都是正文证据，属于本段，不能把它们排除在「复核行须在段末」之外，
    否则正文一收尾就恒被这份判定漏看。
    """
    match = HEADING.search(body)
    if not match:
        return None
    rest = body[match.end():]
    nxt = NEXT_HEADING.search(rest)
    return rest[:nxt.start()] if nxt else rest


def read_body_text(path):
    return Path(path).read_text(encoding='utf-8', errors='replace')


def parse_time(text):
    """`2026-11-02T00:00:00Z` / GitHub 的 `2026-11-02T00:00:00Z` → aware datetime；空值与坏值返回 None。"""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.strip().replace('Z', '+00:00'))
    except ValueError:
        return None


def gate_applies(base, now=None):
    """issue #324 时点边界：基线早于上界 → 闸门不适用（None 表示基线不可得，同样不适用）。

    `now` 缺省取当前时刻（UTC）。上界还没到（`APPROVED_AT` 是未来）时同样不适用：
    闸门自那个时刻起才生效，之前开的 PR 一律按旧口径。把它显式收进判据，是为了让
    「上界之前」这件事**可测**——不然后测的夹具只能靠改系统时钟或伪造未来提交才能
    走到闸门分支，那不是回归夹具该依赖的东西。
    """
    stamp = parse_time(base)
    limit = parse_time(APPROVED_AT)
    if stamp is None or limit is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current < limit:
        return False
    return stamp >= limit


def review_record(section):
    """`## 门禁改动` 段里的独立复核结论行（issue #324）。

    返回 (结论行原文, 结论)；结论为 None 表示「还没贴出结论」（只有 `<PENDING>` /
    空白 / `待复核` 之类）。纯函数，供 check() 与 self_test() 共用。
    """
    if section is None:
        return None, None
    for raw in section.splitlines():
        line = raw.strip()
        if not REVIEW_LINE.match(line):
            continue
        upper = line.upper()
        for verdict in VERDICTS:
            if verdict in upper:
                return line, verdict
        return line, None
    return None, None


def review_order_problem(section):
    """自审判据（issue #324）：复核行之后不许再有实质内容。

    返回第一处「排在复核行之后」的实质内容行（去掉外层空白）；合规时返回 None。
    段里没有复核行时也返回 None——缺结论由 review_record() 判，不在这里重复报。
    分隔线与纯 `Refs #NN` 收尾行不算实质内容：它们不带任何判据、也不构成证据，
    只有它们排在复核之后时正文仍是「先证据、后结论」。
    """
    if section is None:
        return None
    lines = [line.strip() for line in section.splitlines()]
    order = [i for i, line in enumerate(lines) if REVIEW_LINE.match(line)]
    if not order:
        return None
    tail = [
        line for line in lines[order[-1] + 1:]
        if line and not DIVIDER.match(line) and not FOOTER_LINE.match(line)
    ]
    return tail[0] if tail else None


def fetch_pull(number, cache=None):
    """`GET /repos/<owner>/<repo>/pulls/<n>` 的 payload；取不到时返回 None。

    `created_at`（#324 时点边界）与 `body`（#338 现取正文）共用这一次只读调用。
    token 从 `GH_TOKEN` / `GITHUB_TOKEN` 取。没有 number / 没 token / 没 slug /
    网络不可达 / 解析失败一律返回 None，由调用方各自决定退回什么——本函数不猜。
    """
    if not number:
        return None
    if cache is not None and number in cache:
        return cache[number]
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    slug = os.environ.get('GITHUB_REPOSITORY')
    payload = None
    if token and slug:
        url = f'https://api.github.com/repos/{slug}/pulls/{number}'
        request = urllib.request.Request(url, headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'check_gate_change_review',
        })
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode('utf-8', errors='replace'))
        except urllib.error.HTTPError as error:
            payload = {'__error__': f'HTTP {error.code}'}
        except Exception as error:  # noqa: BLE001 - 失败原因原样带给调用方，不在这里区分
            payload = {'__error__': type(error).__name__}
        if not isinstance(payload, dict):
            payload = {'__error__': '响应不是对象'}
    if cache is not None:
        cache[number] = payload
    return payload


def pr_created_at(number, cache=None):
    """PR 创建时间（`created_at`）；取不到时返回 None。

    时点判据退回基线，**绝不因为查不到时间就放行**（放行由 gate_applies 的基线判据
    决定）。
    """
    payload = fetch_pull(number, cache)
    if not payload or '__error__' in payload:
        return None
    return payload.get('created_at')


def live_body(number, cache=None):
    """按 PR 编号现取正文（issue #338）→ (正文, 取不到的原因)。

    正文被清空（`body` 为 null）是**有正文、没有段**：返回 `('', None)`，与取不到正文
    分开——前者由 check() 报「没有 `## 门禁改动` 段」，后者报「拿不到 PR 正文」。
    """
    if not number:
        return None, '没有 PR 编号（PR_NUMBER 为空）'
    payload = fetch_pull(number, cache)
    if payload is None:
        return None, '没有 token / 仓库名（GH_TOKEN、GITHUB_REPOSITORY 缺失），无法现取'
    if '__error__' in payload:
        reason = payload['__error__']
        if reason == 'HTTP 403':
            reason += '（token 缺 pull-requests: read 权限）'
        return None, f'GET pulls/{number} 失败：{reason}'
    body = payload.get('body')
    return (body if isinstance(body, str) else ''), None


def body_from_args(args, number='', cache=None):
    """正文优先级：--body-file > PR_BODY_FILE > 按 PR_NUMBER 现取 > PR_BODY。

    返回 (正文, 来源, 取不到的原因)。现取失败时**不**退回 `PR_BODY`（issue #338）：
    那是事件快照，`gh run rerun` 重放的就是它——拿旧正文冒充当下正文，正是 #338 的
    那个坑。`PR_BODY` 只在本机没有 PR 上下文时兜底调试。
    """
    if args.body_file:
        return read_body_text(args.body_file), f'--body-file {args.body_file}', None
    path = os.environ.get('PR_BODY_FILE')
    if path:
        return read_body_text(path), f'PR_BODY_FILE {path}', None
    if number:
        body, reason = live_body(number, cache)
        if body is not None:
            return body, f'现取 PR #{number}', None
        return None, '现取失败', reason
    return os.environ.get('PR_BODY'), 'PR_BODY（事件快照，仅本机调试）', None


def check(touched, body, gated=False):
    """核心判定：门禁链命中文件 + PR 正文 → (退出码, 输出行)。

    body 为 None 表示正文不可得（与空串区分：空串是「有正文、没有段」）。
    gated=True 表示这条 PR 落在 `APPROVED_AT` 之后，独立复核闸门生效（issue #324）；
    self_test() 里显式传，main() 按基线 / PR 创建时间算。纯函数，供 main() 与
    self_test() 共用（issue #107）。
    """
    lines = []
    section = gate_section(body) if body is not None else None
    gated = gated and bool(touched)

    if not touched:
        if section is not None:
            lines.append('  ok  本次未触及门禁链；正文带 `## 门禁改动` 段（允许）')
        else:
            lines.append('  ok  本次未触及门禁链，不需要 `## 门禁改动` 段')
        return 0, lines + ['PASS: 门禁链无改动。']

    if body is None:
        lines.append(f'  FAIL 改动触及门禁链（{len(touched)} 个文件），但拿不到 PR 正文')
        lines.append('     CI 按 PR_NUMBER 现取（需 pull-requests: read），本机用 --body-file')
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

    record, verdict = review_record(section)
    order_tail = review_order_problem(section)
    if not gated:
        if record is None:
            lines.append(f'  hint 段内没有独立复核结论行（闸门自 {APPROVED_AT} 起生效，本 PR 在之前）')
        elif verdict is None:
            lines.append('  hint 段内复核行还没贴出结论（闸门生效前，放行）')
        elif order_tail:
            lines.append('  hint 复核行之后还有实质内容（闸门生效前，放行）')
        else:
            lines.append(f'  ok  独立复核已贴：{verdict}')
    else:
        if record is None:
            lines.append('  FAIL `## 门禁改动` 段没有独立复核结论行（issue #324）')
            lines.append('     需要一行：`独立复核`：APPROVE / REQUEST_CHANGES + 复核证据；')
            lines.append('     复核行放在段末（先摆证据、后给结论）')
            return 1, lines + ['FAIL: 独立复核缺席（闸门未通过）。']
        if verdict is None:
            lines.append(f'  FAIL 独立复核仍是占位、没有结论：{record}')
            lines.append('     只认 APPROVE / APPROVED / REQUEST_CHANGES；`<PENDING>` 不算复核')
            return 1, lines + ['FAIL: 独立复核尚未贴出结论（闸门未通过）。']
        if order_tail:
            lines.append(f'  FAIL 复核结论排在正文证据之前，后面还有：{order_tail}')
            lines.append('     复核行须在段末：先摆断言改了什么 / 为什么改 / 三种组合证据，最后给结论')
            return 1, lines + ['FAIL: 复核结论顺序不对（自审形状，闸门未通过）。']
        lines.append(f'  ok  独立复核已贴：{verdict}')

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

    def expect_rc(name, touched, body, want_rc, signature=None, gated=False):
        code, out = check(touched, body, gated=gated)
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

    # issue #324：独立复核闸门 + 自审判据。坏形态红 / 同形好形态绿成对出现。
    def section_of(evidence, review):
        return ('## 门禁改动\n\n'
                f'- `scripts/check_x.py`：{evidence}\n\n'
                f'{review}\n')

    touched = ['scripts/check_x.py']
    pending = section_of('改了断言', '**独立复核**：<PENDING>')
    approve_last = section_of('改了断言', '**独立复核**：APPROVE，复核证据见评论')
    approved_last = section_of('改了断言', '**独立复核**：APPROVED，复核证据见评论')
    approve_early = section_of('**独立复核**：APPROVE，复核证据见评论', '- 断言改了什么：改了')
    evidence_after = ('## 门禁改动\n\n'
                      '- `scripts/check_x.py`：改了断言\n\n'
                      '**独立复核**：APPROVE\n\n'
                      '- 三种组合证据：见评论\n')
    request_last = section_of('改了断言', '独立复核：REQUEST_CHANGES，理由见评论')
    no_review = section_of('改了断言', '')
    divider_tail = ('## 门禁改动\n\n- `scripts/check_x.py`：改了断言\n\n'
                    '**独立复核**：APPROVE\n\n---\n')
    lowered = section_of('改了断言', '独立复核：approved')

    # #324 回归：`Refs #NN` / 引用块 / `###` 小节都在 `## 门禁改动` 段内，不是段外。
    # 复核行必须排在它们之后才叫「段末」——这正是真实 PR 正文的收尾形状。
    refs_after = ('## 门禁改动\n\n'
                  '- `scripts/check_x.py`：改了断言\n\n'
                  '**独立复核**：APPROVE\n\n'
                  'Refs #324\n')
    refs_last = ('## 门禁改动\n\n'
                 '- `scripts/check_x.py`：改了断言\n\n'
                 '> **职责边界**：只水群\n'
                 '> 发现即立单\n\n'
                 '### 复核要点\n\n'
                 '- 判据方向正确\n\n'
                 'Refs #324\n\n'
                 '**独立复核**：APPROVE\n\n'
                 '---\n\n'
                 '## 测试\n\n'
                 '- 下一段的内容不算本段\n')
    heading_cut = ('## 门禁改动\n\n'
                   '- `scripts/check_x.py`：改了断言\n\n'
                   '**独立复核**：APPROVE\n\n'
                   '## 测试\n\n'
                   '- 下一段的内容不算本段\n')

    expect('review_record 认出 PENDING', review_record(pending)[1], None)
    expect('review_record 认出 APPROVED 优先于 APPROVE',
           review_record(approved_last)[1], 'APPROVED')
    expect('review_record 认出裸 APPROVE', review_record(approve_last)[1], 'APPROVE')
    expect('review_record 认出 REQUEST_CHANGES', review_record(request_last)[1], 'REQUEST_CHANGES')
    expect('review_record 小写 approved 也认', review_record(lowered)[1], 'APPROVED')
    expect('review_record 无段返回 (None, None)', review_record(None), (None, None))
    expect('review_order_problem 复核在段末合规', review_order_problem(approve_last), None)
    expect('review_order_problem 分隔线不算实质内容',
           review_order_problem(divider_tail), None)
    expect('review_order_problem 复核在前报出后续行',
           review_order_problem(evidence_after), '- 三种组合证据：见评论')
    expect('review_order_problem 段首就是复核行（无后续）', review_order_problem(approve_early), None)
    expect('review_order_problem 复核后的纯 Refs 收尾不算实质内容',
           review_order_problem(gate_section(refs_after)), None)
    expect('review_order_problem 复核后的证据行仍算实质内容',
           review_order_problem(gate_section(evidence_after)), '- 三种组合证据：见评论')
    expect('review_order_problem 引用块 / ### 段落内的复核仍算段末',
           review_order_problem(gate_section(refs_last)), None)
    expect('review_order_problem 同级 ## 截断本段',
           review_order_problem(gate_section(heading_cut)), None)

    expect_rc('闸门：PENDING 红', touched, pending, 1, signature='尚未贴出结论', gated=True)
    expect_rc('闸门：同形 APPROVE 在段末绿', touched, approve_last, 0, gated=True)
    expect_rc('闸门：没复核行也红', touched, no_review, 1, signature='没有独立复核结论行', gated=True)
    expect_rc('闸门：REQUEST_CHANGES 允许（结论已贴）', touched, request_last, 0, gated=True)
    expect_rc('闸门：自审形状（结论后面还有证据）红', touched, evidence_after, 1,
              signature='复核结论排在正文证据之前', gated=True)
    expect_rc('闸门：分隔线收尾仍绿', touched, divider_tail, 0, gated=True)
    expect_rc('闸门：纯 Refs 收尾仍绿', touched, refs_after, 0, gated=True)
    expect_rc('闸门：引用块 / ### 之后写在段末绿', touched, refs_last, 0, gated=True)
    expect_rc('闸门：搭段但漏列文件仍红', touched, section_partial, 1, signature='漏列', gated=True)
    # 同一批夹具在闸门生效前（gated=False）恢复旧口径：只校验段 + 漏列，不因复核缺席变红。
    expect_rc('生效前：PENDING 放行（旧口径）', touched, pending, 0)
    expect_rc('生效前：没复核行放行（旧口径）', touched, no_review, 0)
    expect_rc('生效前：漏列仍红（旧口径）', touched, section_partial, 1, signature='漏列')
    expect_rc('生效前：正文不可得仍红（旧口径）', touched, None, 1)

    # main() 无 base 但显式声明时 fail-closed（不冒充漏列），这条由 main 分支保证。


    # 上界未到时一律按旧口径（与基线无关），上界之后才看基线——这条让「闸门建立前」
    # 可测，而不是只能靠伪造未来提交 / 改系统时钟去撞。
    before = datetime(2026, 11, 1, tzinfo=timezone.utc)
    after = datetime(2026, 11, 3, tzinfo=timezone.utc)
    # 时点判据一律显式喂 now：默认取墙钟，跑到上界那天这类用例会自己改结论。
    expect('gate_applies 基线晚于上界', gate_applies('2026-11-03T00:00:00Z', after), True)
    expect('gate_applies 基线恰为上界', gate_applies(APPROVED_AT, after), True)
    expect('gate_applies 基线早于上界', gate_applies('2026-10-01T00:00:00Z', after), False)
    expect('gate_applies 基线不可得不适用', gate_applies('', after), False)
    expect('gate_applies 基线坏值不适用', gate_applies('not-a-sha', after), False)
    expect('gate_applies 上界未到：新基线也不适用', gate_applies('2026-12-01T00:00:00Z', before), False)
    expect('gate_applies 上界刚过：新基线适用', gate_applies('2026-11-02T00:00:00Z', after), True)

    # issue #338：正文来源是「当下的 PR 正文」，不是事件快照。
    # 全部离线：现取路径用预置 cache 喂 payload，不打网络。
    class Args:
        body_file = None

    saved = {key: os.environ.pop(key, None)
             for key in ('PR_BODY_FILE', 'PR_BODY', 'PR_NUMBER', 'GH_TOKEN',
                         'GITHUB_TOKEN', 'GITHUB_REPOSITORY')}
    try:
        # 没有 PR 编号 → 退回 PR_BODY 快照（本机调试的唯一用途）
        os.environ['PR_BODY'] = no_section
        body, source, reason = body_from_args(Args(), '', {})
        expect('#338 无 PR 编号退回快照', body, no_section)
        expect('#338 快照来源如实标注', '快照' in source, True)
        expect('#338 快照不留原因', reason, None)

        # 有编号 + 现取成功 → 用现取正文，**不用**快照（这正是 #338 的坏形态）
        os.environ.update(GH_TOKEN='t', GITHUB_REPOSITORY='o/r')
        cache = {'9': {'body': approve_last, 'created_at': '2027-01-01T00:00:00Z'}}
        body, source, reason = body_from_args(Args(), '9', cache)
        expect('#338 现取优先于快照', body, approve_last)
        expect('#338 现取来源如实标注', '现取' in source, True)
        expect('#338 现取到的正文直接过闸门', check(touched, body, gated=True)[0], 0)

        # 有编号但现取失败 → 不退回快照，正文记为不可得并带原因
        body, source, reason = body_from_args(Args(), '9', {'9': {'__error__': 'HTTP 403'}})
        expect('#338 现取失败不退回快照', body, None)
        expect('#338 403 提示缺权限', 'pull-requests: read' in reason, True)
        expect('#338 现取失败仍红', check(touched, body, gated=True)[0], 1)

        # body 为 null（作者清空正文）→ 有正文、没有段；与「取不到」分开
        body, source, reason = body_from_args(Args(), '9', {'9': {'body': None}})
        expect('#338 正文被清空不是「取不到」', (body, reason), ('', None))
        expect('#338 正文清空 → 段缺失', check(touched, body, gated=True)[0], 1)
        expect('#338 正文清空报「段缺失」',
               any('门禁改动段缺失' in line
                   for line in check(touched, body, gated=True)[1]), True)

        # created_at 与正文共用同一次调用：cache 命中即不再请求
        expect('#338 created_at 走同一份 payload',
               pr_created_at('9', {'9': {'created_at': '2027-01-01T00:00:00Z'}}),
               '2027-01-01T00:00:00Z')
        expect('#338 无编号时 created_at 为 None', pr_created_at(''), None)
        expect('#338 没有编号时 live_body 说原因', live_body('')[1], '没有 PR 编号（PR_NUMBER 为空）')
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    if failures:
        for line in failures:
            print(f'SELF-TEST FAIL {line}')
        raise AssertionError(f'self_test 失败 {len(failures)} 项（issue #107 / #324 夹具）')
    print('SELF-TEST OK PATTERNS 命中范围（含 .githooks/）、check() 判定、独立复核闸门（#324）'
          '与正文现取（#338）的回归夹具全部符合预期')


def main():
    self_test()
    _main(sys.argv[1:])


def _main(argv):
    parser = argparse.ArgumentParser(
        description='门禁链改动必须在 PR 正文交代（issue #62 / #107 / #324 / #338）')
    parser.add_argument('--base', help='PR 的 base ref（默认读环境变量 PR_BASE_SHA）')
    parser.add_argument('--body-file',
                        help='PR 正文文件（默认读 PR_BODY_FILE，再按 PR_NUMBER 现取，最后 PR_BODY）')
    parser.add_argument('--gate-changes', action='store_true',
                        help='显式声明本次改动含门禁链（push / parity 无 base 时不许 SKIP）')
    parser.add_argument('--pr-head', help='仅作语义标注：PR 的 head sha（CI 注入 PR_HEAD_SHA）')
    args = parser.parse_args(argv)

    base = args.base or os.environ.get('PR_BASE_SHA') or ''
    declared = args.gate_changes or os.environ.get('GATE_CHANGES') == '1'
    head = args.pr_head or os.environ.get('PR_HEAD_SHA') or 'HEAD'
    number = os.environ.get('PR_NUMBER') or ''

    if not base and not declared:
        print('  SKIP 没有 PR base（本机直跑 / push 事件）：门禁改动段只在 PR 上校验')
        print('PASS: 非 PR 上下文，跳过门禁改动段校验。')
        return 0

    if base:
        try:
            touched = gate_chain(changed_files(base))
        except RuntimeError as error:
            print(f'  FAIL 无法计算 {base}...HEAD 的改动集：{error}')
            print('FAIL: 门禁改动段校验无法完成（fail-closed）。')
            return 1
    else:
        # 无 base 但显式声明（push 事件直接推 slim）：拿不到 diff 就不知道改了什么，
        # 也就无法核对正文是否逐个交代——fail-closed，但把话说清楚，不冒充「漏列」。
        print('  FAIL 显式声明了门禁链改动（GATE_CHANGES / --gate-changes），但没有 PR base')
        print('     改动集不可得 → 无法核对 `## 门禁改动` 段是否逐个交代；')
        print('     直接推 slim 绕过 PR 时，请走 PR（CI 会注入 PR_BASE_SHA）')
        print('FAIL: 门禁改动段校验无法完成（fail-closed）。')
        return 1

    # issue #324 时点边界与 issue #338 现取正文共用同一次 GET pulls/<n>。
    cache = {}
    stamp = pr_created_at(number, cache) or base
    gated = gate_applies(stamp)
    if gated:
        print(f'  gate 独立复核闸门生效（基线 / PR 创建于 {stamp[:10]}，不早于 {APPROVED_AT[:10]}）')
    elif datetime.now(timezone.utc) < parse_time(APPROVED_AT):
        print(f'  gate 独立复核闸门尚未生效（上界 {APPROVED_AT[:10]} 未到），只提示')
    else:
        print(f'  gate 独立复核闸门不适用（基线 / PR 创建早于 {APPROVED_AT[:10]} 或不可得），只提示')
    if head != 'HEAD':
        print(f'  info 对照 head={head}')

    body, source, reason = body_from_args(args, number, cache)
    if body is not None:
        print(f'  info 正文来源：{source}')
    elif declared and not number and touched:
        # push 事件：有 base 可算改动集，但没有 PR、因此没有正文可读。
        # 合并进 slim/main 的 push 天然落在这里（改动已经过 PR 闸门），照常放行；
        # 直接推到受保护分支以外要留痕，但通行与否由分支保护决定，不在本门禁伪判。
        print(f'  note 非 PR 上下文（GATE_CHANGES=1）：改动集含 {len(touched)} 个门禁链改动，'
              '无正文可读，按已过 PR 闸门放行')
        for path in touched:
            print(f'     门禁链改动：{path}')
        print('     PR 前的首推请走 PR：正文带 `## 门禁改动` 与独立复核结论')
        print('PASS: 非 PR 上下文，门禁链改动的披露由 PR 闸门负责。')
        return 0

    code, lines = check(touched, body, gated=gated)
    if body is None and reason:
        # issue #338：现取失败时 check() 照旧判红（不拿事件快照兜底），这里补上原因，
        # 让「权限没开」「网络不通」与「正文真没写」在同一条日志里可分。
        lines.insert(-1, f'     正文取不到：{reason}')
    for line in lines:
        print(line)
    return code


if __name__ == '__main__':
    sys.exit(main())
