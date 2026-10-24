"""issue #78 gate: 仓库平台侧设置只读探测（分支保护 / 合并后删分支 / 本地钩子 / 复核政策）。

`slim` 的分支保护、`delete_branch_on_merge` 与 `core.hooksPath` 都是**平台或本机设置**，
不随 git 走，仓库里的文件无法自证；本脚本用 `gh api` / `git config` 只读复核，
任何一条不满足即 FAIL 并打印修复命令。

issue #78 追加「门禁复核的平台侧两态政策」。门禁脚本是这套协作里唯一的第三方校验，
平台级强制（CODEOWNERS + required review）却一直缺位：`slim` 的分支保护只要求
`checks` 通过、不要求 review。但**单人维护下**建 CODEOWNERS 等价于自己给自己放行、
要求 review 则自己的 PR 拿不到非本人批准——所以政策按**可合并维护者数量**
（有 push 权限的 collaborator）分两态，两态都钉：

- 维护者 >= 2：`required_pull_request_reviews` 至少 1 人，且 `.github/CODEOWNERS`
  覆盖门禁执行链路径（`scripts/check_*.py`、`scripts/gates.json`、
  `scripts/run_gates.py`、`.github/workflows/ci.yml`）——issue #78 候选 1/2；
- 维护者 <= 1：reviews 必须为 null、CODEOWNERS 不得覆盖门禁路径——此时复核靠
  issue #62 的「不同上下文 agent + 结论贴 PR」（`check_gate_change_review.py`
  机器校验段存在性），CODEOWNERS 在没要求 review 时不产生强制、只制造虚假安全感。

第二个维护者就位时本门禁转红，并按打印的命令翻转（CODEOWNERS 模板 + protection
PUT）；翻转后平台强制生效。两态断言随维护者数量自动切换，设置漂移即红。

CI 的 `GITHUB_TOKEN` 只有 `contents: read`，读 branch protection 会 403，所以本脚本
归 `scripts/gates.json` 的 `manual` 档（维护者在合并前后本机跑）：

    python scripts/check_repo_settings.py
    python scripts/check_repo_settings.py --branch slim
    python scripts/check_repo_settings.py --self-test   # 离线 A/B 夹具（匹配器 + 两态政策）

退出码：0 全绿；1 设置不符；2 无法探测（`gh` 缺失 / 未登录 / 权限不足）。
"""
import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_CONTEXT = 'checks'
HOOKS_DIR = '.githooks'
CODEOWNERS_FILE = '.github/CODEOWNERS'
PROTECTION_FIX = (
    "重建分支保护的等价命令（PowerShell）：\n"
    "  @'\n"
    '  {"required_status_checks":{"strict":true,"contexts":["checks"]},"enforce_admins":false,'
    '"required_pull_request_reviews":null,"restrictions":null,"allow_force_pushes":false,'
    '"allow_deletions":false}\n'
    "  '@ | gh api -X PUT repos/:owner/:repo/branches/slim/protection --input -"
)
DELETE_FIX = 'gh api -X PATCH repos/:owner/:repo -F delete_branch_on_merge=true'
HOOKS_FIX = f'git config core.hooksPath {HOOKS_DIR}'
SINGLE_POLICY_FIX = (
    '单人口径下撤掉平台侧伪强制：把 required_pull_request_reviews 置回 null'
    '（用上面 PROTECTION_FIX 的命令），并删掉 .github/CODEOWNERS 里对门禁路径的覆盖'
    '（文件只覆盖非门禁路径时可保留）。复核机制是 issue #62 的'
    '「不同上下文 agent + 结论贴 PR」，不靠平台。'
)
TEAM_PROTECTION_FIX = (
    '第二个维护者就位后，把分支保护翻成平台强制（PowerShell）：\n'
    '  @\'\n'
    '  {"required_status_checks":{"strict":true,"contexts":["checks"]},"enforce_admins":false,'
    '"required_pull_request_reviews":{"required_approving_review_count":1},'
    '"restrictions":null,"allow_force_pushes":false,"allow_deletions":false}\n'
    '  \'@ | gh api -X PUT repos/:owner/:repo/branches/slim/protection --input -'
)
TEAM_CODEOWNERS_FIX = (
    '并新增 .github/CODEOWNERS（占位符换成真实 GitHub handle）：\n'
    '  /scripts/check_*.py        @<维护者一> @<第二个维护者>\n'
    '  /scripts/gates.json        @<维护者一> @<第二个维护者>\n'
    '  /scripts/run_gates.py      @<维护者一> @<第二个维护者>\n'
    '  /.github/workflows/ci.yml  @<维护者一> @<第二个维护者>'
)


def run(command):
    """跑一条只读命令，返回 CompletedProcess（文本模式，UTF-8）。"""
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8')


