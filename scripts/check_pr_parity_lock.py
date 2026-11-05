"""parity 跨进程互斥锁（issue #348）：两个 parity 同时活着一度会互相抽掉对方的 pristine 树。

现场：同机连跑 3 次 parity，2 次整片红，失败名单里每一条都是
`can't open file '.../idlejot-parity-XXXX/pristine/scripts/<gate>.py'`——文件不是被门禁
删的，是**整棵树还在跑的时候被从底下抽走了**。红多少条取决于哪一刻被抽走（17 条 /
87 条是同一个因的两种表现）。

锁保护的是「建树 → 跑门禁 → 收树」这一整段：拿到锁才建树，所以同一台机器上任何时刻
最多只有一份 parity 在跑。为什么必须跨进程、而不能是脚本里的一个计数器：两个 worktree
各 push 一次、或者一边 push 一边手跑，是两个**独立进程**。

为什么要独立成模块：锁的位置只能有一个出处，`check_pr_parity.py` 与门禁
`check_pr_parity_isolation`（#378 删除档起为包）都从这里取，不在两处各写一份路径规则。`.githooks/pre-push`
**不持这把锁**：它逐个 commit 调 `check_pr_parity.py`，那几次调用自己在同一条队上排好；
钩子若先持锁再去起子进程，父子会互等同一把锁直到超时。

Windows 上没有 `fcntl`（本机实测 `import fcntl` → ModuleNotFoundError），`mkdir` 又会在
进程被硬杀后留下死目录，所以用 `msvcrt.locking` 锁一段字节：持锁进程被 `taskkill /F`
时 Windows 关闭句柄、字节锁自动释放；这与 `wg_images._locked()`（issue #228）同一个取舍，
`commands.py` 那套 `O_EXCL` 是「单实例占位」，被硬杀后会留死锁文件要手删。

    python scripts/check_pr_parity_lock.py --hold --ref <sha>  # 持锁直到本进程被杀（排查/门禁夹具）
    python scripts/check_pr_parity_lock.py --serialize        # 等锁空出来再返回（脚本串联）
    python scripts/check_pr_parity_lock.py --probe            # 只读：有人拿着返回 1
    python scripts/check_pr_parity_lock.py --self-test        # 逐项自检（不碰网络、不建树）

`check_pr_parity.py` 在建树前拿到这把锁、收树后才放，所以同一台机器上任何时刻只有一份
parity 在建树/跑门禁/收树；两个会话各自 push 时，排的是同一条队。

`_lock()` 只把「别人拿着」（`EACCES` / `EDEADLK`）当占用；区域锁不被支持（`ENOLCK` /
`EINVAL` / `ENOTSUP`）时**报错退出**，不混进自旋——否则那种机器上 `blocking=True` 会空转到
天荒地老，症状和「有人一直拿着」一模一样。`acquire(blocking=True)` 等到
`ACQUIRE_CAP_SECONDS` 还没拿到就说清「等满了」并返回 None，不无限挂着。

`--hold` 的释放在**进程退出**时发生（`taskkill` / Ctrl-C / 调用方主动收掉），不靠 stdin：
句柄被继承给子进程时，父进程关掉 stdin 并不会让子进程那边的读返回 EOF，锁就一直在手里
（issue #348 复核第 ③ 条）。

环境变量：`IDLEJOT_PARITY_HOME` 改锁所在目录（默认系统临时目录）；`IDLEJOT_PARITY_LOCK`
直接指定锁文件（多机共享 temp 时才需要）——两者都是测试夹具用的，运行时不设。
"""
import argparse
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import ctypes
except ImportError:  # pragma: no cover - Windows-only 工具，这里只防静态检查
    ctypes = None

try:
    import msvcrt
except ImportError:  # 非 Windows：parity 本身 Windows-only，这里明确报错而不是静默降级
    msvcrt = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

