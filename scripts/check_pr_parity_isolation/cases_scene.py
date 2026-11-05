"""`check_pr_parity_isolation` 的现场/锁/收尾用例（[1]–[16]）。

从旧 `scripts/check_pr_parity_isolation.py` 整体搬运，断言与正文逐字保持，零重排
（#283/#285 拆包先例）。编号与打印的 `[n]` 标号保持原样，`[19]`-`[21]` 在
`cases_reclaim.py`。
"""
import contextlib
from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

from ._common import (
    LOCK_SCRIPT, ROOT, SCRIPTS,
    _alive_scene, _scene_registry, call_parity, check, forget_or_report,
    forget_registration, git, head_sha, lock, lock_cli, parity, patched,
    registry_diff, scene_env, scene_paths, scene_rel, unregister_scene, wait_for,
)


def case_lock_is_cross_process(scene):
    """1. 跨进程：一个进程持锁期间别的进程拿不到，收掉持锁进程即恢复。"""
    print('\n[1] 跨进程互斥')
    holder = subprocess.Popen([sys.executable, str(LOCK_SCRIPT), '--hold', '--ref', 'case-1'],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, cwd=ROOT, env=scene_env(scene),
                              text=True, encoding='utf-8')
    try:
        wait_for(lambda: (scene / lock.HOLDER_NAME).exists())
        busy = lock_cli(scene, '--probe')
        check('持锁期间 probe rc=1', busy.returncode, 1)
        check('probe 说出持锁者 ref', 'case-1' in busy.stdout, True)
        check('持锁期间 serialize rc=1', lock_cli(scene, '--serialize').returncode, 1)
    finally:
        # --hold 靠**进程退出**放锁（不靠 stdin EOF）：句柄继承给子进程时关 stdin 不放锁，
        # 拿「关 stdin」当释放手段就会在这里挂满 120s（issue #348 复核第 ③ 条）。
        lock.terminate(holder)
    check('持锁进程被收掉', holder.poll() is not None, True)
    # 对照：同一条命令在没人拿着时必须反过来，否则上面那句 rc=1 什么都不证明。
    check('释放后 probe rc=0（对照）', lock_cli(scene, '--probe').returncode, 0)
    check('释放后 serialize rc=0', lock_cli(scene, '--serialize').returncode, 0)


def case_same_process_two_handles(scene):
    """2. 同进程两个句柄也互斥；释放后可重拿。"""
    print('\n[2] 同进程重入互斥')
    lockfile = scene / lock.LOCK_NAME
    first = lock.acquire(lockfile)
    check('第一个句柄拿到锁', first is not None, True)
    check('第二个句柄拿不到（同一进程）', lock.acquire(lockfile), None)
    lock.release(first, lockfile)
    second = lock.acquire(lockfile)
    check('释放后第二个句柄拿得到', second is not None, True)
    lock.release(second, lockfile)


def case_lock_serializes_threads(scene):
    """3. 两个线程抢同一把锁时，临界区互不重叠（等的时候真的在等）。"""
    print('\n[3] 等锁串行化')
    lockfile = scene / lock.LOCK_NAME
    previous = {key: os.environ.get(key) for key in (lock.HOME_ENV, lock.LOCK_ENV)}
    os.environ[lock.HOME_ENV] = str(scene)
    os.environ[lock.LOCK_ENV] = str(lockfile)
    seen, guard = [], threading.Lock()
    inside = []

    def worker(name):
        with lock.held(name):
            with guard:
                seen.append(name)
                inside.append(name)
            time.sleep(0.4)
            with guard:
                inside.remove(name)

    try:
        threads = [threading.Thread(target=worker, args=(n,)) for n in ('a', 'b')]
        for thread in threads:
            thread.start()
        peak = 0
        deadline = time.monotonic() + 120
        while any(t.is_alive() for t in threads) and time.monotonic() < deadline:
            with guard:
                peak = max(peak, len(inside))
            time.sleep(0.05)
        for thread in threads:
            thread.join(timeout=120)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    check('两个线程都进了临界区', sorted(seen), ['a', 'b'])
    check('临界区最多同时一个人', peak, 1)


