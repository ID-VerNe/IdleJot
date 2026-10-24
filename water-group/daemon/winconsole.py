"""Make a console spawned by these scripts readable, or better, not spawn one.

Two separate Windows quirks, both of which show up as the same symptom -- a black
window full of garbage where a listener was supposed to run:

1. A console-less parent that spawns a console child gets a *fresh* console
   window for it. Every subprocess call therefore needs CREATE_NO_WINDOW, or
   this loop of short-lived children puts a black box on a timer.
   DETACHED_PROCESS looks like a stronger version of the same idea and does the
   opposite -- it removes the console instead of its window, so the child's own
   console children have to allocate a visible one. `_with_no_window` swaps it
   out; see the measurement there.

2. A fresh console uses the OEM code page (936 on a Chinese box). Our scripts
   write UTF-8, and UTF-8 bytes read as GBK are mojibake -- not "wrong Chinese",
   unreadable garbage. SetConsoleOutputCP(65001) makes the console read them as
   UTF-8. It is a no-op when stdout is a pipe, which is the common case when an
   agent is the one running us.

`reply.py --wait` used to call subprocess.run without the flag, which is where
the reported window came from. One call site per module used to carry its own
copy of the flag, so a module added later could -- and did -- miss it (issue
#39). `run_hidden` / `popen_hidden` are now the only way the daemons start a
console child; `scripts/check_no_console_child.py` fails the build if a raw
`subprocess` call reappears without the flag.

Both halves of quirk 2 have to agree, which is what `ensure_utf8_stdio` is for:
`sys.stdout.reconfigure(encoding="utf-8")` only decides which bytes Python
writes, while the console decides how those bytes are read back. Five entry
points used to call the reconfigure on its own -- UTF-8 bytes into a 936 console,
the exact mojibake in issue #4 -- so the console CP stayed at 936 forever.

There is a third quirk hiding behind the first one: a child started with
CREATE_NO_WINDOW does not write where its caller's stdout points when the caller
has no console. `run_passthrough` is the answer for the call sites that need the
child's text (issue #91); see `run_hidden` for why inheriting is not enough.
"""
import ctypes
import subprocess
import sys

# 0x08000000: create the process without a console window of its own. Absent on
# POSIX, hence the getattr.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# 0x00000008: the child gets *no console at all*. It is not a window suppressor,
# and Windows ignores CREATE_NO_WINDOW when both are set -- see _with_no_window.
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)

UTF8_CODEPAGE = 65001


def _with_no_window(kwargs):
    """Copy `kwargs` with NO_WINDOW OR-ed into whatever flags the caller wanted.

    OR, not replace, for every flag except one: DETACHED_PROCESS. That one is
    *removed* and replaced, because it means "no console at all" rather than "no
    console window":

      CREATE_NO_WINDOW   child gets a console with no window; its own console
                         children inherit that console and stay invisible.
      DETACHED_PROCESS   child gets no console, so any console program it starts
                         afterwards has to allocate a fresh one -- a real window
                         on the desktop. Measured 2026-10-23: `cmd /c ping` run
                         DETACHED flashes Windows Terminal; the same command with
                         CREATE_NO_WINDOW is silent.
      both together      Windows documents CREATE_NO_WINDOW as ignored, so the
                         DETACHED behaviour wins and the caller gets the bug it
                         was trying to avoid.

    Dropping DETACHED is safe here: it never helped a child outlive its parent
    (job objects do that, see up.py), and "do not inherit the caller's console"
    is preserved -- NO_WINDOW also starts a *new* console rather than reusing the
    parent's. Keeping it would let one missed call site keep flashing windows.
    """
    flags = kwargs.get("creationflags", 0)
    if flags & DETACHED:
        flags = (flags & ~DETACHED) | NO_WINDOW
    return {**kwargs, "creationflags": flags | NO_WINDOW}


