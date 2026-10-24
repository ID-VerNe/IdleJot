"""Catch a console window at the moment it appears, and name the process behind it.

issue #39: a black box flashes on the desktop for a few hundred milliseconds, and
by the time you look there is nothing left to inspect. Logs do not record it,
process sampling does not catch it (the child lives for two seconds at most), and
the stack is idle -- so the only thing that sees it is a window hook.

What it does: `SetWinEventHook(EVENT_OBJECT_SHOW)` across the whole desktop,
filtered to the three window classes a console shows up as:

  ConsoleWindowClass              the old conhost window
  PseudoConsoleWindow             the ConPTY backing window (Windows Terminal host)
  CASCADIA_HOSTING_WINDOW_CLASS   the Windows Terminal window itself

A console child of a console-less parent gets a *new* console, so it shows up as
one of the last two; a hit therefore means "something spawned an unflagged
console program", which is exactly the shape `check_no_console_child.py` forbids
statically.

    uv run python scripts/check_no_console_child.py     # the static gate
    uv run python scripts/check_console_window.py --minutes 30
    uv run python scripts/check_console_window.py --self-test

`--self-test` is the A/B control: four modes, two of which are controls that must
flash (`visible` with no flag, `detached-cmd` with DETACHED_PROCESS) and two that
must stay silent (`hidden` with CREATE_NO_WINDOW, `nowindow-grandchild` where a
flag-less child inherits the windowless console). It opens two real windows on
purpose; those flashes are the test. Needs an interactive desktop, so it is never
part of CI -- `ci.yml` only runs the static gates.
"""
import argparse
import ctypes
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if sys.platform == 'win32':
    from ctypes import wintypes
else:  # --help must still work; main() refuses to run off Windows
    wintypes = None

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'water-group' / 'daemon'))

EVENT_OBJECT_SHOW = 0x8002
WINEVENT_OUTOFCONTEXT = 0x0000
OBJID_WINDOW = 0
PM_REMOVE = 0x0001
CONSOLE_CLASSES = frozenset({'ConsoleWindowClass',
                             'PseudoConsoleWindow',
                             'CASCADIA_HOSTING_WINDOW_CLASS'})

# How long each half of the A/B control watches for a window. A cold Windows
# Terminal takes a couple of seconds to draw, so this is generous on purpose.
PROBE_SECONDS = 10
CHILD_SECONDS = 2.5


def _user32():
    return ctypes.windll.user32  # type: ignore[attr-defined]


def window_class(hwnd):
    buffer = ctypes.create_unicode_buffer(256)
    _user32().GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def window_title(hwnd):
    buffer = ctypes.create_unicode_buffer(512)
    _user32().GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def window_pid(hwnd):
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


class ConsoleWindowWatch:
    """Desktop-wide EVENT_OBJECT_SHOW hook, filtered to console windows."""

    def __init__(self, on_window):
        if wintypes is None:
            raise RuntimeError('这个探针只能在 Windows 上跑')
        self.on_window = on_window
        self._hook = None
        # The callback must stay referenced for as long as the hook lives, or it
        # is collected and Windows calls into freed memory.
        self._callback = ctypes.WINFUNCTYPE(
            None, ctypes.c_void_p, wintypes.DWORD, wintypes.HWND,
            wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
        )(self._on_event)

    def _on_event(self, _hook, _event, hwnd, id_object, _id_child, _thread, _stamp):
        # id_object != 0 means a child object of a window (a caret, a menu item),
        # not a window being shown; those are not what flashes.
        if not hwnd or id_object != OBJID_WINDOW:
            return
        name = window_class(hwnd)
        if name in CONSOLE_CLASSES:
            self.on_window(hwnd, name)

    def start(self):
        user32 = _user32()
        user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                          type(self._callback), wintypes.DWORD,
                                          wintypes.DWORD, wintypes.DWORD]
        user32.SetWinEventHook.restype = ctypes.c_void_p
        user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]
        self._hook = user32.SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW,
                                            None, self._callback, 0, 0,
                                            WINEVENT_OUTOFCONTEXT)
        if not self._hook:
            raise RuntimeError('SetWinEventHook 失败：拿不到窗口事件')
        return self

    def stop(self):
        if self._hook:
            _user32().UnhookWinEvent(self._hook)
            self._hook = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()
        return False

    def pump(self, seconds):
        """Run this thread's message loop; the hook only fires while it turns."""
        deadline = time.monotonic() + seconds
        message = wintypes.MSG()
        user32 = _user32()
        while time.monotonic() < deadline:
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            time.sleep(0.05)


def watch(minutes):
    """Print one line per console window that appears; 0 hits is the good result."""
    seen = []

    def report(hwnd, name):
        pid = window_pid(hwnd)
        title = window_title(hwnd)
        seen.append({'pid': pid, 'class': name, 'title': title})
        print(f'SHOW {time.strftime("%H:%M:%S")} pid {pid} class {name} title {title!r}',
              flush=True)

    watcher = ConsoleWindowWatch(report)
    with watcher:
        print(f'盯 {minutes} 分钟；命中会打印 SHOW + pid。0 命中说明没有东西开窗。', flush=True)
        print('判读：PseudoConsoleWindow / CASCADIA_* = 新建控制台（本仓库的症状）；'
              'ConsoleWindowClass = 老式 conhost。', flush=True)
        print('拿到 pid 后：Get-CimInstance Win32_Process -Filter "ProcessId=<pid>" 看命令行。',
              flush=True)
        # The hook only fires while this thread pumps messages, so the wait has to
        # be a pump loop, not a sleep.
        remaining = minutes * 60
        while remaining > 0:
            chunk = min(60, remaining)
            watcher.pump(chunk)
            remaining -= chunk
    print(f'结束：{len(seen)} 次命中', flush=True)
    return 0


