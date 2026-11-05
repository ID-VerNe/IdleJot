"""`check_pr_parity_isolation` 的回收用例（[17]–[21]，issue #378 回收两档）。

[17]/[18] 从旧 `scripts/check_pr_parity_isolation.py` 搬运：[17] 补上判据④（keep 标记）
的夹具与断言，[18] 逐字保持。[19]–[21] 是 #378 删除档（`--reclaim --yes`）与 `--keep`
标记机制的回归。
"""
import contextlib
from io import StringIO
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from ._common import (
    FAILS, LOCK_SCRIPT, PARITY_SCRIPT, ROOT, call_parity, check, forget_or_report,
    head_sha, lock, parity, patched, scene_env, wait_for,
)


def case_reclaim_report(scene):
    """17. `--reclaim` 只报告：四判据必须咬住（含 keep 标记排除），且**一条都不删**。"""
    print('\n[17] --reclaim 只报告')
    root = scene / 'reclaim-root'
    root.mkdir()
    good = root / 'idlejot-parity-good'
    good.mkdir()
    orphan = root / 'idlejot-parity-orphan'
    (orphan / 'pristine').mkdir(parents=True)
    live = root / 'idlejot-parity-live'
    (live / 'pristine' / 'scripts').mkdir(parents=True)
    (live / 'pristine' / 'scripts' / 'gates.json').write_text('{}', encoding='utf-8')
    busy = root / 'idlejot-parity-busy'
    busy.mkdir()
    kept = root / 'idlejot-parity-kept'
    kept.mkdir()
    parity.write_keep_marker(kept)
    foreign = root / 'tulpa-foreign'
    foreign.mkdir()
    script = root / 'parity-348-foreign'
    script.mkdir()
    registered = {str(orphan / 'pristine'), str(live / 'pristine')}
    usable = lambda path: (Path(path) / 'scripts' / 'gates.json').is_file()  # noqa: E731
    busy_names = {'idlejot-parity-busy'}

    candidates, problems, kept_scenes = parity.reclaim_candidates(
        root, registered=registered, usable=usable, busy=busy_names)
    try:
        names = [candidate.scene.name for candidate in candidates]
        check('符合四条判据的假现场进候选', names,
              ['idlejot-parity-good', 'idlejot-parity-orphan'])
        check('活树不进候选', 'idlejot-parity-live' in names, False)
        check('有进程引用不进候选', 'idlejot-parity-busy' in names, False)
        check('keep 现场不进候选（带 .keep 标记）', 'idlejot-parity-kept' in names, False)
        check('keep 现场回传进 kept 列表', [path.name for path in kept_scenes],
              ['idlejot-parity-kept'])
        check('tulpa- 前缀不进候选', 'tulpa-foreign' in names, False)
        check('parity-348- 前缀不进候选', 'parity-348-foreign' in names, False)
        check('扫描没有问题', problems, [])
        states = {candidate.scene.name: candidate.registration for candidate in candidates}
        check('孤儿登记标为 prunable', states.get('idlejot-parity-orphan'), 'prunable')
        check('无登记标为 none', states.get('idlejot-parity-good'), 'none')
        # 反向对照：把前缀放宽到 tulpa-* 之后，该目录必须进候选——证明①不是空转。
        widened, _, _ = parity.reclaim_candidates(
            root, prefixes=('tulpa-',), registered=registered, usable=usable, busy=busy_names)
        check('反向对照：放宽前缀后 tulpa- 目录必进候选',
              [candidate.scene.name for candidate in widened], ['tulpa-foreign'])
        check('无登记只给目录删除命令', parity.reclaim_commands(good),
              [f'Remove-Item -LiteralPath "{good}" -Recurse -Force'])
        orphan_commands = parity.reclaim_commands(orphan, orphan / 'pristine')
        check('孤儿登记先摘登记', 'git worktree remove --force' in orphan_commands[0], True)
        out = StringIO()
        with contextlib.redirect_stdout(out):
            code = parity.reclaim_report(root, registered=registered, usable=usable,
                                         busy=busy_names)
        text = out.getvalue()
        check('报告退出码 0', code, 0)
        check('报告写明未删除任何东西', '未删除任何东西' in text, True)
        check('报告打印目录删除命令', 'Remove-Item -LiteralPath' in text, True)
        check('报告提示 --keep 已知限制', '已知限制' in text, True)
        check('报告说出跳过的 keep 现场', '跳过 keep 现场' in text, True)
        check('报告后假现场目录仍在（只读）', good.is_dir(), True)
        check('报告后 keep 现场原样留着', kept.is_dir(), True)
        empty = root / 'empty-root'
        empty.mkdir()
        empty_out = StringIO()
        with contextlib.redirect_stdout(empty_out):
            empty_code = parity.reclaim_report(empty, registered=set(), usable=usable, busy=set())
        check('空根 0 候选也退 0', empty_code, 0)
        check('空根打印 0 个候选', '候选 0 个' in empty_out.getvalue(), True)
    except Exception as error:  # noqa: BLE001 — 用例自身崩了也要报出来，别让 guarded() 独自背
        FAILS.append(f'[17] --reclaim 用例中断 — {type(error).__name__}: {error}')