HOME_ENV = 'IDLEJOT_PARITY_HOME'
LOCK_ENV = 'IDLEJOT_PARITY_LOCK'
LOCK_NAME = 'idlejot-parity.lock'
HOLDER_NAME = 'idlejot-parity.holder'
# 锁文件里那段被锁住的字节写的是持有者；1 字节放不下 pid，所以锁整段 8 字节。
LOCK_BYTES = 8
POLL = 0.5
# `acquire(blocking=True)` 最多等这么久。锁本该在「建树 + 跑门禁」那个量级内放开
# （本机全量 parity 约 3–4 分钟），等过这个数的更可能是「持有者活着但卡死」——
# 无限自旋分不清「在排队」和「卡住了」，等满就明说，调用方照旧按失败处理。
ACQUIRE_CAP_SECONDS = 3600.0
# 「别人拿着」的 errno：msvcrt 对已被锁住的区域报 EACCES（13），同进程重入报 EDEADLK。
BUSY_ERRNOS = frozenset({errno.EACCES, errno.EDEADLK})


def lock_dir():
    """锁所在目录：`IDLEJOT_PARITY_HOME` 优先，否则系统临时目录。"""
    return Path(os.environ.get(HOME_ENV) or tempfile.gettempdir())


def lock_path():
    """锁文件路径：`IDLEJOT_PARITY_LOCK` 优先，否则在 `lock_dir()` 下用 `LOCK_NAME`。"""
    override = os.environ.get(LOCK_ENV)
    return Path(override) if override else lock_dir() / LOCK_NAME


def holder_path():
    """持有者记录路径：与锁文件同目录（换了锁位置就跟着换）。"""
    return lock_path().with_name(HOLDER_NAME)


class Holder:
    """持有者记录（pid + ref + 起始时刻）。

    `msvcrt` 的字节锁读不出「谁拿着」，而排队时要能说清「在等谁」，所以另写一个
    小文件。它是**提示性**的：真伪以字节锁为准——记录可能是上一轮硬杀留下的，
    `recorded_holder()` 会核对 pid 是否还在，对不上就当没有。
    """

    def __init__(self, payload):
        self.pid = int(payload.get('pid') or 0)
        self.ref = str(payload.get('ref') or '')
        self.at = float(payload.get('at') or 0)

    def describe(self):
        who = f'pid={self.pid}' if self.pid else 'pid 未知'
        since = time.strftime('%H:%M:%S', time.localtime(self.at)) if self.at else '时刻未知'
        return f'{who}（ref={self.ref or "?"}，自 {since}）'


def _pid_alive(pid):
    """进程还活着吗——已退出但句柄没关的僵尸也算死。

    `os.kill(pid, 0)` 在 Windows 上对**残留的僵尸句柄**照样返回成功，
    于是「刚被别人 `Popen` 起来又退出的 pid」会被当成活着，死记录永远清不掉
    （issue #348 复核时自检第 4 项实测到）。这里再问一次 Win32：
    句柄开不出来是死；开得出来但退出码已是 `STILL_ACTIVE` 以外则也是死。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    kernel32 = getattr(ctypes, 'windll', None) if ctypes is not None else None
    if kernel32 is None:
        return True
    handle = kernel32.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.kernel32.CloseHandle(handle)


def recorded_holder():
    """别人留下的持有者记录；没有或已死返回 None（死记录顺手清掉）。"""
    path = holder_path()
    try:
        holder = Holder(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError):
        return None
    if not holder.pid or not _pid_alive(holder.pid):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return holder


def _lock(path):
    """拿锁：拿到返回 fd，**别人拿着**返回 None；别的失败（区域锁不支持）直接抛。

    把「占用」与「这台机器根本不支持字节区域锁」分开，是 issue #348 复核的第 ③ 条：
    原先一律返回 None，于是 `blocking=True` 会拿一条永远不会变的前提出来自旋。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, LOCK_BYTES)
    except OSError as error:
        os.close(fd)
        if error.errno in BUSY_ERRNOS:
            return None
        raise RuntimeError(
            f'无法在 {path} 上建立跨进程锁（errno={error.errno}: {error.strerror}）——'
            '该文件系统不支持字节区域锁，parity 无法保证两处现场互不干扰'
        ) from error
    return fd


