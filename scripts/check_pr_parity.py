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

现场隔离（issue #348）：同机两处 parity 同时活着时，其中一边的门禁会整片报
`can't open file '.../pristine/scripts/<gate>.py'`——树在跑的过程中被另一边抽走了。
现在「建树 → 跑门禁 → 收树」整段持 `scripts/check_pr_parity_lock.py` 的跨进程锁，
收尾只 `remove` 自己登记过的那条路径（不再全局 `prune`），收不掉时说进 stderr。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import check_pr_parity_lock as lock

ROOT = Path(__file__).resolve().parents[1]
ISOLATED_NAMES = ('WATER_GROUP_HOME', 'PYTHONPATH')
ISOLATED_PREFIXES = ('IDLEJOT_WG_', 'TULPA_WG_')
LOG_ENCODING = 'utf-8'
ADD_OK = 'ok'


def run(command, cwd, env=None):
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding=LOG_ENCODING)


def output_of(result):
    return (result.stderr or result.stdout or '').strip()


def resolve_ref(ref):
    """把 ref 解析成 commit sha；解析不了返回 None。"""
    result = run(['git', 'rev-parse', '--verify', f'{ref}^{{commit}}'], ROOT)
    return result.stdout.strip() if result.returncode == 0 else None


def registered_worktrees():
    """本仓在 git 登记表里的 worktree 路径集合（保留 git 的写法）。"""
    result = run(['git', 'worktree', 'list', '--porcelain'], ROOT)
    if result.returncode != 0:
        return set()
    return {line[len('worktree '):] for line in result.stdout.splitlines()
            if line.startswith('worktree ')}


def _same_path(left, right):
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def is_registered(path):
    """`path` 是否还挂在 git 的 worktree 登记表上（按规范化路径比，容忍斜杠差异）。"""
    return any(_same_path(path, entry) for entry in registered_worktrees())


def add_worktree(sha, scratch):
    """在 `scratch/pristine` 建 detached worktree。

    返回 `(状态, 路径, 原因)`：`ADD_OK` 新建成；`'reuse'` 该路径已在登记表里（上一轮
    硬杀留下的），直接复用而不重复 `add`；`'fail'` 建不起来（原因在第三项）。

    返回值里的路径是**登记表里的写法**：`git` 会把 `C:\\a\\b` 之类的多余分隔符规范化，
    拿请求时的字符串去比对登记表会假阴性，后续收尾必须用这里的路径（issue #348）。
    """
    path = scratch / 'pristine'
    for entry in registered_worktrees():
        if _same_path(path, entry):
            return 'reuse', Path(entry), None
    result = run(['git', 'worktree', 'add', '--detach', str(path), sha], ROOT)
    if result.returncode != 0:
        return 'fail', None, output_of(result)
    for entry in registered_worktrees():
        if _same_path(path, entry):
            return ADD_OK, Path(entry), None
    # add 成功却查不到登记：不猜，按失败处理（收尾会再去核一次）。
    return 'fail', None, f'git worktree add 成功但登记表里没有 {path}'


def cleanup(path, *, quiet=True):
    """收掉**自己的**那棵树：先核对登记，再 `remove --force`，最后删临时目录。

    只认 `path` 这一条（issue #348 的现场就是一边收尾顺手动了另一边）。刻意**不调**
    `git worktree prune`：那是全仓范围的写操作，会把别处「目录已不在」的登记一并清掉，
    正是「抽别人的树」最容易扩散的那一步；孤儿登记留给 `git worktree list` 自己报，
    或由人工 `git worktree prune` 处理。

    收不掉时把原因写进 `Report.problems` 并（非 quiet 时）打到 stderr，不静默成功。
    """
    report = Report(path)
    path = Path(path)
    if is_registered(path):
        result = run(['git', 'worktree', 'remove', '--force', str(path)], ROOT)
        report.removed = result.returncode == 0
        if not report.removed:
            report.problems.append(f'git worktree remove 失败：{output_of(result) or "（无输出）"}')
    elif path.exists():
        # 登记没了但目录还在：没人认领，只能自己删（此处只可能落到自己的 scratch 下）。
        report.problems.append('登记表里没有这棵树，按目录清理')
    scratch = path.parent
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            report.problems.append(f'目录删不掉：{path}')
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
        if scratch.exists():
            report.problems.append(f'临时目录删不掉：{scratch}')
    report.ok = not report.problems and not path.exists() and not scratch.exists()
    if not quiet and report.problems:
        for problem in report.problems:
            print(f'parity: 收尾未完成 — {problem}', file=sys.stderr)
    return report


class Report:
    """收尾结果：`ok` 与逐条问题分开，调用方不必猜。"""

    def __init__(self, path):
        self.path = Path(path)
        self.removed = False
        self.problems = []
        self.ok = False



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

    # 建树 → 跑门禁 → 收树 整段持锁：同一台机器上任何时刻只该有一份 parity 在跑
    # （issue #348：并发的两边会互相 `remove` + `prune` 掉对方正在跑的树）。
    path = lock.lock_path()
    fd = lock.acquire(path, blocking=True,
                      on_wait=lambda holder: print(lock.describe_wait(holder), file=sys.stderr))
    try:
        lock.write_holder(args.ref if args.ref != 'HEAD' else sha)
        return _parity(args, sha)
    finally:
        lock.release(fd, path)


def _parity(args, sha):
    scratch = Path(tempfile.mkdtemp(prefix='idlejot-parity-'))
    home = scratch / 'home'
    home.mkdir()
    status, pristine, reason = add_worktree(sha, scratch)
    if status == 'fail':
        shutil.rmtree(scratch, ignore_errors=True)
        print(f'FAIL: 建不起 pristine worktree（{reason}）')
        return 2
    if status == 'reuse':
        print(f'parity: 复用了已登记的 worktree（{pristine}）')

    code = 2
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
            code = 0
        else:
            print(f'FAIL: parity 未通过（{Path(str(pristine / "scripts" / "run_gates.py")).name} '
                  f'退出 {result.returncode}）')
            code = 1 if result.returncode == 1 else 2
        return code
    finally:
        if args.keep:
            print(f'保留现场：worktree={pristine} home={home}')
        else:
            report = cleanup(pristine)
            if not report.ok:
                # 收尾失败不改跑门禁的结论，但必须说出来：静默残留正是 #348 的现场。
                for problem in report.problems:
                    print(f'parity: 收尾未完成 — {problem}', file=sys.stderr)
                print(f'parity: 残留现场 {scratch}（请手工清理）', file=sys.stderr)



if __name__ == '__main__':
    sys.exit(main())
