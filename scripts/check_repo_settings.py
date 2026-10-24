"""issue #58 gate: 仓库平台侧设置只读探测（分支保护 / 合并后删分支 / 本地钩子）。

`slim` 的分支保护、`delete_branch_on_merge` 与 `core.hooksPath` 都是**平台或本机设置**，
不随 git 走，仓库里的文件无法自证；本脚本用 `gh api` / `git config` 只读复核，
任何一条不满足即 FAIL 并打印修复命令。

CI 的 `GITHUB_TOKEN` 只有 `contents: read`，读 branch protection 会 403，所以本脚本
归 `scripts/gates.json` 的 `manual` 档（维护者在合并前后本机跑）：

    python scripts/check_repo_settings.py
    python scripts/check_repo_settings.py --branch slim

退出码：0 全绿；1 设置不符；2 无法探测（`gh` 缺失 / 未登录 / 权限不足）。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_CONTEXT = 'checks'
HOOKS_DIR = '.githooks'
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
    """读 branch protection 并返回 (问题列表, 无法探测原因)。"""
    result = gh_api(f'repos/:owner/:repo/branches/{branch}/protection')
    if result is None:
        return [], 'gh 未安装或不在 PATH'
    if result.returncode != 0:
        message = api_error(result)
        if 'not protected' in message.lower():
            return [f'分支 {branch} 没有分支保护：{message}'], None
        return [], f'读不到分支保护（{message}）'
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], f'分支保护返回的不是 JSON：{result.stdout[:120]!r}'

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
    return problems, None


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


def main():
    parser = argparse.ArgumentParser(description='仓库平台侧设置只读探测（issue #58）')
    parser.add_argument('--branch', default='slim', help='受保护的分支名（默认 slim）')
    args = parser.parse_args()

    protection, protection_blocker = protection_problems(args.branch)
    delete_on_merge, delete_blocker = delete_on_merge_problems()
    hooks = hooks_problems()

    for problem in protection:
        print(f'  FAIL {problem}')
    for problem in delete_on_merge:
        print(f'  FAIL {problem}')
    for problem in hooks:
        print(f'  FAIL {problem}')

    blockers = [reason for reason in (protection_blocker, delete_blocker) if reason]
    if blockers:
        for reason in blockers:
            print(f'  跳过 {reason}')
        print('FAIL: 无法探测平台设置（gh 缺失/未登录/权限不足），设置是否合规未知')
        return 2

    if not (protection or delete_on_merge or hooks):
        print(f'  ok  分支保护：{args.branch} 的 required check {REQUIRED_CONTEXT}（strict）')
        print('  ok  禁止 force push 与删除分支')
        print('  ok  delete_branch_on_merge=true')
        print(f'  ok  core.hooksPath={HOOKS_DIR}')
        print('PASS: 仓库平台侧设置符合合并条件。')
        return 0

    if protection:
        print(PROTECTION_FIX)
    if delete_on_merge:
        print(DELETE_FIX)
    if hooks:
        print(HOOKS_FIX)
    print(f'FAIL: {len(protection) + len(delete_on_merge) + len(hooks)} 处平台设置不符')
    return 1


if __name__ == '__main__':
    sys.exit(main())
