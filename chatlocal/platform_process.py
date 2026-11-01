"""Process identities and owned children on Linux; no shell or privilege changes."""
import copy
import os
from pathlib import Path
import signal
import subprocess
import sys

# The backend can run without a console (pythonw, a service wrapper), and a
# console child of a console-less parent gets a fresh console window -- the black
# box issue #39 is about. Every spawn in this package goes through one of the two
# helpers below so the flag is written once, not once per call site.
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# The flag above governs whether a console is *allocated*; it does not govern
# whether one is *shown* (issue #319, measured 2026-11-01). The display state is
# the other half, and it is what removes "which console program is this" from the
# question entirely:
SW_HIDE = 0
STARTF_USESHOWWINDOW = getattr(subprocess, 'STARTF_USESHOWWINDOW', 0)


def _hidden_startupinfo(startupinfo=None):
    """A STARTUPINFO asking for a hidden window, or None off Windows.

    Copies rather than shares the caller's struct: two concurrent spawns must not
    write the show-state into one object, and a caller that set `dwFlags` or
    `lpDesktop` keeps it. Every other field is left as it was.
    """
    if os.name != 'nt':
        return startupinfo
    info = copy.copy(startupinfo) if startupinfo is not None else subprocess.STARTUPINFO()
    info.dwFlags |= STARTF_USESHOWWINDOW
    info.wShowWindow = SW_HIDE
    return info


def _hidden_kwargs(kwargs):
    """`kwargs` with both halves of the window suppression applied.

    `startupinfo` is overwritten rather than defaulted: `setdefault` would keep
    the caller's struct as-is, and a caller that had one would then get the flag
    without the show-state -- the exact hole issue #319 left open.
    """
    kwargs['creationflags'] = kwargs.get('creationflags', 0) | NO_WINDOW
    kwargs['startupinfo'] = _hidden_startupinfo(kwargs.get('startupinfo'))
    return kwargs


def linux_identity(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        root = Path('/proc') / str(pid)
        if root.stat().st_uid != os.getuid():
            return None
        fields = (root/'stat').read_text().rsplit(')', 1)[1].split()
        return dict(pid=pid, created=fields[19], executable=os.readlink(root/'exe'),
                    boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    except (OSError, ValueError, IndexError):
        return None


def linux_processes():
    for path in Path('/proc').iterdir():
        if path.name.isdecimal() and (identity := linux_identity(int(path.name))):
            yield identity


def run_hidden(args, **kwargs):
    """`subprocess.run` for a one-shot console child: no window of its own.

    Parsers and account detectors are children we only wait for, so they need the
    flags but not the ownership wrapper. POSIX passes creationflags=0 and no
    startupinfo, which the stdlib accepts.
    """
    return subprocess.run(args, **_hidden_kwargs(kwargs))


def owned_popen(args, *, memory_bytes=0, **kwargs):
    if os.name == 'nt':
        return subprocess.Popen(args, **_hidden_kwargs(kwargs))
    wrapper = Path(__file__).with_name('posix_child.py')
    # Isolate this stdlib-only bootstrap: its directory contains collections.py,
    # which must never shadow Python's collections during interpreter startup.
    return subprocess.Popen([sys.executable, '-I', str(wrapper), '--parent', str(os.getpid()),
                             '--memory', str(memory_bytes), '--', *map(str, args)],
                            start_new_session=True, **kwargs)


def terminate_group(process):
    """Only accepts our still-owned Popen object; never a persisted PID."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
