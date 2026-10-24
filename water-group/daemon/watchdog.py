"""Keep the bot QQ instance online: relaunch when the injected QQ exits or hangs.

A listening-but-silent OneBot port is the failure mode seen here, so the probe is an
HTTP get_login_info call, not a TCP connect: the port stays bound while the QQ
renderer behind it is already wedged, and only the HTTP answer tells them apart.

The launcher is started through bootstrap.ensure_napcat(), which knows both the .bat
path and the direct-injection fallback for hosts where `reg query` is unavailable.
"""
import json
import os
import socket
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
# The bridge package lives at the repo root; adding the hyphenated directory here
# instead is what made `from water_group import bootstrap` fail on every restart.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wg_daemon import logger
from wg_paths import logs_dir

# The watchdog runs without a console, so any console child it spawns (powershell,
# netstat, taskkill) gets a fresh console window -- the black box that flashes
# every time a probe runs. winconsole.run_hidden keeps them silent; we only ever
# read their redirected stdout.
import winconsole  # noqa: E402  (探针开窗抑制，一次收口)

LOG = logs_dir() / "napcat-watchdog.log"
CONFIG = ROOT / "water-group" / "config.json"
PORT = int(os.environ.get("IDLEJOT_ONEBOT_PORT") or os.environ.get("TULPA_ONEBOT_PORT", "43000"))

# A relaunch that keeps failing must slow down instead of hammering. Each attempt
# spawns a whole QQ process tree; when QQ dies during Chromium startup the children
# never come up, crashpad cannot finish the report because CrashSender.exe cannot be
# created, and the dead process objects stay in the table. Unthrottled, one broken
# install becomes hundreds of unreapable objects in an afternoon.
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("IDLEJOT_WG_MAX_FAILS") or os.environ.get("TULPA_WG_MAX_FAILS", "5"))
BACKOFF_BASE = int(os.environ.get("IDLEJOT_WG_BACKOFF") or os.environ.get("TULPA_WG_BACKOFF", "30"))
BACKOFF_MAX = 1800
ZOMBIE_LIMIT = int(os.environ.get("IDLEJOT_WG_ZOMBIE_LIMIT") or os.environ.get("TULPA_WG_ZOMBIE_LIMIT", "30"))
HALT_SLEEP = 600

# How long a freshly injected instance gets to either settle or die before the
# watchdog judges it. Health cannot answer that question: the node sidecar keeps
# replying to get_login_info for minutes after the QQ main has crashed, so a probe
# fired two seconds after launch always says "online". The crash itself lands at
# roughly twenty seconds, so the settle window has to clear that comfortably.
SETTLE_SECONDS = int(os.environ.get("IDLEJOT_WG_SETTLE") or os.environ.get("TULPA_WG_SETTLE", "60"))


def onebot_token():
    """Token for this probe, read fresh every call -- never snapshot it.

    Rotating `onebot_token` in config.json must reach the next probe, and a
    watchdog that started before the file was written must notice the token
    appear. Snapshotting at import (the old module-level TOKEN) froze the
    first read forever: a stale or empty token made every probe 401, three
    retries failed, and the watchdog read that as a wedged instance -- killing
    and re-injecting QQ on a loop while the log only said `unresponsive`.
    up.py re-reads config on every call for the same reason; both must agree.
    """
    token = (os.environ.get("IDLEJOT_ONEBOT_TOKEN") or os.environ.get("TULPA_ONEBOT_TOKEN", "")).strip()
    if token:
        return token
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")).get("onebot_token", "")
    except (OSError, ValueError):
        return ""


# issue #11 PB: 日志助手收口到 wg_daemon，[watchdog] 前缀经 tag 参数保留
log = logger(LOG, tag="watchdog")


def port_open():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.8):
            return True
    except OSError:
        return False


def healthy():
    if not port_open():
        return False
    try:
        r = httpx.post(f"http://127.0.0.1:{PORT}/get_login_info",
                       headers={"Authorization": f"Bearer {onebot_token()}"},
                       timeout=20, trust_env=False)
        return r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:  # noqa: BLE001
        return False


def dead_instance_count():
    """Count QQ process objects that are already dead but were never reaped.

    A process that dies before its Chromium children exist can leave an object in
    the table with zero threads and zero handles. It cannot be killed — there is
    nothing left to kill — and it disappears only once whatever holds the handle
    lets go, or after a reboot. Seeing them means the host, not the network, is
    what broke, so relaunching is pointless and actively harmful.
    """
    script = (
        "$n=0; Get-Process -Name QQ -ErrorAction SilentlyContinue | "
        "ForEach-Object { if ($_.Threads.Count -eq 0) { $n++ } }; $n"
    )
    try:
        out = winconsole.run_hidden(["powershell", "-NoProfile", "-NonInteractive",
                                     "-Command", script], capture_output=True, timeout=25)
        text = out.stdout.decode("utf-8", "ignore").strip()
        return int(text) if text.isdigit() else 0
    except Exception:  # noqa: BLE001
        return 0


