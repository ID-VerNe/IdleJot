"""Supervise every long-lived piece of the water-group stack.

Children:
  db_keeper.py  holds SQLite WAL connections open so -shm is never yanked mid-read
  server_wg.py  the water-group backend: HTTP on 47860, MCP listener on 48777
  collector.py  holds the MCP long-poll and persists every delivered message
  commands.py   answers # keywords straight from script, no agent in the loop
  watchdog.py   keeps the injected QQ/NapCat instance answering

Everything runs as a child of one process so a single job object can own the whole
tree. The process that calls main() never returns, which is exactly what makes the
tree survive: on Windows a detached spawn still dies with the job of the shell that
created it, so the only reliable keeper is a parent that stays alive.

The blocking children are also checked through their heartbeat files: a hung
long-poll is not a dead process, so process liveness alone would hide exactly the
failure this guards against.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
import winconsole  # noqa: E402  (子进程开窗抑制，一次收口)
from wg_paths import logs_dir, state_dir
STATE_DIR = state_dir()
# 每个会阻塞等待的孩子一条心跳：卡在长轮询/WS recv 里不等于进程死了，只看进程
# 存活会漏掉正是要害的那种故障。
HEARTBEATS = {
    "collector": STATE_DIR / "wg_heartbeat",
    "commands": STATE_DIR / "wg_commands_heartbeat",
}
LOG = logs_dir() / "wg-supervisor.log"
STALE_AFTER = int(os.environ.get("IDLEJOT_WG_STALE") or os.environ.get("TULPA_WG_STALE", "300"))
PY = sys.executable

# The supervisor has no console of its own, so every child it starts would get a
# fresh console window. Restarts happen on their own schedule, which made this
# the most random-looking black box in the stack; popen_hidden suppresses it.

# Per-child log handles. Kept on the supervisor instance so the file objects are
# not garbage-collected (which would close them) between spawns and restarts.
CHILD_LOGS = {}

# Targets that failed to start repeatedly.  The reloader owns the decision and
# writes it into this set; both start_all() and supervise() must respect it or
# the old 15s loop would fight the quarantine forever.
QUARANTINED = set()

# app.py is started first in spirit, but db_keeper has no dependency and opening it
# early means the WAL sidecar is already pinned by the time the backend touches it.
CHILDREN = {
    "db_keeper": HERE / "db_keeper.py",
    "app": Path(os.environ.get("IDLEJOT_WG_SERVER") or os.environ.get("TULPA_WG_SERVER", ROOT / "server_wg.py")),
    "collector": HERE / "collector.py",
    "commands": HERE / "commands.py",
    "watchdog": HERE / "watchdog.py",
}


def sanitized_env():
    """Drop the host proxy: it intercepts 127.0.0.1 and NapCat only answers there."""
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def log(line):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def heartbeat_age(name):
    try:
        return time.time() - int(HEARTBEATS[name].read_text(encoding="utf-8").strip())
    except (OSError, ValueError, KeyError):
        return 10 ** 9


def child_log(name):
    """Return an append-mode binary handle for a child's stdout/stderr.

    Binary because `Popen` only takes the descriptor: the child writes bare
    bytes, so a text handle's `encoding` never applied to them (same trap as
    bootstrap.start_process_detached, issue #4). Children are started with
    PYTHONIOENCODING=utf-8 in sanitized_env, so the bytes are UTF-8 and a plain
    utf-8 read works; wg_logs.decode_log_line is the safe reader if that ever
    stops being true.

    The handle is cached on CHILD_LOGS so it stays open for the life of the
    supervisor; a short-lived local would be GC'd and close the file between
    restarts, hiding ImportError tracebacks in a black box.
    """
    fh = CHILD_LOGS.get(name)
    if fh is None or fh.closed:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        fh = open(LOG.parent / f"{name}.log", "ab")
        CHILD_LOGS[name] = fh
    return fh


def spawn(name, script=None):
    """Start one named child.  ``script`` is optional so the reloader can call
    ``spawn("app")`` without knowing the CHILDREN table."""
    script = script or CHILDREN[name]
    try:
        return winconsole.popen_hidden(
            [PY, str(script)], cwd=str(ROOT), env=sanitized_env(),
            stdout=child_log(name), stderr=subprocess.STDOUT,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"{name} spawn failed: {exc}")
        return None


def start_all(procs=None, only=None, quarantined=None):
    """Launch every child once, in dependency order, and hand back the pid map.

    ``only`` is used by tests and by a future partial start; the guard still
    starts everything.  Quarantined children are left out on purpose.
    """
    procs = {} if procs is None else procs
    quarantined = QUARANTINED if quarantined is None else quarantined
    names = list(CHILDREN) if only is None else [n for n in CHILDREN if n in only]
    for name in names:
        if name in quarantined:
            log(f"{name} skip (quarantined)")
            continue
        if procs.get(name) is not None and procs[name].poll() is None:
            continue
        log(f"{name} start")
        procs[name] = spawn(name)
    return procs


def supervise(procs, on_tick=None, quarantined=None):
    """Blocking watch loop. Returns only if something catastrophic happens.

    Every 2 seconds the reloader gets a chance to claim a mailbox request; the
    more expensive heartbeat check stays on its original 15 second cadence.
    There is deliberately one writer (this thread) for ``procs``.
    """
    quarantined = QUARANTINED if quarantined is None else quarantined
    last_health = 0.0
    while True:
        if on_tick is not None:
            try:
                on_tick(procs)
            except Exception as exc:  # noqa: BLE001  reload must not kill supervision
                log(f"reload tick failed: {type(exc).__name__}: {exc}")
        now = time.monotonic()
        check_health = now - last_health >= 15
        for name, script in CHILDREN.items():
            if name in quarantined:
                continue
            proc = procs.get(name)
            dead = proc is None or proc.poll() is not None
            hung = (check_health and name in HEARTBEATS and not dead
                    and heartbeat_age(name) > STALE_AFTER)
            if not (dead or hung):
                continue
            reason = "exited" if dead else f"heartbeat stale {heartbeat_age(name):.0f}s"
            if proc is not None and proc.poll() is None:
                proc.kill()
                time.sleep(2)
            log(f"{name} restart ({reason})")
            procs[name] = spawn(name, script)
        if check_health:
            last_health = now
        time.sleep(2)


def silence_crash_dialogs():
    """A crashed child would otherwise leave a "has stopped working" box on the
    desktop until someone dismisses it. Error mode is inherited, so setting it once
    here covers every child this process will ever start."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)  # FAILCRITICALERRORS | NOGPFAULTERRORBOX
    except Exception:  # noqa: BLE001
        pass


def main():
    silence_crash_dialogs()
    log(f"supervisor start, pid={os.getpid()}")
    procs = start_all()
    try:
        import reload as hot_reload

        hot_reload.write_guard_state(os.getpid(), build=hot_reload.build_stamp())
        reloader = hot_reload.Reloader(
            spawn=spawn, healthy=hot_reload.napcat_healthy, quarantined=QUARANTINED)
        return supervise(procs, on_tick=reloader.tick, quarantined=QUARANTINED)
    except Exception as exc:  # noqa: BLE001  old supervisor behavior is the fallback
        log(f"reloader unavailable: {type(exc).__name__}: {exc}")
        return supervise(procs)


if __name__ == "__main__":
    sys.exit(main())