def console_python():
    """The console twin of whatever interpreter is running this (pythonw has none)."""
    candidate = Path(sys.executable).with_name('python.exe')
    return str(candidate) if candidate.exists() else sys.executable


PROBE_MODES = ('visible', 'hidden', 'detached-cmd', 'nowindow-grandchild')


def probe_argv(mode):
    """The command the probe starts for one mode.

    `detached-cmd` is called 'cmd' on purpose: DETACHED is what the old code used
    for every launch, and a console program is what exposes it. The grandchild
    mode exists because the property we rely on is inheritance -- a windowless
    console handed to a middle process must silence a flag-less child of it.
    """
    console = console_python()
    if mode in ('visible', 'hidden'):
        return [console, '-c', f'import time; time.sleep({CHILD_SECONDS})']
    if mode == 'detached-cmd':
        return ['cmd.exe', '/c', f'ping -n {int(CHILD_SECONDS) + 2} 127.0.0.1']
    if mode == 'nowindow-grandchild':
        inner = f'cmd.exe /c ping -n {int(CHILD_SECONDS) + 2} 127.0.0.1'
        return [console, '-c', 'import subprocess, time; '
                                f'subprocess.Popen({inner!r}); '
                                f'time.sleep({CHILD_SECONDS})']
    raise SystemExit(f'unknown probe mode: {mode}')


def probe_flags(mode):
    import winconsole

    if mode == 'visible':
        return 0
    if mode == 'detached-cmd':
        return winconsole.DETACHED
    return winconsole.NO_WINDOW


def probe(mode, out_path):
    """Child side of the A/B control: run inside pythonw, so it has no console."""
    hits = []
    watcher = ConsoleWindowWatch(lambda hwnd, name: hits.append({'pid': window_pid(hwnd),
                                                                'class': name,
                                                                'title': window_title(hwnd)}))
    with watcher:
        child = subprocess.Popen(probe_argv(mode), creationflags=probe_flags(mode))
        try:
            watcher.pump(PROBE_SECONDS)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
    Path(out_path).write_text(json.dumps({'mode': mode, 'hits': hits}), encoding='utf-8')
    return 0


def self_test():
    """A/B control: the probe must see the bug shapes and stay silent on the fix.

    Four modes, two of which are controls that *must* flash: `visible` proves the
    hook works at all, `detached-cmd` proves this machine still reproduces the old
    DETACHED bug -- if someone puts that flag back, `hidden` may still be quiet
    while `detached-cmd` keeps passing, so both controls stay in the suite.
    """
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.exists():
        print(f'SKIP: 找不到 {pythonw}，无法起一个无控制台的探针进程')
        return 2

    expected = {'visible': True, 'hidden': False,
                'detached-cmd': True, 'nowindow-grandchild': False}

    print('自测会故意闪 2 次黑框（visible / detached-cmd 两组对照），这是对照组，不是 bug。', flush=True)
    results = {}
    with tempfile.TemporaryDirectory(prefix='console-window-') as tmp:
        for mode in PROBE_MODES:
            out_path = Path(tmp) / f'{mode}.json'
            child = subprocess.Popen([str(pythonw), str(Path(__file__).resolve()),
                                      '--probe', mode, '--out', str(out_path)],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                child.wait(timeout=PROBE_SECONDS * 2 + 40)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            if not out_path.is_file():
                print(f'FAIL: {mode} 探针没有写出结果（退出码 {child.returncode}）')
                return 1
            results[mode] = json.loads(out_path.read_text(encoding='utf-8'))['hits']

    ok = True
    for mode in PROBE_MODES:
        hits = results[mode]
        if not expected[mode] and hits:
            print(f'✗ {mode} 路径仍然开窗：{hits}')
            ok = False
        elif expected[mode] and not hits:
            print(f'FAIL: {mode} 对照组 0 命中。要么没有交互桌面，要么默认终端不新建窗口；'
                  '这时的 0 命中不能证明修复有效。')
            ok = False
        elif expected[mode]:
            print(f'✓ {mode} 对照组命中 {len(hits)} 个窗口：{hits[0]["class"]} —— 探针没瞎')
        else:
            print(f'✓ {mode} 路径 0 窗口')
    print(f'\nPASS: 控制台开窗 A/B 复现通过（{len(PROBE_MODES)} 组）。' if ok
          else '\nFAIL: A/B 复现未通过。')
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description='盯桌面上的控制台窗口，抓到就打印 pid')
    parser.add_argument('--minutes', type=int, default=30, help='盯多少分钟（默认 30）')
    parser.add_argument('--self-test', action='store_true',
                        help='跑 4 组 A/B 对照复现（会故意闪两次黑框）')
    parser.add_argument('--probe', choices=PROBE_MODES, help='内部用：无控制台探针模式')
    parser.add_argument('--out', help='内部用：探针结果写到哪里')
    args = parser.parse_args()

    if sys.platform != 'win32':
        print('这个探针只在 Windows 上有意义。')
        return 2
    if args.probe:
        return probe(args.probe, args.out)
    if args.self_test:
        return self_test()
    return watch(args.minutes)


if __name__ == '__main__':
    sys.exit(main())
