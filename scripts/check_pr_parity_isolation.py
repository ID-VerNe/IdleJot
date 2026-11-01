"""issue #348 gate: 一份 parity 的临时现场只能被它自己收掉。

现场：同机两处 parity 同时活着时，其中一边的门禁整片报
`can't open file '.../idlejot-parity-XXXX/pristine/scripts/<gate>.py'`——树在跑的过程中
被另一边从底下抽走了。原先的收尾是「`remove` 自己的 → `prune` 全仓 → `rmtree`」，
且建树前不核对登记，所以任意一边都能抽掉另一边的树。

本门禁把修法的三条分别钉住，每条都配「不修就会红」的对照：

1. **跨进程互斥**：`check_pr_parity_lock` 的字节锁真能挡住第二个**进程**；同一个进程里
   两个句柄也互斥（钩子同进程连跑两次 parity 靠的就是这一条）；
2. **只认自己那份**：登记表按规范化路径比对；收尾只 `remove` 自己那条，别人正在用的树
   一格都不许动；
3. **不静默**：收不掉的残留要说进 `Report.problems`，不许一声不响地当成功。

跑法：`python scripts/check_pr_parity_isolation.py`（本机手跑；它建真 worktree，量级与
`check_pr_parity.py` 同档，所以不在 CI 分片里跑）。
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import check_pr_parity as parity  # noqa: E402
import check_pr_parity_lock as lock  # noqa: E402

FAILS = []
LOCK_SCRIPT = SCRIPTS / 'check_pr_parity_lock.py'


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f'{name}：期望 {want!r}，实得 {got!r}')
    print(f'{"ok  " if ok else "FAIL"} {name}')


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True,
                          encoding='utf-8')


def scene_env(scene):
    env = dict(os.environ, **{lock.HOME_ENV: str(scene), lock.LOCK_ENV: str(scene / lock.LOCK_NAME)})
    env.pop('PYTHONPATH', None)
    return env


def lock_cli(scene, *args):
    return subprocess.run([sys.executable, str(LOCK_SCRIPT), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding='utf-8',
                          env=scene_env(scene), timeout=120)


def head_sha():
    return git('rev-parse', 'HEAD').stdout.strip()


def wait_for(predicate, seconds=30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return bool(predicate())


def case_lock_is_cross_process(scene):
    """1. 跨进程：一个进程持锁期间别的进程拿不到，关掉 stdin 即恢复。"""
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
        holder.stdin.close()
        holder.wait(timeout=120)
    check('持锁进程正常退出', holder.returncode, 0)
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
    check('建树状态', status, parity.ADD_OK)
    check('登记表认得返回的路径', parity.is_registered(path), True)
    check('返回的就是 pristine 目录', Path(path).name, 'pristine')
    again, same, _ = parity.add_worktree(sha, scratch)
    check('重复调用复用同一棵（不重复 add）', (again, same), ('reuse', Path(path)))
    report = parity.cleanup(path)
    check('收尾 ok', report.ok, True)
    check('收尾后登记已清', parity.is_registered(path), False)
    check('收尾后目录已清', Path(path).exists(), False)


def case_prune_spares_others(scene):
    """5. 收尾不许连坐别人正在用的树（#348 的原现场）。"""
    print('\n[5] 不连坐')
    sha = head_sha()
    status, mine, _ = parity.add_worktree(sha, scene / 'mine')
    check('自己的树建好', status, parity.ADD_OK)
    theirs = scene / 'theirs' / 'pristine'
    added = git('worktree', 'add', '--detach', str(theirs), sha)
    check('别人的树建好', added.returncode, 0)
    try:
        parity.cleanup(mine)
        check('别人的树还在盘上', (theirs / 'scripts' / 'gates.json').is_file(), True)
        check('别人的树还在登记里', parity.is_registered(theirs), True)
    finally:
        git('worktree', 'remove', '--force', str(theirs))


def case_cleanup_not_silent(scene):
    """6. 收得掉就不许报问题；登记被抹掉时必须说出来（不静默）。"""
    print('\n[6] 不静默')
    sha = head_sha()
    status, path, _ = parity.add_worktree(sha, scene / 'gone')
    check('树建好', status, parity.ADD_OK)
    clean = parity.cleanup(path)
    check('正常收尾 ok', clean.ok, True)
    check('正常收尾不报问题（对照）', clean.problems, [])
    check('正常收尾确实 remove 过', clean.removed, True)

    status, path, _ = parity.add_worktree(sha, scene / 'orphan')
    check('第二棵树建好', status, parity.ADD_OK)
    # 把登记抹掉（模拟上一轮硬杀留下的孤儿目录，或别处 prune 过）：此时 remove 走不下去，
    # 收尾必须把「登记表里没有这棵树」记进 problems，而不是静默 ok。
    git('worktree', 'remove', '--force', str(path))
    Path(path).mkdir(parents=True, exist_ok=True)
    (path / 'leftover.txt').write_text('x', encoding='utf-8')
    report = parity.cleanup(path)
    check('孤儿目录如实记账', bool(report.problems), True)
    check('孤儿目录没走 remove 路径', report.removed, False)
    check('孤儿目录仍被删掉', Path(path).exists(), False)


def guarded(name, case, scene):
    """一组用例崩了也继续跑下一组：崩在哪一组、崩成什么，都要看得见（基线对照要的是全貌）。"""
    try:
        case(scene)
    except Exception as error:  # noqa: BLE001 — 门禁要报出「这一组没跑完」，不是把栈吞掉
        FAILS.append(f'{name}：用例中断 — {type(error).__name__}: {error}')
        print(f'FAIL {name}：用例中断 — {type(error).__name__}: {error}')


def main():
    if os.name != 'nt':
        print('SKIP: parity 是 Windows-only（msvcrt）')
        return 0
    scene = Path(tempfile.mkdtemp(prefix='parity-348-'))
    try:
        for name, case in (('跨进程互斥', case_lock_is_cross_process),
                           ('同进程重入互斥', case_same_process_two_handles),
                           ('等锁串行化', case_lock_serializes_threads),
                           ('只认自己那份', case_registration),
                           ('不连坐', case_prune_spares_others),
                           ('不静默', case_cleanup_not_silent)):
            guarded(name, case, scene)
    finally:
        shutil.rmtree(scene, ignore_errors=True)
    if FAILS:
        for line in FAILS:
            print(f'FAIL {line}')
        return 1
    print('PASS parity 现场隔离六项')
    return 0


if __name__ == '__main__':
    sys.exit(main())