def case_registration(scene):
    """4. 建树前先核对登记；收尾只动自己那份。"""
    print('\n[4] 只认自己那份')
    sha = head_sha()
    scratch = scene / 'own'
    status, path, reason = parity.add_worktree(sha, scratch)
    try:
        check('建树状态', status, parity.ADD_OK)
        check('登记表认得返回的路径', parity.is_registered(path), True)
        check('返回的就是 pristine 目录', Path(path).name, 'pristine')
        again, same, _ = parity.add_worktree(sha, scratch)
        check('重复调用复用同一棵（不重复 add）', (again, same), ('reuse', Path(path)))
        report = parity.cleanup(path)
        check('收尾 ok', report.ok, True)
        check('收尾后登记已清', parity.is_registered(path), False)
        check('收尾后目录已清', Path(path).exists(), False)
    finally:
        forget_or_report(path)


def case_prune_spares_others(scene):
    """5. 收尾不许连坐别人正在用的树（#348 的原现场）。"""
    print('\n[5] 不连坐')
    sha = head_sha()
    status, mine, _ = parity.add_worktree(sha, scene / 'mine')
    theirs = scene / 'theirs' / 'pristine'
    try:
        check('自己的树建好', status, parity.ADD_OK)
        added = git('worktree', 'add', '--detach', str(theirs), sha)
        check('别人的树建好', added.returncode, 0)
        try:
            parity.cleanup(mine)
            check('别人的树还在盘上', (theirs / 'scripts' / 'gates.json').is_file(), True)
            check('别人的树还在登记里', parity.is_registered(theirs), True)
        finally:
            git('worktree', 'remove', '--force', str(theirs))
    finally:
        forget_or_report(mine)


def case_cleanup_not_silent(scene):
    """6. 收得掉就不许报问题；登记被抹掉时必须说出来（不静默）。"""
    print('\n[6] 不静默')
    sha = head_sha()
    first = second = None
    try:
        status, first, _ = parity.add_worktree(sha, scene / 'gone')
        check('树建好', status, parity.ADD_OK)
        clean = parity.cleanup(first)
        check('正常收尾 ok', clean.ok, True)
        check('正常收尾不报问题（对照）', clean.problems, [])
        check('正常收尾确实 remove 过', clean.removed, True)

        status, second, _ = parity.add_worktree(sha, scene / 'orphan')
        check('第二棵树建好', status, parity.ADD_OK)
        # 把登记抹掉（模拟上一轮硬杀留下的孤儿目录，或别处 prune 过）：此时 remove 走不下去，
        # 收尾必须把「登记表里没有这棵树」记进 problems，而不是静默 ok。
        git('worktree', 'remove', '--force', str(second))
        Path(second).mkdir(parents=True, exist_ok=True)
        (Path(second) / 'leftover.txt').write_text('x', encoding='utf-8')
        report = parity.cleanup(second)
        check('孤儿目录如实记账', bool(report.problems), True)
        check('孤儿目录没走 remove 路径', report.removed, False)
        check('孤儿目录仍被删掉', Path(second).exists(), False)
    finally:
        forget_or_report(first)
        forget_or_report(second)


def case_orphan_not_reused(scene):
    """7. 登记在、目录不在的孤儿不许被当成可复用现场（砍掉全局 prune 之后的新坑）。"""
    print('\n[7] 孤儿登记不复用')
    sha = head_sha()
    scratch = scene / 'orphan-reuse'
    orphan = control = None
    try:
        status, orphan, _ = parity.add_worktree(sha, scratch)
        check('先建一棵真树', status, parity.ADD_OK)
        # 只拆目录、留登记：正是上一轮被硬杀（或别处 prune 掉目录）之后的现场。
        # 等登记真的变成 prunable 再往下走：`rmtree` 返回后 `git worktree list` 可能还认那棵树
        # 一两个 tick（Windows 删目录是延迟生效的），不等就会看到「目录不在、但也不是孤儿」的
        # 中间态，后面那句「孤儿不许 reuse」会误红。
        shutil.rmtree(orphan)
        check('目录已拆掉但登记还在', parity.is_registered(orphan), True)
        check('登记已变成 prunable',
              wait_for(lambda: any('prunable' in fields for _, fields in scene_paths(str(scene)))), True)

        again, reused, reason = parity.add_worktree(sha, scratch)
        check('孤儿登记不许 reuse', again, 'fail')
        check('孤儿不给路径', reused, None)
        check('孤儿原因点名 prune', 'prune' in (reason or ''), True)
        # 对照：登记与目录都在时才必须 reuse（否则上面那条 fail 什么都不证明）。
        # **不跑 `git worktree prune` 来造这棵对照树**——那是全仓写操作，正是 `cleanup()`
        # 明文不做的事（`check_pr_parity.py:cleanup` docstring），在门禁里跑一遍等于把
        # 别人的孤儿登记顺手清掉。换一个 scratch 直接建第二棵真树即可。
        other = scene / 'orphan-reuse-control'
        status, control, _ = parity.add_worktree(sha, other)
        check('对照树建成', status, parity.ADD_OK)
        third, same, _ = parity.add_worktree(sha, other)
        check('真树仍在时才 reuse（对照）', (third, same), ('reuse', Path(control)))
        parity.cleanup(control)
    finally:
        # 孤儿登记目录早没了，靠 `git worktree remove --force` 逐条自摘（不 prune 全仓）。
        forget_or_report(orphan)
        forget_or_report(control)