def gh_api(path):
    """`gh api <path>`；gh 缺失时返回 None，由调用方按「无法探测」处理。"""
    if run(['gh', '--version']).returncode != 0:
        return None
    return run(['gh', 'api', path])


def api_error(result):
    return (result.stderr or result.stdout or '').strip()


def protection_problems(branch):
    """读 branch protection 并返回 (问题列表, 无法探测原因, 原始 payload)。

    payload 供 issue #78 的复核政策断言读 `required_pull_request_reviews`；
    探测失败时 payload 为 None。
    """
    result = gh_api(f'repos/:owner/:repo/branches/{branch}/protection')
    if result is None:
        return [], 'gh 未安装或不在 PATH', None
    if result.returncode != 0:
        message = api_error(result)
        if 'not protected' in message.lower():
            return [f'分支 {branch} 没有分支保护：{message}'], None, None
        return [], f'读不到分支保护（{message}）', None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], f'分支保护返回的不是 JSON：{result.stdout[:120]!r}', None

    problems = []
    status = payload.get('required_status_checks')
    if not status:
        problems.append(f'分支 {branch} 没有 required status checks')
    else:
        contexts = status.get('contexts') or []
        if REQUIRED_CONTEXT not in contexts:
            problems.append(f'required status checks 里没有 {REQUIRED_CONTEXT}：{contexts}')
        if status.get('strict') is not True:
            problems.append('required status checks 没开 strict（分支落后 slim 时仍可合并）')
    if (payload.get('allow_force_pushes') or {}).get('enabled'):
        problems.append('允许 force push')
    if (payload.get('allow_deletions') or {}).get('enabled'):
        problems.append('允许删除分支')
    return problems, None, payload


def maintainer_problems():
    """可合并维护者（有 push 权限的 collaborator）登录名列表；读不到返回 (None, 原因)。"""
    result = gh_api('repos/:owner/:repo/collaborators?per_page=100')
    if result is None:
        return None, 'gh 未安装或不在 PATH'
    if result.returncode != 0:
        return None, f'读不到协作者列表（{api_error(result)}）'
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, '协作者列表返回的不是 JSON'
    if not isinstance(payload, list):
        return None, '协作者列表形状异常（不是数组）'
    maintainers = [
        row.get('login')
        for row in payload
        if isinstance(row, dict) and (row.get('permissions') or {}).get('push')
    ]
    return maintainers, None


def delete_on_merge_problems():
    """仓库级 delete_branch_on_merge；无法探测时返回 (问题列表, 原因)。"""
    result = gh_api('repos/:owner/:repo')
    if result is None:
        return [], 'gh 未安装或不在 PATH'
    if result.returncode != 0:
        return [], f'读不到仓库设置（{api_error(result)}）'
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], '仓库设置返回的不是 JSON'
    if payload.get('delete_branch_on_merge') is not True:
        return ['delete_branch_on_merge 为 false：合并后远端分支会残留'], None
    return [], None


def hooks_problems():
    """本地 core.hooksPath 是否装好，且目录存在。"""
    result = run(['git', 'config', 'core.hooksPath'])
    configured = (result.stdout or '').strip()
    if not configured:
        return ['core.hooksPath 未设置：pre-push 的发布门禁会静默跳过']
    if not (ROOT / configured).is_dir():
        return [f'core.hooksPath={configured} 指向的目录不存在']
    return []


def gate_paths(root=ROOT):
    """门禁执行链路径：`scripts/check_*.py` 按磁盘展开成逐个脚本，另三个是单文件。"""
    paths = [f'scripts/{path.name}' for path in sorted((root / 'scripts').glob('check_*.py'))]
    paths += ['scripts/gates.json', 'scripts/run_gates.py', '.github/workflows/ci.yml']
    return paths


def codeowners_text(root=ROOT):
    """本地 .github/CODEOWNERS 文本；不存在返回空串（即无覆盖）。"""
    path = root / CODEOWNERS_FILE
    if not path.is_file():
        return ''
    return path.read_text(encoding='utf-8', errors='replace')


def _entry_matches(token, path):
    """单条 CODEOWNERS 模式（首个空白分隔 token）是否覆盖 path。

    gitignore 风格：`/` 开头或中含 `/` 的模式锚定仓库根，否则任意层级匹配；
    模式匹配到路径的**前缀段**即算覆盖（目录模式涵盖其下所有文件）；
    开头的 `!` 是否定，由调用方按末匹配胜出处理。
    """
    pattern = token.strip()
    if pattern.startswith('!'):
        pattern = pattern[1:]
    pattern = pattern.rstrip('/')
    if not pattern:
        return False
    anchored = pattern.startswith('/') or '/' in pattern
    pattern_segments = pattern.lstrip('/').split('/')
    path_segments = path.split('/')
    starts = [0] if anchored else range(len(path_segments))
    for start in starts:
        window = path_segments[start:start + len(pattern_segments)]
        if len(window) == len(pattern_segments) and all(
            fnmatch.fnmatchcase(seg, pat) for pat, seg in zip(pattern_segments, window)
        ):
            return True
    return False