def acquire(path=None, *, blocking=False, on_wait=None, sleep=time.sleep,
            monotonic=time.monotonic, cap=ACQUIRE_CAP_SECONDS):
    """拿锁：成功返回 fd（调用方负责 `release`）；非阻塞拿不到返回 None。

    `blocking=True` 时自旋到拿到为止，最多 `cap` 秒；等满仍拿不到返回 None 并说明。
    `on_wait` 只在**第一次**发现有人在用时叫一次，免得排队期间刷屏。
    `sleep` / `monotonic` / `cap` 可注入（门禁与自检用它免掉真实等待）。
    """
    if msvcrt is None:
        raise RuntimeError('跨进程锁需要 Windows（msvcrt）；parity 是 Windows-only 工具')
    target = Path(path or lock_path())
    fd = _lock(target)
    if fd is not None or not blocking:
        return fd
    if on_wait is not None:
        on_wait(recorded_holder())
    deadline = monotonic() + cap
    while fd is None:
        if monotonic() >= deadline:
            print(f'parity: 等锁已满 {cap:.0f}s 仍未拿到（{target}），按失败处理', file=sys.stderr)
            return None
        sleep(POLL)
        fd = _lock(target)
    return fd


def release(fd, path=None):
    """释放锁并清掉持有者记录；重复释放不炸。"""
    if fd is None:
        return
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, LOCK_BYTES)
    except OSError:
        pass
    finally:
        os.close(fd)
    # 记录是本进程写的；只删自己那条，免得误删刚接手等待者的记录（advisory，真伪仍以字节锁为准）。
    record = Path(path or lock_path()).with_name(HOLDER_NAME)
    try:
        holder = Holder(json.loads(record.read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError):
        return
    if holder.pid != os.getpid():
        return
    try:
        record.unlink()
    except OSError:
        pass


def write_holder(ref):
    """写下「现在是本进程拿着锁」的记录（提示性，锁本身才是判据）。"""
    path = holder_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps({'pid': os.getpid(), 'ref': ref, 'at': time.time()}),
                        encoding='utf-8')
    except OSError:
        pass


@contextmanager
def held(ref='', on_wait=None):
    """整段持有；`ref` 写进持有者记录（排队的人看得见在等哪次推送）。"""
    path = lock_path()
    fd = acquire(path, blocking=True, on_wait=on_wait)
    try:
        write_holder(ref)
        yield path
    finally:
        release(fd, path)


def wait(seconds, opener=None, sleep=time.sleep, monotonic=time.monotonic):
    """等锁最多 `seconds` 秒；拿到返回 fd，等满没有返回 None。

    `opener` 可注入（门禁用它把时钟与「拿没拿到」攥在手里），默认 `acquire`。
    """
    opener = opener or (lambda: acquire(lock_path()))
    deadline = monotonic() + seconds
    while True:
        fd = opener()
        if fd is not None:
            return fd
        if monotonic() >= deadline:
            return None
        sleep(POLL)


def describe_wait(holder):
    """排队时给人看的一行：说清在等谁，拿不到细节也不编。"""
    if holder is None:
        return '另一个 parity 正拿着锁，排队等它收工…'
    return f'另一个 parity 正拿着锁（{holder.describe()}），排队等它收工…'


def main(argv=None):
    parser = argparse.ArgumentParser(description='parity 跨进程互斥锁（issue #348）')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--hold', action='store_true',
                       help='拿住锁直到本进程被杀（给门禁/排查当持锁夹具；不是钩子路径）')
    group.add_argument('--serialize', action='store_true',
                       help='只确认锁现在空闲（拿得到即返回 0，拿不到打印持有者并返回 1；'
                            '默认 --timeout 0 = 立即判定，要排队请显式给 --timeout）')
    group.add_argument('--probe', action='store_true', help='只读：有人拿着返回 1，否则 0')
    group.add_argument('--self-test', action='store_true', help='逐项自检')
    parser.add_argument('--timeout', type=float, default=0.0,
                        help='等锁上限秒数（--serialize 默认 0：立即判定）')
    parser.add_argument('--ref', default='', help='写进持有者记录的 ref（给人看）')
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()
    if msvcrt is None:
        print('FAIL: 跨进程锁需要 Windows（msvcrt）', file=sys.stderr)
        return 2
    if args.probe:
        fd = acquire()
        if fd is None:
            holder = recorded_holder()
            print(f'held：{lock_path()}·{holder.describe() if holder else "持有者未知"}')
            return 1
        release(fd)
        recorded_holder()  # 顺手清掉死记录：probe 说 free 时别留一条假的「有人在用」
        print(f'free：{lock_path()}')
        return 0
    if args.serialize:
        fd = wait(args.timeout)
        if fd is None:
            holder = recorded_holder()
            who = holder.describe() if holder else '持有者未知'
            print(f'FAIL: 另一个 parity 还拿着锁（{lock_path()}·{who}）', file=sys.stderr)
            return 1
        release(fd)
        print(f'ok：{lock_path()} 空闲')
        return 0
    if not args.hold:
        parser.print_help()
        return 2
    path = lock_path()
    fd = acquire(path, blocking=True, on_wait=lambda holder: print(describe_wait(holder), file=sys.stderr))
    if fd is None:
        print(f'FAIL: 等锁超时（{path}）', file=sys.stderr)
        return 1
    try:
        write_holder(args.ref)
        # 拿到锁就报一行，调用方据此知道「锁已经在手里」可以开始做别的事，
        # 而不必去猜持有者记录落盘了没有。
        print(f'holding：{path}', flush=True)
        # 释放靠进程退出（taskkill / Ctrl-C / 调用方 kill）：**不读 stdin**。
        # 句柄被继承给子进程时，父进程关 stdin 不会让子进程这边的读返回 EOF，
        # 「读到这里就结束」的前提不成立，锁会一直握在手里（issue #348 复核第 ③ 条）。
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 130
    finally:
        release(fd, path)