def case_reclaim_busy_keeps_holder(scene):
    """18. 锁被占时 `--reclaim` 退 2，且**一条都不删**——连死掉的持有者记录也不删。

    形状照复审实测：一个真进程拿着锁，持有者记录里写的是**已经死掉的 pid**（上一轮
    硬杀的形状）。修复前的 rc=2 分支调 `recorded_holder()`，它会把这条死记录 `unlink()`
    （`stale_holder_before=True → after=False`）；修复后只读记录、文件原样还在。
    """
    print('\n[18] 锁被占时 --reclaim 不删 holder')
    home = scene / 'reclaim-busy-home'
    home.mkdir()
    record = home / lock.HOLDER_NAME
    holder = subprocess.Popen([sys.executable, str(LOCK_SCRIPT), '--hold'], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=scene_env(home), text=True, encoding='utf-8')
    try:
        check('持锁进程拿到锁', wait_for(lambda: record.exists()), True)
        # 改写成死记录：旧代码正是在这条记录上触发 `unlink()`。
        record.write_text(json.dumps({'pid': 99999999, 'ref': 'stale', 'at': 0}),
                          encoding='utf-8')
        busy = subprocess.run([sys.executable, str(PARITY_SCRIPT), '--reclaim'], cwd=ROOT,
                              capture_output=True, text=True, encoding='utf-8',
                              env=scene_env(home), timeout=120)
        check('锁被占时 --reclaim rc=2', busy.returncode, 2)
        check('输出说明另一个 parity 正在跑', '另一个 parity 正在跑' in busy.stdout, True)
        check('锁被占时 holder 文件仍存在（一条都没删）', record.exists(), True)
        check('holder 内容没被改写', record.read_text(encoding='utf-8').find('stale') >= 0, True)
    finally:
        lock.terminate(holder)
    # 对照：锁空出来后同一条命令必须退 0（证明 rc=2 来自锁，不是别处失败）。
    free = subprocess.run([sys.executable, str(PARITY_SCRIPT), '--reclaim'], cwd=ROOT,
                          capture_output=True, text=True, encoding='utf-8',
                          env=scene_env(home), timeout=120)
    check('锁空出来后 --reclaim rc=0（对照）', free.returncode, 0)


def case_keep_marker_early(scene):
    """19. `--keep` 标记在跑门禁之前就已落地（#378 删除档：硬杀也拦得住回收）。"""
    print('\n[19] --keep 标记早落地')
    scratch = scene / 'idlejot-parity-keep-marker'
    scratch.mkdir()
    seen = []

    def probing_run_gates(pristine, shard, env):
        seen.append(parity.is_kept_scene(scratch))
        return 0

    with patched(run_gates=probing_run_gates, make_scratch=lambda: scratch):
        code, out, err = call_parity(SimpleNamespace(shard=None, keep=True), head_sha())
    path = scratch / 'pristine'
    try:
        check('keep 跑通', code, 0)
        check('跑门禁时标记已在盘上（硬杀也拦得住）', seen, [True])
        check('keep 现场带着标记', parity.is_kept_scene(scratch), True)
        check('普通目录没有标记', parity.is_kept_scene(scene), False)
        report = parity.cleanup(path)
        check('keep 现场仍可被 cleanup 收掉', report.ok, True)
    finally:
        forget_or_report(path)