def codeowners_covers(text, paths):
    """CODEOWNERS 文本覆盖到的 path 集合（末匹配胜出，`!` 开头是否定）。"""
    covered = set()
    for raw in (text or '').splitlines():
        entry = raw.strip()
        if not entry or entry.startswith('#'):
            continue
        token = entry.split()[0]
        for path in paths:
            if _entry_matches(token, path):
                if token.startswith('!'):
                    covered.discard(path)
                else:
                    covered.add(path)
    return covered


def policy_problems(maintainers, protection, codeowners, paths):
    """issue #78 两态复核政策的问题列表（纯函数，供 --self-test 离线夹具）。

    maintainers <= 1：reviews 必须为 null、CODEOWNERS 不得覆盖门禁路径；
    maintainers >= 2：reviews 至少 1 人且 CODEOWNERS 覆盖全部门禁路径。
    """
    problems = []
    reviews = (protection or {}).get('required_pull_request_reviews')
    count = (reviews or {}).get('required_approving_review_count') or 0
    covered = codeowners_covers(codeowners, paths)
    uncovered = [path for path in paths if path not in covered]
    if maintainers >= 2:
        if not reviews or count < 1:
            problems.append(
                f'有 {maintainers} 个可合并维护者，但分支保护没要求 review'
                '（required_pull_request_reviews 缺失或人数 < 1）')
        if uncovered:
            problems.append(
                f'有 {maintainers} 个可合并维护者，但 .github/CODEOWNERS 未覆盖门禁路径：'
                + '、'.join(uncovered))
    else:
        if reviews and count >= 1:
            problems.append(
                f'只有 {maintainers} 个可合并维护者，分支保护却要求 review'
                '（自己的 PR 拿不到非本人批准，直接不可合并）')
        if covered:
            problems.append(
                f'只有 {maintainers} 个可合并维护者，.github/CODEOWNERS 却覆盖门禁路径（'
                + '、'.join(sorted(covered))
                + '）——没要求 review 时 CODEOWNERS 不产生强制，只制造虚假安全感')
    return problems


def policy_summary(maintainers, codeowners, paths):
    """全绿时的复核政策一行摘要。"""
    if maintainers >= 2:
        return (f'{maintainers} 个可合并维护者：平台强制就位'
                f'（review ≥1 人、CODEOWNERS 覆盖 {len(paths)} 个门禁路径）')
    return (f'{maintainers} 个可合并维护者：单人口径'
            '（不要求 review、CODEOWNERS 不覆盖门禁路径；复核走 #62 agent 流程）')


def policy_fix(maintainers):
    """按维护者规模给出对应的翻转/撤回命令。"""
    if maintainers >= 2:
        return TEAM_PROTECTION_FIX + '\n' + TEAM_CODEOWNERS_FIX
    return SINGLE_POLICY_FIX


MATCHER_FIXTURES = [
    # (CODEOWNERS 文本, 被判定路径, 期望覆盖集合)
    ('/scripts/check_*.py @someone', ['scripts/check_publication.py'],
     {'scripts/check_publication.py'}),
    ('/scripts/ @someone', ['scripts/gates.json', 'scripts/run_gates.py'],
     {'scripts/gates.json', 'scripts/run_gates.py'}),
    ('* @someone', ['.github/workflows/ci.yml'], {'.github/workflows/ci.yml'}),
    ('/docs/ @someone', ['scripts/gates.json'], set()),
    ('/github/ @someone', ['.github/workflows/ci.yml'], set()),
    ('# 注释\n\n/scripts/ @someone\n!scripts/check_publication.py',
     ['scripts/check_publication.py', 'scripts/gates.json'], {'scripts/gates.json'}),
    ('scripts @someone', ['scripts/gates.json'], {'scripts/gates.json'}),
    ('*.json @someone', ['scripts/gates.json', 'scripts/run_gates.py'], {'scripts/gates.json'}),
]