def case_unregister_spares_others(scene):
    """9. 收尾的孤儿清理只动本现场：现场之外还有**活着**的 `idlejot-parity-*` 时必须不 prune
    （硬断言）；只剩孤儿登记时才有资格放行 prune，而环境本就不干净（旁树活着）时该分支
    打印 SKIP、不判红（issue #378 C1：环境脏不该染红）。"""
    print('\n[9] 收尾不误伤别人的登记')
    sha = head_sha()
    own = scene / 'own-orphan'
    own2 = scene / 'own-orphan-2'
    stranger_root = Path(tempfile.mkdtemp(prefix='idlejot-parity-stranger-'))
    stranger = stranger_root / 'pristine'
    stranger_dead = stranger_root / 'dead' / 'pristine'
    status, own_path, _ = parity.add_worktree(sha, own)
    try:
        check('本现场的树建好', status, parity.ADD_OK)
        shutil.rmtree(own_path)
        check('本现场的孤儿登记还在', parity.is_registered(own_path), True)
        check('本现场的登记已变成 prunable',
              wait_for(lambda: any('prunable' in fields for _, fields in scene_paths(str(scene)))), True)
        # 现场之外的「别人」：换一个不在 `scene` 下的前缀，假装是另一处 parity 留下的登记。
        added = git('worktree', 'add', '--detach', str(stranger), sha)
        check('别人的树建好', added.returncode, 0)

        # 硬断言：别人的树活着时，全仓 prune 一律不许动——本现场的孤儿必须原样留着并报出来。
        # 这一条与机器上有没有别的残留无关，任何环境都必须成立。
        leftovers, clean = unregister_scene(scene)
        check('别人的树活着时不动全仓 prune',
              sorted(scene_rel(scene, p) for p, _ in scene_paths(str(scene))), ['own-orphan/pristine'])
        check('本现场的残留被如实报出',
              sorted(scene_rel(scene, p) for p, _ in leftovers), ['own-orphan/pristine'])
        check('本现场没被判干净', clean, False)
        check('别人的活树原样留着', parity.is_registered(stranger), True)

        shutil.rmtree(stranger)  # 只拆目录，让它的登记也变成 prunable
        check('别人的孤儿登记还在', parity.is_registered(stranger), True)

        # 本现场的登记只有这一条：别的组各自在 finally 里收干净了（[7] 不再把孤儿留给本组）。
        check('本现场的登记只有这一条',
              sorted(scene_rel(scene, p) for p, _ in scene_paths(str(scene))), ['own-orphan/pristine'])

        # 「旁人只剩孤儿登记 → 放行 prune」的前提是现场之外没有活树。本机常有硬杀留下的旁树
        # （#378 对账：c_toyqdz / qj6vp4xk），此时这一支本来就到不了——打印 SKIP，不判红。
        outside_live = [path for path in _scene_registry() if _alive_scene(path, scene)]
        if outside_live:
            print('SKIP [9] 「只剩孤儿登记时放行 prune」：前置不成立'
                  f'（现场之外还有 {len(outside_live)} 棵 idlejot-parity-* 树活着：'
                  f'{[Path(path).parent.name for path in outside_live]}）')
            # 没走 prune，本现场的孤儿就地逐条自摘，给下面的对照一个干净起点。
            forget_or_report(own / 'pristine')
        else:
            # 干净前提下的覆盖：prune 谁也伤不到（本现场孤立 + 旁人只剩孤儿登记）。
            leftovers, clean = unregister_scene(scene)
            check('旁人只剩孤儿登记时放行 prune', scene_paths(str(scene)), [])
            check('prune 之后本现场判干净', clean, True)
            check('别人的孤儿登记没被这次 prune 留着', parity.is_registered(stranger), False)

        # 对照：真的活树旁人必须拦得住——否则上面那条「放行」只是「一律放行」。
        added = git('worktree', 'add', '--detach', str(stranger_dead), sha)
        check('对照用的旁人树建好', added.returncode, 0)
        status, own2_path, _ = parity.add_worktree(sha, own2)
        check('再造一条本现场的孤儿', status, parity.ADD_OK)
        shutil.rmtree(own2_path)
        check('本现场的孤儿登记已变成 prunable',
              wait_for(lambda: 'own-orphan-2/pristine'
                       in {scene_rel(scene, p) for p, f in scene_paths(str(scene))
                           if 'prunable' in f}), True)
        leftovers, clean = unregister_scene(scene)
        check('活树旁人拦住 prune（对照）',
              sorted(scene_rel(scene, p) for p, _ in scene_paths(str(scene))), ['own-orphan-2/pristine'])
        check('本现场的孤儿如实报出',
              sorted(scene_rel(scene, p) for p, _ in leftovers), ['own-orphan-2/pristine'])
        check('活树旁人原样留着（对照）', parity.is_registered(stranger_dead), True)
    finally:
        # 夹具自建自摘（issue #378 卫生）：本组造的登记逐条摘掉，不把收尾寄托给全仓 prune
        # ——旁树活着时它本来就会被拦，寄托给它就一定会留残留。
        forget_or_report(own / 'pristine')
        forget_or_report(own2 / 'pristine')
        forget_or_report(stranger)
        forget_or_report(stranger_dead)
        shutil.rmtree(stranger_root, ignore_errors=True)