def case_reclaim_apply(scene):
    """20. 删除档：真删候选——先摘登记再删目录；keep 现场与外来目录一格不碰；收不掉要说话。"""
    print('\n[20] --reclaim --yes 删除档')
    root = scene / 'reclaim-apply-root'
    root.mkdir()
    shell = root / 'idlejot-parity-shell'
    shell.mkdir()
    (shell / 'home').mkdir()
    prunable = root / 'idlejot-parity-prunable'
    status, pristine, _ = parity.add_worktree(head_sha(), prunable)
    kept = root / 'idlejot-parity-kept'
    kept.mkdir()
    parity.write_keep_marker(kept)
    foreign = root / 'tulpa-foreign'
    foreign.mkdir()
    registered = {str(pristine)}
    usable = lambda path: (Path(path) / 'scripts' / 'gates.json').is_file()  # noqa: E731
    busy = set()
    try:
        check('真树建好', status, parity.ADD_OK)
        shutil.rmtree(pristine)  # 只拆目录：登记成孤儿（硬杀之后的形状）
        check('登记已变孤儿', parity.is_registered(pristine), True)

        candidates, problems, kept_scenes = parity.reclaim_candidates(
            root, registered=registered, usable=usable, busy=busy)
        names = [candidate.scene.name for candidate in candidates]
        check('无登记壳与孤儿登记进候选', names,
              ['idlejot-parity-prunable', 'idlejot-parity-shell'])
        check('keep 现场不进候选', 'idlejot-parity-kept' in names, False)
        check('外来目录不进候选',
              'tulpa-foreign' in [candidate.scene.name for candidate in candidates], False)

        out, err = StringIO(), StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = parity.reclaim_report(root, apply=True, registered=registered,
                                         usable=usable, busy=busy)
        check('删除档退出码 0', code, 0)
        check('无登记壳被删', shell.exists(), False)
        check('孤儿登记的现场目录被删', prunable.exists(), False)
        check('孤儿登记被真摘除', parity.is_registered(pristine), False)
        check('keep 现场原样留着', kept.is_dir(), True)
        check('外来目录原样留着', foreign.is_dir(), True)
        check('输出报出已回收条数', '已回收' in out.getvalue(), True)

        # 收不掉要说话：注入登记摘不掉 + 目录删不掉 → 问题进 stderr、给手工命令、不计删净。
        stuck = root / 'idlejot-parity-stuck'
        stuck.mkdir()
        candidate = SimpleNamespace(scene=stuck, pristine=str(stuck / 'pristine'),
                                    registration='prunable')

        def failing_tree(report, target, label):
            report.problems.append(f'注入：{label}删不掉')

        out2, err2 = StringIO(), StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            removed2, problems2 = parity.reclaim_apply(
                [candidate],
                registration_remover=lambda path: '注入：登记摘不掉',
                tree_remover=failing_tree)
        check('删除失败不计入删净', removed2, 0)
        check('目录删不掉说出来', any('删不掉' in problem for problem in problems2), True)
        check('登记摘不掉说出来', any('登记摘不掉' in problem for problem in problems2), True)
        check('收不掉时给手工清理命令', '手工清理命令' in err2.getvalue(), True)
        check('收不掉时目录原样留着', stuck.is_dir(), True)

        # 删除循环里的复核：keep 候选与外来前缀候选必须被跳过并记账（上游判据被改坏的兜底）。
        out3, err3 = StringIO(), StringIO()
        with contextlib.redirect_stdout(out3), contextlib.redirect_stderr(err3):
            removed3, problems3 = parity.reclaim_apply(
                [SimpleNamespace(scene=kept, pristine=None, registration='none'),
                 SimpleNamespace(scene=foreign, pristine=None, registration='none')])
        check('keep 候选被复核跳过', kept.is_dir(), True)
        check('外来前缀候选被复核跳过', foreign.is_dir(), True)
        check('复核跳过都记进问题', len(problems3), 2)
        check('复核跳过不计删净', removed3, 0)
    finally:
        forget_or_report(pristine)
        shutil.rmtree(root, ignore_errors=True)


def case_reclaim_yes_end_to_end(scene):
    """21. 端到端：真进程跑 `--reclaim --yes --root` 删隔离根里的假现场；锁被占 rc=2；
    `--yes` 单用被拒；0 候选照常退 0。"""
    print('\n[21] --reclaim --yes 端到端')
    home = scene / 'reclaim-yes-home'
    home.mkdir()
    root = scene / 'reclaim-yes-root'
    root.mkdir()
    fake = root / 'idlejot-parity-e2e'
    fake.mkdir()
    (fake / 'home').mkdir()
    env = scene_env(home)

    def run_cli(*args):
        return subprocess.run([sys.executable, str(PARITY_SCRIPT), *args], cwd=ROOT,
                              capture_output=True, text=True, encoding='utf-8',
                              env=env, timeout=120)

    try:
        done = run_cli('--reclaim', '--yes', '--root', str(root))
        check('端到端 rc=0', done.returncode, 0)
        check('端到端删掉假现场', fake.exists(), False)
        check('端到端输出报已回收', '已回收' in done.stdout, True)

        again = run_cli('--reclaim', '--yes', '--root', str(root))
        check('0 候选 rc=0', again.returncode, 0)
        check('0 候选打印 0 个', '候选 0 个' in again.stdout, True)

        holder = subprocess.Popen([sys.executable, str(LOCK_SCRIPT), '--hold'], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, text=True, encoding='utf-8')
        try:
            check('持锁进程拿到锁', wait_for(lambda: (home / lock.HOLDER_NAME).exists()), True)
            busy = run_cli('--reclaim', '--yes', '--root', str(root))
            check('锁被占时 --yes rc=2', busy.returncode, 2)
            check('锁被占时说明稍后再试', '另一个 parity 正在跑' in busy.stdout, True)
        finally:
            lock.terminate(holder)

        bad = run_cli('--yes')
        check('--yes 单用被拒 rc=2', bad.returncode, 2)
        check('--yes 单用报出只与 --reclaim 连用', '只与 --reclaim 连用' in bad.stderr, True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


RECLAIM_CASES = (
    ('--reclaim 只报告', case_reclaim_report),
    ('锁被占时不删 holder', case_reclaim_busy_keeps_holder),
    ('--keep 标记早落地', case_keep_marker_early),
    ('--reclaim --yes 删除档', case_reclaim_apply),
    ('--reclaim --yes 端到端', case_reclaim_yes_end_to_end),
)