POLICY_FIXTURE_PATHS = (
    'scripts/check_publication.py', 'scripts/gates.json', 'scripts/run_gates.py',
    '.github/workflows/ci.yml',
)
ALL_GATES_CODEOWNERS = (
    '/scripts/check_*.py @x\n/scripts/gates.json @x\n'
    '/scripts/run_gates.py @x\n/.github/workflows/ci.yml @x'
)
POLICY_FIXTURES = [
    # (维护者数, required_pull_request_reviews, CODEOWNERS 文本, 期望是否有问题, 用例名)
    (1, None, '', False, '单人 + 无 review + 无 CODEOWNERS（现状口径）'),
    (1, None, '/scripts/ @x', True, '单人 + CODEOWNERS 覆盖门禁路径（虚假安全感）'),
    (1, {'required_approving_review_count': 1}, '', True, '单人 + 要求 review（自己 PR 不可合并）'),
    (2, {'required_approving_review_count': 1}, ALL_GATES_CODEOWNERS, False,
     '双维护者 + review≥1 + CODEOWNERS 全覆盖'),
    (2, {'required_approving_review_count': 1}, '* @x', False, '双维护者 + review≥1 + 通配覆盖'),
    (2, None, '', True, '双维护者 + 无 review + 无 CODEOWNERS'),
    (2, {'required_approving_review_count': 1}, '/docs/ @x', True,
     '双维护者 + review 但 CODEOWNERS 只覆盖 docs'),
    (2, {'required_approving_review_count': 0}, '/scripts/ @x', True, '双维护者 + review 人数 0'),
    (2, None, ALL_GATES_CODEOWNERS, True,
     '双维护者 + 无 review + CODEOWNERS 全覆盖（隔离 review 维度，独立复核追改）'),
]


def self_test():
    """离线 A/B 夹具：CODEOWNERS 匹配器 8 组 + 两态复核政策 9 组（issue #78）。

    政策夹具按「维度隔离」设计：每条断言分支至少有一组只暴露该维度的用例
    （双人态 review 断言由「全覆盖 + 无 review」隔离，混因用例不替代隔离用例）。
    """
    failures = []
    for text, paths, expected in MATCHER_FIXTURES:
        covered = codeowners_covers(text, paths)
        if covered != expected:
            failures.append(f'匹配器夹具 {text!r}：期望覆盖 {sorted(expected)}，实得 {sorted(covered)}')
    for maintainers, reviews, codeowners, expect_problem, name in POLICY_FIXTURES:
        protection = {'required_pull_request_reviews': reviews}
        problems = policy_problems(maintainers, protection, codeowners, POLICY_FIXTURE_PATHS)
        if bool(problems) != expect_problem:
            failures.append(f'政策夹具「{name}」：期望{"有" if expect_problem else "无"}问题，'
                            f'实得 {problems or "无"}')
    if failures:
        for line in failures:
            print(f'SELF-TEST FAIL {line}')
        return 1
    print('SELF-TEST OK CODEOWNERS 匹配器与两态复核政策的 A/B 夹具全部符合预期')
    return 0


def main():
    parser = argparse.ArgumentParser(description='仓库平台侧设置只读探测（issue #58 / #78）')
    parser.add_argument('--branch', default='slim', help='受保护的分支名（默认 slim）')
    parser.add_argument('--self-test', action='store_true',
                        help='离线 A/B 夹具：CODEOWNERS 匹配器 + 两态复核政策')
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    protection, protection_blocker, payload = protection_problems(args.branch)
    delete_on_merge, delete_blocker = delete_on_merge_problems()
    hooks = hooks_problems()
    maintainers, maintainer_blocker = maintainer_problems()

    for problem in protection:
        print(f'  FAIL {problem}')
    for problem in delete_on_merge:
        print(f'  FAIL {problem}')
    for problem in hooks:
        print(f'  FAIL {problem}')

    blockers = [reason for reason in (protection_blocker, delete_blocker, maintainer_blocker) if reason]
    if blockers:
        for reason in blockers:
            print(f'  跳过 {reason}')
        print('FAIL: 无法探测平台设置（gh 缺失/未登录/权限不足），设置是否合规未知')
        return 2

    paths = gate_paths()
    policy = policy_problems(len(maintainers), payload, codeowners_text(), paths)
    for problem in policy:
        print(f'  FAIL {problem}')

    if not (protection or delete_on_merge or hooks or policy):
        print(f'  ok  分支保护：{args.branch} 的 required check {REQUIRED_CONTEXT}（strict）')
        print('  ok  禁止 force push 与删除分支')
        print('  ok  delete_branch_on_merge=true')
        print(f'  ok  core.hooksPath={HOOKS_DIR}')
        print(f'  ok  复核政策：{policy_summary(len(maintainers), codeowners_text(), paths)}')
        print('PASS: 仓库平台侧设置符合合并条件。')
        return 0

    if protection:
        print(PROTECTION_FIX)
    if delete_on_merge:
        print(DELETE_FIX)
    if hooks:
        print(HOOKS_FIX)
    if policy:
        print(policy_fix(len(maintainers)))
    total = len(protection) + len(delete_on_merge) + len(hooks) + len(policy)
    print(f'FAIL: {total} 处平台设置不符')
    return 1


if __name__ == '__main__':
    sys.exit(main())