def owner_pid():
    try:
        raw = winconsole.run_hidden(["netstat", "-ano"], capture_output=True,
                                    timeout=10).stdout or b""
        out = raw.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{PORT}") and parts[3] == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def crash_dialog_pids():
    """PIDs of QQ processes showing a "QQ has stopped working" box.

    This install is missing CrashSender.exe, so when QQ crashes its bundled
    crashpad cannot hand the report off and instead opens a modal MessageBox.
    Nobody dismisses it, so the process sits there forever holding the dialog --
    which is what the black boxes on the desktop actually were.

    The HTTP probe cannot see any of this: NapCat's node sidecar is a separate
    process and keeps answering long after the QQ main has crashed. A dialog is
    the only reliable sign that the instance behind the bridge is dead.
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lparam):
            if not u32.IsWindowVisible(hwnd):
                return True
            length = u32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(length + 1)
            u32.GetWindowTextW(hwnd, title, length + 1)
            if "stopped working" in title.value or "已停止工作" in title.value:
                pid = wintypes.DWORD()
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append(int(pid.value))
            return True

        u32.EnumWindows(visit, 0)
        return found
    except Exception:  # noqa: BLE001
        return []


def is_our_instance(pid):
    """Whether this pid is a QQ we injected, judged by its `-q <account>` switch.

    The user's own QQ runs from the desktop with no arguments at all, so a crash
    dialog belonging to it must be left alone -- killing their client because it
    happened to crash would be far worse than a stray window.
    """
    from water_group import bootstrap

    account = bootstrap.configured_account()
    if not account:
        return False
    script = f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"
    try:
        out = winconsole.run_hidden(["powershell", "-NoProfile", "-NonInteractive",
                                     "-Command", script], capture_output=True, timeout=25)
        return f"-q {account}" in out.stdout.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return False


def relaunch():
    from water_group import bootstrap

    try:
        ok, note = bootstrap.ensure_napcat()
    except Exception as exc:  # noqa: BLE001
        log(f"launch failed: {type(exc).__name__}: {exc}")
        return False
    log(f"launch -> {ok} {note}")
    return ok


def kill_injected_instances():
    """Take down every QQ instance this launcher started, whole tree.

    The port belongs to NapCat's node sidecar, not to QQ itself, so killing the
    port owner leaves the QQ main running with no bridge: a ~190 MB orphan that
    still holds the shared profile and fights whatever gets injected next. That
    is how ten of them accumulated while the log kept reporting a clean relaunch.
    """
    from water_group import bootstrap

    try:
        return bootstrap.kill_stale_instances()
    except Exception as exc:  # noqa: BLE001
        log(f"instance cleanup failed: {type(exc).__name__}: {exc}")
        return 0


def main():
    log("start")
    from water_group import bootstrap

    sandbox = bootstrap.sandbox_dll()
    if sandbox:
        log(f"WARN this process carries {sandbox}: every injected QQ dies ~20s after "
            f"launch (0xC06D007F). Restart the stack from water-group\\start-bot.pyw.")
    fails = 0
    while True:
        try:
            # Checked before the probe, and on a short cycle: a crashed instance
            # still answers on the port through its sidecar, so health alone would
            # never notice. Every second the dialog stays up is a second it is on
            # the user's screen.
            stuck = [pid for pid in crash_dialog_pids() if is_our_instance(pid)]
            if stuck:
                log(f"crash dialog from injected pid={stuck}, taking the instance down")
                kill_injected_instances()
                if fails >= MAX_CONSECUTIVE_FAILURES:
                    # Every fresh instance dies within seconds. Re-injecting on a
                    # tight loop does not fix that, it only churns the process table
                    # and burns CPU. Cool right down and try once more: resetting the
                    # counter is what makes this a slow retry instead of a permanent
                    # stop, which would leave the bot down after a transient cause.
                    log(f"halt: {fails} consecutive crashes, cooling down {HALT_SLEEP}s before one more try")
                    time.sleep(HALT_SLEEP)
                    fails = 0
                    continue
                time.sleep(2)
                relaunch()
                time.sleep(SETTLE_SECONDS)
                again = [pid for pid in crash_dialog_pids() if is_our_instance(pid)]
                if again:
                    fails += 1
                    delay = min(BACKOFF_BASE * (2 ** (fails - 1)), BACKOFF_MAX)
                    log(f"crash loop (#{fails}) pid={again}, backing off {delay}s")
                    time.sleep(delay)
                else:
                    log("settled after crash")
                    fails = 0
                continue

            if healthy():
                if fails:
                    log(f"recovered after {fails} failed boot(s)")
                fails = 0
                time.sleep(2)
                continue

            # One missed probe means nothing: NapCat stops answering while it pushes
            # a burst of events, and killing it there costs the session, the cursor
            # and the connection. Three misses spread over half a minute separate a
            # wedged instance from a busy one.
            recovered = False
            for _ in range(3):
                time.sleep(10)
                if healthy():
                    recovered = True
                    break
            if recovered:
                fails = 0
                continue

            dead = dead_instance_count()
            if dead >= ZOMBIE_LIMIT:
                log(f"halt: {dead} dead QQ objects never reaped, relaunching only adds more")
                time.sleep(HALT_SLEEP)
                continue
            if fails >= MAX_CONSECUTIVE_FAILURES:
                log(f"halt: {fails} consecutive boot failures, waiting for a human")
                time.sleep(HALT_SLEEP)
                continue

            pid = owner_pid()
            killed = kill_injected_instances()
            log(f"unresponsive pid={pid}, killed {killed} injected instance(s)")
            if killed:
                time.sleep(3)
            relaunch()
            for _ in range(40):
                time.sleep(2)
                if healthy():
                    log("online")
                    fails = 0
                    break
            else:
                fails += 1
                delay = min(BACKOFF_BASE * (2 ** (fails - 1)), BACKOFF_MAX)
                log(f"boot timeout (#{fails}), backing off {delay}s")
                time.sleep(delay)
        except Exception as exc:  # noqa: BLE001
            log(f"loop error: {type(exc).__name__}: {exc}")
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