def case_main_holds_lock(scene):
    """8. `main()` 真的持锁：锁被占期间绝不进入 `_parity`，放锁之后才进去。

    这是整条修复的接线处——`check_pr_parity.py:main()` 里那段 `lock.acquire(...)`。
    只验 `check_pr_parity_lock` 自身（第 1–3 组）证明不了 `main()` 用上了它：把那段
    删掉，门禁照样全绿。所以这里跑**真的 `main()`**，只把 `_parity` 换成一行记录。
    """
    print('\n[8] main() 持锁接线')
    driver = scene / 'driver.py'
    marker = scene / 'entered.txt'
    driver.write_text(
        'import pathlib, sys\n'
        f'sys.path.insert(0, {str(SCRIPTS)!r})\n'
        'import check_pr_parity as parity\n'
        f'marker = pathlib.Path({str(marker)!r})\n'
        'def fake(args, sha):\n'
        '    marker.write_text("entered", encoding="utf-8")\n'
        '    return 0\n'
        'parity._parity = fake\n'
        'sys.argv = ["check_pr_parity.py", "--ref", sys.argv[1]]\n'
        'sys.exit(parity.main())\n', encoding='utf-8')
    sha = head_sha()
    runner = subprocess.Popen([sys.executable, str(driver), sha], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=scene_env(scene), text=True, encoding='utf-8')
    holder = None
    try:
        holder = lock.acquire(scene / lock.LOCK_NAME, blocking=True)
        check('门禁自己拿着锁', holder is not None, True)
        # 锁在门禁手里时 main() 必须卡在排队上：给足让它跑完 _parity 的时间。
        time.sleep(3.0)
        check('锁被占时没进入 _parity', marker.exists(), False)
        check('锁被占时 main() 还没退出', runner.poll(), None)
    finally:
        lock.release(holder, scene / lock.LOCK_NAME)
    try:
        runner.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        runner.kill()
        runner.communicate(timeout=30)
    check('放锁后 main() 才进入 _parity（对照）', marker.exists(), True)
    check('main() 正常返回', runner.returncode, 0)


