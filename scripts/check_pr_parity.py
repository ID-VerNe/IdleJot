"""issue #61 gate: 「本地绿」必须在 CI 等价条件下复现。

背景：PR #53 正文写着本地同时通过 `check_now_clock`，CI 却连红三次在同一条断言上。
根因是本地 worktree 继承了开发机状态——`~/.water-group` 私有库、`WATER_GROUP_HOME` /
`IDLEJOT_WG_*` 环境变量、被 ignore 的 `water-group/config.json` 与 `tools/`——而 CI 是
干净 checkout。本脚本把推送前的本地复跑挪到等价条件：

1. `git worktree add --detach <临时目录> <ref>`：只有该 ref 的已跟踪文件；
2. 子进程环境剥掉全部 `IDLEJOT_WG_*` / `TULPA_WG_*` 与 `WATER_GROUP_HOME` / `PYTHONPATH`
   （前缀匹配，以后新增的开关自动排除），`WATER_GROUP_HOME` 指到临时空目录，
   `PYTHONUTF8=1` / `GRADIO_ANALYTICS_ENABLED=False` 与 CI 一致；
3. 用同一份清单执行器（`scripts/run_gates.py`，issue #59/#60）在 pristine 树里跑：
   默认全部 ci 门禁，`--shard` 只跑一片；
4. 退出码：0 全绿；1 有门禁红；2 环境问题（worktree 建不起来 / 未知分片 / 清单缺失）。

    uv run python scripts/check_pr_parity.py                 # 当前 HEAD 全量
    uv run python scripts/check_pr_parity.py --shard static  # 只跑一片
    uv run python scripts/check_pr_parity.py --ref <sha> --keep

`pre-push` 钩子对每个被推送的本地 sha 跑一次；`SKIP_PARITY=1` 显式跳过（钩子里留痕）。
CI 本身不跑本脚本：CI 已经是那个干净环境，且它需要完整 git 历史。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISOLATED_NAMES = ('WATER_GROUP_HOME', 'PYTHONPATH')
ISOLATED_PREFIXES = ('IDLEJOT_WG_', 'TULPA_WG_')
LOG_ENCODING = 'utf-8'


def run(command, cwd, env=None):
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding=LOG_ENCODING)


def output_of(result):
    return (result.stderr or result.stdout or '').strip()


def resolve_ref(ref):
    """把 ref 解析成 commit sha；解析不了返回 None。"""
    result = run(['git', 'rev-parse', '--verify', f'{ref}^{{commit}}'], ROOT)
    return result.stdout.strip() if result.returncode == 0 else None


def add_worktree(sha, scratch):
    """在临时目录里建 detached worktree；失败返回 (None, 原因)。"""
    path = scratch / 'pristine'
    result = run(['git', 'worktree', 'add', '--detach', str(path), sha], ROOT)
    if result.returncode != 0:
        return None, output_of(result)
    return path, None


def remove_worktree(path):
    run(['git', 'worktree', 'remove', '--force', str(path)], ROOT)
    run(['git', 'worktree', 'prune'], ROOT)


def clean_env(home):
    """剥掉私有库与环境开关，只留 CI 同款变量（前缀匹配，新开关自动生效）。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ISOLATED_NAMES and not key.startswith(ISOLATED_PREFIXES)
    }
    env['WATER_GROUP_HOME'] = str(home)
    env['PYTHONUTF8'] = '1'
    env['GRADIO_ANALYTICS_ENABLED'] = 'False'
    return env


def shard_problem(pristine, shard):
    """校验分片名用的是**被跑 ref 自己那份**清单；合法返回 None。"""
    manifest = pristine / 'scripts' / 'gates.json'
    executor = pristine / 'scripts' / 'run_gates.py'
    if not manifest.is_file() or not executor.is_file():
        return f'{shard or "所有分片"}：该 ref 缺少 scripts/gates.json 或 scripts/run_gates.py'
    if shard is None:
        return None
    names = [row.get('name') for row in json.loads(manifest.read_text(encoding=LOG_ENCODING)).get('shards') or []]
    if shard not in names:
        return f'未知分片 {shard!r}；该 ref 的合法值：' + '、'.join(names)
    return None


def parity_command(pristine, shard):
    command = [sys.executable, str(pristine / 'scripts' / 'run_gates.py'), 'ci']
    return command + (['--shard', shard] if shard else [])


def main():
    parser = argparse.ArgumentParser(description='CI 等价条件复跑（issue #61）')
    parser.add_argument('--ref', default='HEAD', help='要复跑的 ref（默认 HEAD）')
    parser.add_argument('--shard', help='只跑某个分片（名字取自该 ref 的 scripts/gates.json）')
    parser.add_argument('--keep', action='store_true', help='保留临时 worktree 与空数据根，打印路径供排查')
    args = parser.parse_args()

    sha = resolve_ref(args.ref)
    if sha is None:
        print(f'FAIL: 解析不了 ref {args.ref!r}（先 fetch 或用完整 sha）')
        return 2

    scratch = Path(tempfile.mkdtemp(prefix='idlejot-parity-'))
    home = scratch / 'home'
    home.mkdir()
    pristine, reason = add_worktree(sha, scratch)
    if pristine is None:
        shutil.rmtree(scratch, ignore_errors=True)
        print(f'FAIL: 建不起 pristine worktree（{reason}）')
        return 2

    try:
        problem = shard_problem(pristine, args.shard)
        if problem:
            print(f'FAIL: {problem}')
            return 2
        print(f'parity: ref {sha} → {pristine}')
        print(f'parity: WATER_GROUP_HOME={home}（空数据根，无 config.json / 私有库）')
        result = subprocess.run(parity_command(pristine, args.shard), cwd=pristine, env=clean_env(home))
        if result.returncode == 0:
            label = f'分片 {args.shard}' if args.shard else '全部分片'
            print(f'PASS: parity 通过（{label}，干净 checkout + 空数据根）')
            return 0
        print(f'FAIL: parity 未通过（{Path(pristine / "scripts" / "run_gates.py").name} 退出 {result.returncode}）')
        return 1 if result.returncode == 1 else 2
    finally:
        if args.keep:
            print(f'保留现场：worktree={pristine} home={home}')
        else:
            remove_worktree(pristine)
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