def _run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, encoding='utf-8', **kwargs)


def self_test():
    """逐项自检：每项都配「必须失败」的对照，只有说过的事被断言。

    用真实的 `msvcrt` 锁与真实的子进程——锁的语义（跨进程）是它唯一存在的理由，
    在同一个 fd 上自娱自乐等于没验。
    """
    if msvcrt is None:
        print('SKIP: 非 Windows（parity 本身 Windows-only）')
        return 0
    failures = []

    def expect(name, got, want):
        if got != want:
            failures.append(f'{name}：期望 {want!r}，实得 {got!r}')

    script = str(Path(__file__).resolve())
    scene = Path(tempfile.mkdtemp(prefix='parity-lock-case-'))
    previous = {key: os.environ.get(key) for key in (HOME_ENV, LOCK_ENV)}
    try:
        lock = scene / LOCK_NAME
        holder = scene / HOLDER_NAME
        os.environ[HOME_ENV] = str(scene)
        os.environ[LOCK_ENV] = str(lock)
        env = dict(os.environ, **{HOME_ENV: str(scene), LOCK_ENV: str(lock)})
        env.pop('PYTHONPATH', None)

        def cli(*args):
            return _run([sys.executable, script, *args], env=env)

        # 1. 没人拿着 → probe 说 free
        first = cli('--probe')
        expect('空闲时 probe rc', first.returncode, 0)
        expect('空闲时 probe 说 free', 'free' in first.stdout, True)

        # 2. 本进程拿住 → 子进程 probe 必须说 held，并说出持有者
        fd = acquire(lock)
        write_holder('deadbeef')
        occupied = cli('--probe')
        expect('有人拿着时 probe rc', occupied.returncode, 1)
        expect('有人拿着时 probe 说 held', 'held' in occupied.stdout, True)
        expect('probe 说出持有者 pid', f'pid={os.getpid()}' in occupied.stdout, True)
        expect('probe 说出 ref', 'deadbeef' in occupied.stdout, True)
        # 对照组：-serialize 立刻拿到锁时 rc=0，拿不到时 rc=1（不是「一律 0」）
        blocked = cli('--serialize')
        expect('被占着时 serialize rc', blocked.returncode, 1)
        expect('被占着时 serialize 说出持有者', 'deadbeef' in blocked.stderr, True)
        release(fd, lock)
        # 3. 释放之后：serialize 0；持有者记录不会被留下来（下一次没人拿着）
        free = cli('--serialize')
        expect('释放后 serialize rc', free.returncode, 0)
        expect('释放后持有者记录已清', holder.exists(), False)

        # 4. 死记录不算数：pid 已退出的记录写下去，probe 必须说 free 并清掉它
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        holder.write_text(json.dumps({'pid': dead.pid, 'ref': 'gone', 'at': time.time()}),
                          encoding='utf-8')
        after = cli('--probe')
        expect('死记录不冒充持有者', after.returncode, 0)
        expect('死记录被顺手清掉', holder.exists(), False)

        # 5. --hold 真的持有：子进程活着时本进程拿不到锁；收掉它（SIGTERM）后可拿。
        #    刻意**不**关 stdin：旧实现读到 stdin EOF 才放手，而句柄一旦继承给子进程，
        #    父进程关 stdin 并不会让那边读到 EOF——素材就在这里，别再把它当释放手段。
        piping = subprocess.Popen([sys.executable, script, '--hold', '--ref', 'hookref'],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, text=True, encoding='utf-8')
        try:
            _until(lambda: 'holding' in (piping.stdout.readline() or ''))
            expect('--hold 写下了持有者记录', _holder_ref(holder), 'hookref')
            held_probe = cli('--probe')
            expect('--hold 期间 probe 说 held', held_probe.returncode, 1)
            piping.stdin.close()  # 关 stdin 不再等于放锁：下面这条正是不许放的那个前提
            expect('关 stdin 后锁仍被 Hold 握着', cli('--probe').returncode, 1)
        finally:
            if piping.poll() is None:
                terminate(piping)
        expect('--hold 结束即释放', cli('--probe').returncode, 0)

        # 6. acquire 的自旋不会无限：等满 cap 就说清并返回 None（时钟可注入，不真等 1 小时）
        def spin(cap, failure):
            """每次加锁尝试都按 `failure` 失败，返回 (结果, 累计睡眠, 尝试次数)。"""
            ticks = [0.0]
            tries = []
            module = sys.modules[__name__]
            saved = module.msvcrt.locking

            def fake_locking(fd, mode, size):
                tries.append(mode)
                raise failure

            def fake_sleep(step):
                ticks[0] += step

            module.msvcrt.locking = fake_locking
            try:
                got = acquire(lock, blocking=True, sleep=fake_sleep,
                              monotonic=lambda: ticks[0], cap=cap)
            finally:
                module.msvcrt.locking = saved
            return got, ticks[0], len(tries)

        got, waited, tries = spin(2.0, OSError(errno.EACCES, 'busy'))
        expect('被占着时等满返回 None', got, None)
        expect('等满的时长被 cap 住', 2.0 <= waited <= 2.0 + 2 * POLL, True)
        expect('等满前真的重试过', tries > 1, True)

        raised = None
        try:
            spin(2.0, OSError(errno.ENOLCK, 'no locks available'))
        except RuntimeError as error:
            raised = error
        expect('区域锁不被支持时报错而不是自旋', raised is not None, True)
        expect('报错里点名 errno', f'errno={errno.ENOLCK}' in str(raised), True)
    finally:
        shutil.rmtree(scene, ignore_errors=True)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # 6. wait() 的时钟：等满返回 None，`opener` 第二次给 fd 就返回
    def run_wait(seconds):
        calls, clock = [], [0.0]

        def opener():
            calls.append(clock[0])
            return 'fd' if len(calls) > 1 else None

        def fake_sleep(step):
            clock[0] += step

        got = wait(seconds, opener=opener, sleep=fake_sleep, monotonic=lambda: clock[0])
        return got, calls

    got, calls = run_wait(0.0)
    expect('deadline 为 0 时只试一次', (got, len(calls)), (None, 1))
    got, calls = run_wait(1.2)
    expect('第二次拿到 fd', got, 'fd')
    expect('只在 deadline 前重试过', len(calls), 2)

    if failures:
        for line in failures:
            print(f'SELF-TEST FAIL {line}')
        return 1
    print('SELF-TEST OK 互斥八项：空闲 free / 占用 held 且说出持有者 / '
          'serialize 两态相反 / 释放即清记录 / 死记录不冒充 / --hold 靠退出放锁（关 stdin 不放）/ '
          '等满 cap 返回 None / 不支持区域锁时报错不自旋 / wait 等满即返回')
    return 0


def terminate(process, seconds=20):
    """收掉 `--hold` 夹具：先 SIGTERM（走 `finally` 正常放锁），不退再 kill。

    门禁与自检都用它收持锁夹具——`--hold` 不认 stdin EOF，只认进程退出。
    """
    if process.poll() is not None:
        return process.returncode
    try:
        process.terminate()
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=seconds)
    return process.returncode


def _holder_ref(path):
    try:
        return Holder(json.loads(Path(path).read_text(encoding='utf-8'))).ref
    except (OSError, ValueError, TypeError):
        return ''


def _until(predicate, seconds=20.0, step=0.1):
    """自旋到 `predicate()` 为真；超时返回 False（自检用来等子进程写记录）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


if __name__ == '__main__':
    sys.exit(main())