def case_cleanup_step_oserror(scene):
    """10. 收尾某步抛 `OSError` 后，后续步骤仍执行且失败被说出来（#378 缺口②）。

    夹具刻意**不建真树**：`cleanup()` 第一步（读登记表）调 `run()` 抛 `OSError`，此时
    目录还在、登记表里没有它（上一轮硬杀的形状）。修复前这里会直接抛出去，后面两次
    `rmtree` 全跳过；修复后要看到目录被删、失败原因与清理命令都打出来。
    """
    print('\n[10] 收尾单步抛错仍继续')
    scratch = scene / 'idlejot-parity-cleanup-error'
    (scratch / 'pristine').mkdir(parents=True)
    (scratch / 'home').mkdir()
    (scratch / 'pristine' / 'leftover.txt').write_text('x', encoding='utf-8')

    def exploding_run(command, cwd, env=None):
        raise OSError('注入：git 不可用')

    out, err = StringIO(), StringIO()
    with patched(run=exploding_run):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            parity._finish_scene(SimpleNamespace(keep=False), scratch, None)
    check('后续步骤仍删掉工作树目录', (scratch / 'pristine').exists(), False)
    check('后续步骤仍删掉现场目录', scratch.exists(), False)
    check('失败原因有日志', '收尾未完成' in err.getvalue(), True)
    check('日志带可复制 remove 命令', 'git worktree remove --force' in err.getvalue(), True)
    check('日志带可复制目录删除命令', 'Remove-Item -LiteralPath' in err.getvalue(), True)


def case_build_failure_no_scene(scene):
    """11. 建树中途抛错不留半截现场（#378 缺口③）。"""
    print('\n[11] 建树中途失败不留半截现场')
    scratch = scene / 'idlejot-parity-build-fail'
    scratch.mkdir()

    def exploding_add(sha, target):
        raise OSError('注入：建树中途失败')

    with patched(add_worktree=exploding_add, make_scratch=lambda: scratch):
        code, out, err = call_parity(SimpleNamespace(shard=None, keep=False), head_sha())
    check('建树抛错返回环境失败码', code, 2)
    check('半截现场已回收', scratch.exists(), False)
    check('抛错原因被说出来', '建树/跑门禁抛错' in err, True)


def case_fail_branch_no_orphan(scene):
    """12. `fail` 分支先摘登记再删目录，不留孤儿登记（#378 缺口④）。"""
    print('\n[12] fail 分支不留孤儿登记')
    scratch = scene / 'idlejot-parity-fail-branch'
    scratch.mkdir()
    real_add = parity.add_worktree

    def add_then_fail(sha, target):
        status, path, reason = real_add(sha, target)
        if status == parity.ADD_OK:
            return 'fail', None, '注入：add 成功但按失败处理'
        return status, path, reason

    with patched(add_worktree=add_then_fail, make_scratch=lambda: scratch):
        code, out, err = call_parity(SimpleNamespace(shard=None, keep=False), head_sha())
    path = scratch / 'pristine'
    try:
        check('fail 分支返回环境失败码', code, 2)
        check('登记已摘掉', parity.is_registered(path), False)
        check('登记表里没有本现场条目', [p for p, _ in scene_paths(str(scratch))], [])
        check('目录已删', path.exists(), False)
        check('现场已删', scratch.exists(), False)
        check('说出建树失败', 'FAIL: 建不起 pristine worktree' in out, True)
    finally:
        forget_or_report(path)


def case_keep_prints_commands(scene):
    """13. `--keep` 打印可复制清理命令，而不只是路径（#378 缺口）。"""
    print('\n[13] --keep 打印清理命令')
    scratch = scene / 'idlejot-parity-keep'
    scratch.mkdir()
    with patched(run_gates=lambda pristine, shard, env: 0, make_scratch=lambda: scratch):
        code, out, err = call_parity(SimpleNamespace(shard=None, keep=True), head_sha())
    path = scratch / 'pristine'
    try:
        check('keep 跑通', code, 0)
        check('keep 打印现场路径', '保留现场' in out, True)
        check('keep 打印 worktree remove 命令', 'git worktree remove --force' in out, True)
        check('keep 打印目录删除命令', 'Remove-Item -LiteralPath' in out, True)
        check('keep 命令含确切现场路径', str(scratch) in out, True)
        report = parity.cleanup(path)
        check('keep 现场可被 cleanup 收掉', report.ok, True)
        check('keep 现场登记已清', parity.is_registered(path), False)
    finally:
        forget_or_report(path)