def run_hidden(args, **kwargs):
    """`subprocess.run` for a console program: no console window of its own.

    Every daemon here may run without a console (pythonw, DETACHED_PROCESS), and
    a console child of a console-less parent gets a *fresh* console window -- the
    black box that flashes on the desktop. Redirected stdout still works; only
    the window allocation is suppressed.

    No window is not the same as output passthrough, and the flag is what makes
    the difference: CREATE_NO_WINDOW hands the child a *new* console, so when the
    caller has none, "inherit my stdout" points at that console instead of the
    caller's pipe and everything the child prints is dropped. Leave the output
    here only when nobody wants it (taskkill and friends); use `run_passthrough`
    when the child's output is the point (issue #91).
    """
    return subprocess.run(args, **_with_no_window(kwargs))


# `run_passthrough` owns these, so a caller passing one is a mistake: silently
# honouring it would either drop the text or fight over the pipe.
_OWNED_KWARGS = ('stdout', 'stderr', 'capture_output', 'text', 'encoding', 'errors')


def run_passthrough(args, **kwargs):
    """Run a console child, write its output to ours, and return its code.

    Suppressing the window and inheriting stdout are mutually exclusive when the
    caller has no console (issue #91). `listen.py` promised "with the caller's
    stdout", got a child that printed into its own fresh console instead, and
    reported a bare `WAKE` with no wake lines for four consecutive wakes.

    So the output is piped back rather than inherited: both streams into one
    pipe (keeping the child's own ordering and its tracebacks), decoded as UTF-8
    because every daemon calls `ensure_utf8_stdio`, and written out as a single
    block. One `write` rather than a print per line is deliberate -- a wake block
    read out of the log while we are still alive must never come back as "only
    WAKE", which is the symptom this whole issue is about.
    """
    for taken in _OWNED_KWARGS:
        if taken in kwargs:
            raise TypeError(f'run_passthrough owns {taken!r}; pass it to run_hidden instead')
    result = subprocess.run(args,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            encoding='utf-8',
                            # A stray byte is not worth crashing the wake path over.
                            errors='replace',
                            **_with_no_window(kwargs))
    text = result.stdout or ''
    # pythonw without a redirected stdout has none at all; the child still ran.
    if text and sys.stdout is not None:
        if not text.endswith('\n'):
            text += '\n'
        sys.stdout.write(text)
        sys.stdout.flush()
    return result.returncode


def popen_hidden(args, **kwargs):
    """`subprocess.Popen` for a child the caller keeps and reaps itself."""
    return subprocess.Popen(args, **_with_no_window(kwargs))


def use_utf8_console():
    """Point this process's console at UTF-8, if it has a console at all.

    Returns True when a console was actually retagged. Output and input code
    pages are set independently: the console is shared by every process attached
    to it, so one call here covers whatever a child writes next.
    """
    if sys.platform != "win32":
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    retagged = False
    for setter in (kernel32.SetConsoleOutputCP, kernel32.SetConsoleCP):
        try:
            retagged = bool(setter(UTF8_CODEPAGE)) or retagged
        except Exception:  # noqa: BLE001  -- a console we cannot retag is cosmetic
            pass
    return retagged


def ensure_utf8_stdio(line_buffering=False):
    """Make this process's output UTF-8 end to end: the streams *and* the console.

    `line_buffering=True` is for the scripts whose last line matters if the caller
    kills them mid-wait (reply.py --wait: a buffered "SENT" is a lost receipt).

    Safe on every host this repo runs on: no console, no tty, no `reconfigure`
    (pythonw hands us None), and non-Windows all fall through without raising.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        kwargs = {"encoding": "utf-8"}
        if line_buffering:
            # Only ever turned on, never off: an interactive stdout is already
            # line buffered and passing False would take that away.
            kwargs["line_buffering"] = True
        try:
            reconfigure(**kwargs)
        except (ValueError, OSError):
            # Detached or closed stream -- nothing left to retag.
            pass
    return use_utf8_console()
