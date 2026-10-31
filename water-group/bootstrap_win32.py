import os
import sys
import time
import json
import socket
import subprocess
from pathlib import Path

# bootstrap_win32.py 是 bootstrap.py 三拆（issue #277 R1）的低层工具叶子：win32
# 沙箱/崩溃框抑制 + 无窗口进程派生 + 端口/解释器探针。零耦合，不依赖 bootstrap
# 其余部分，是 bootstrap_napcat / bootstrap_tulpa / bootstrap 的共同基座（DAG 叶子）。
# 用以避免「bootstrap_napcat 反向 import bootstrap」造成的循环导入与脚本态双装载
# （bridge 警告：同一文件在 __main__ 与 water_group.<name> 下双装会撕裂模块级状态）。
from wg_paths import logs_dir

ROOT_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = logs_dir()

# A console child of a console-less parent gets a brand-new console window on
# Windows, which is the black box that flashes whenever the watchdog shells out.
# Every probe here runs from a background daemon with no console, so each one
# would otherwise pop a window; winconsole.run_hidden suppresses it without
# touching the redirected stdout we actually read.
import winconsole  # noqa: E402  (探针开窗抑制，一次收口)

# Win32 SetErrorMode flags. SEM_NOGPFAULTERRORBOX is the one that suppresses the
# "X has stopped working" dialog; SEM_FAILCRITICALERRORS keeps the related
# critical-error box away too.
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def get_python_exe():
    venv_py = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def silence_crash_dialogs():
    """Stop Windows from putting a "QQ has stopped working" box on the desktop.

    Every crash of an injected instance used to leave one of these behind, and
    because the watchdog re-injects on its own schedule the boxes piled up faster
    than anyone dismissed them -- ten were sitting on screen at once. Error mode
    is inherited by child processes, so setting it here covers QQ and everything
    it starts, while leaving the user's own applications reporting errors as usual.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)
    except Exception:  # noqa: BLE001
        pass


def sandbox_dll():
    """Name of the agent-sandbox DLL injected into this process, or "".

    The command sandbox injects ``tsbx.dll`` into every process it spawns, and the
    injection is inherited down the whole chain: up.py -> supervisor -> watchdog ->
    NapCatWinBootMain -> QQ.exe. QQ does not survive it. Every injected instance
    dies roughly twenty seconds after launch with 0xC06D007F -- the delay-load
    helper's ERROR_PROC_NOT_FOUND -- because the hook answers one of its imports
    with "no such export". The visible symptom is a crash dialog per instance and a
    watchdog that relaunches forever.

    Nothing in the repository can undo that, so the stack has to be started from
    Explorer (start-bot.vbs); this check exists so the logs say so instead of
    leaving the next reader to rediscover it from a stack trace.
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetModuleHandleW("tsbx.dll")
        return "tsbx.dll" if handle else ""
    except Exception:  # noqa: BLE001
        return ""


def start_process_detached(cmd, cwd=None, log_name=None, env=None):
    """Spawn a long-lived child with a windowless console, appending its raw output.

    The flag is NO_WINDOW (``winconsole.NO_WINDOW``), not DETACHED_PROCESS. Both
    keep the child from inheriting the caller's console (the 936 window from issue
    #4), but only one of them also keeps the *grandchildren* quiet:

      NO_WINDOW          the child gets its own console with no window. Anything
                         it starts later inherits that console and stays invisible
                         even without a flag of its own.
      DETACHED_PROCESS   Windows gives the child no console, and a console program
                         (cmd.exe, python.exe) allocates one on startup -- with a
                         window. Once per launch, on the desktop (issue #39's
                         residue).

    Measured 2026-10-23 with a window hook: `cmd /c ping` under DETACHED flashes
    Windows Terminal, under NO_WINDOW it is silent, and an unflagged `cmd` started
    *from* a NO_WINDOW child is silent too. A probe child reports
    `(GetConsoleCP(), GetConsoleWindow())` as `(936, 0)` under NO_WINDOW and
    `(936, <hwnd>)` under DETACHED -- the console exists either way, only the
    window differs.

    DETACHED never bought anything else here: it does not help a child outlive the
    shell that started it (job objects do, see up.py's header), and nothing in the
    launcher chain needs "no console handle" specifically.

    The log handle is opened in binary on purpose. `Popen` only takes the
    handle's `fileno()`; the child writes bare bytes into that descriptor, so
    `encoding="utf-8"` on the Python file object promised something it could not
    keep. It also lied in a way that hid a real problem: `cmd.exe` (the .bat
    launcher's echo/pause) writes 936 while NapCat writes UTF-8, so one file ends
    up with two encodings (issue #4). Read it back with
    `wg_logs.decode_log_line`, which sniffs per line.
    """
    silence_crash_dialogs()
    out_file = subprocess.DEVNULL
    opened = None
    if log_name:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        opened = open(LOGS_DIR / log_name, "ab")
        out_file = opened
    try:
        # popen_hidden owns the window flag; passing only the process-group bit
        # keeps this call site free of hand-rolled constants (see
        # scripts/check_no_console_child.py).
        return winconsole.popen_hidden(
            cmd,
            cwd=cwd or str(ROOT_DIR),
            env=env,
            stdout=out_file,
            stderr=out_file,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
    finally:
        # The child owns a duplicate of the handle; keeping ours open would leak
        # one descriptor per watchdog relaunch.
        if opened is not None:
            opened.close()