def case_cleanup_scope(scene):
    """14. 清理只认 `idlejot-parity-` 前缀，绝不碰人工工作树（#378 安全约束）。"""
    print('\n[14] 清理只认 idlejot-parity- 前缀')
    check('本工具现场被认', parity.is_parity_scene(Path('idlejot-parity-abc')), True)
    for foreign in ('.tmp/wt-378', 'tulpa-parity-abc', 'parity-348-abc'):
        check(f'非本工具目录不认：{foreign}', parity.is_parity_scene(Path(foreign)), False)
        raised = None
        try:
            parity.cleanup_commands(Path(foreign))
        except ValueError as error:
            raised = error
        check(f'拒绝为非现场生成清理命令：{foreign}', raised is not None, True)
    commands = parity.cleanup_commands(Path('idlejot-parity-abc'))
    check('本工具现场给出 remove 命令',
          any('git worktree remove --force' in line for line in commands), True)
    check('本工具现场给出目录删除命令',
          any('Remove-Item -LiteralPath' in line for line in commands), True)


def case_forget_registration(scene):
    """15. 孤儿登记逐条自摘：只摘自己那一条，旁人活树一格都不碰（#378 卫生部分）。"""
    print('\n[15] 孤儿登记逐条自摘')
    sha = head_sha()
    orphan = neighbour = None
    try:
        status, orphan, _ = parity.add_worktree(sha, scene / 'forget-orphan')
        check('孤儿树建好', status, parity.ADD_OK)
        shutil.rmtree(orphan)
        check('孤儿登记还在', parity.is_registered(orphan), True)
        status, neighbour, _ = parity.add_worktree(sha, scene / 'forget-neighbour')
        check('旁人树建好', status, parity.ADD_OK)
        reason = forget_registration(orphan)
        check('孤儿登记摘得掉（不 prune 全仓）', reason, None)
        check('孤儿登记已不在表里', parity.is_registered(orphan), False)
        check('旁人活树的登记一格没动', parity.is_registered(neighbour), True)
        check('旁人活树的目录一格没动',
              (Path(neighbour) / 'scripts' / 'gates.json').is_file(), True)
    finally:
        forget_or_report(orphan)
        forget_or_report(neighbour)


def case_registry_snapshot_diff(scene):
    """16. 登记表快照对比：多一条/少一条/条目变化都必须报出差异（#378 回归断言的自检）。"""
    print('\n[16] 登记表快照对比')
    same = (('a', 'b'), ('x', 'y'))
    check('相同快照无差异', registry_diff(same, same), [])
    check('多一条 worktree 有差异',
          bool(registry_diff(same, (('a', 'b', 'c'), ('x', 'y')))), True)
    check('少一条 worktree 有差异',
          bool(registry_diff(same, (('a',), ('x', 'y')))), True)
    check('.git/worktrees 条目变化有差异',
          bool(registry_diff(same, (('a', 'b'), ('x', 'y', 'z')))), True)


SCENE_CASES = (
    ('跨进程互斥', case_lock_is_cross_process),
    ('同进程重入互斥', case_same_process_two_handles),
    ('等锁串行化', case_lock_serializes_threads),
    ('只认自己那份', case_registration),
    ('不连坐', case_prune_spares_others),
    ('不静默', case_cleanup_not_silent),
    ('孤儿登记不复用', case_orphan_not_reused),
    ('main() 持锁接线', case_main_holds_lock),
    ('收尾不误伤别人的登记', case_unregister_spares_others),
    ('收尾单步抛错仍继续', case_cleanup_step_oserror),
    ('建树中途失败不留现场', case_build_failure_no_scene),
    ('fail 分支不留孤儿登记', case_fail_branch_no_orphan),
    ('--keep 打印清理命令', case_keep_prints_commands),
    ('清理只认前缀', case_cleanup_scope),
    ('孤儿登记逐条自摘', case_forget_registration),
    ('登记表快照对比', case_registry_snapshot_diff),
)
