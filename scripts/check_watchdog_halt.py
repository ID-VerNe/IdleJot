"""Watchdog must never halt permanently: boot timeouts retry after cool-down (#45).

Boot timeouts used to end in a permanent stop: after
MAX_CONSECUTIVE_FAILURES of them the watchdog logged "waiting for a human",
slept ten minutes and hit the very same condition again -- the counter never
reset, so QQ stayed down while the log only repeated "halt" every ten minutes
(issue #45: 1h37m of silent outage, @ and # commands dead, no wake-ups). The
crash-loop branch already had the right shape -- cool down, reset the counter,
try once more; this gate drives the real main() loop against a permanently
unhealthy NapCat and asserts the same shape on the boot-timeout path:

  1. after the halt, relaunch() must be called again (>= MAX + 2 launches) --
    stopping there is the regression;
  2. state/napcat_halt.json must exist with kind/fails/first_fail_at/halted_at,
    and the log must say "cooling down", not "waiting for a human";
  3. once healthy() answers again the marker is cleared, so status never shows
    a stale "down".

Every probe is stubbed (no socket, no QQ, no powershell): healthy() is pinned
False, relaunch() counts, and time.sleep records -- a sleep of HALT_SLEEP is a
halt. On the unpatched base this gate fails with the exact issue signature:
launches stop at MAX and the loop only ever reports halt again.

    .venv/Scripts/python.exe scripts/check_watchdog_halt.py
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_fixture_support import isolated_env

WATCHDOG = ROOT / "water-group" / "daemon" / "watchdog.py"


class _Stop(BaseException):
    """Raised by the stubs to break out of the otherwise endless loop.

    BaseException on purpose: the daemon loop wraps every pass in
    ``except Exception``, and an Exception subclass gets swallowed there as a
    "loop error" -- the loop then spins forever instead of stopping.
    """


def load_watchdog(config_path):
    """Import watchdog.py the way the supervisor launches it, config pinned."""
    spec = importlib.util.spec_from_file_location("watchdog_halt_under_test", WATCHDOG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CONFIG = config_path
    return module


def main():
    with tempfile.TemporaryDirectory(prefix="watchdog-halt-") as tmp:
        home = Path(tmp) / "home"
        config = Path(tmp) / "config.json"
        with patch.dict("os.environ", isolated_env(home), clear=True):
            config.write_text(json.dumps({"onebot_token": "gate-token"}), encoding="utf-8")
            watchdog = load_watchdog(config)

            launches = []
            halts = []
            state = {"healthy": False}

            def fake_sleep(seconds):
                if seconds == watchdog.HALT_SLEEP:
                    halts.append(seconds)
                    # The issue #45 signature: two halts with no relaunch in
                    # between means the counter never reset and the loop is
                    # parked for good.
                    if len(halts) >= 2 and len(launches) <= watchdog.MAX_CONSECUTIVE_FAILURES:
                        raise _Stop("permanent halt")
                if state["healthy"] and not watchdog.HALT_FILE.exists():
                    raise _Stop("recovered")
                if len(launches) >= watchdog.MAX_CONSECUTIVE_FAILURES + 2:
                    raise _Stop("retried after halt")

            def fake_relaunch():
                launches.append(1)
                return True

            watchdog.healthy = lambda: state["healthy"]
            watchdog.crash_dialog_pids = lambda: []
            watchdog.dead_instance_count = lambda: 0
            watchdog.owner_pid = lambda: None
            watchdog.kill_injected_instances = lambda: 1
            watchdog.relaunch = fake_relaunch

            with patch.object(watchdog.time, "sleep", fake_sleep):
                # Phase 1: five boot timeouts, then the halt. The fix must
                # relaunch afterwards; the base parks here forever.
                try:
                    watchdog.main()
                except _Stop as stop:
                    reason = str(stop)
                if reason != "retried after halt":
                    print(f"FAIL: {reason}: {len(launches)} launch(es), {len(halts)} halt(s) "
                          "-- the loop stopped retrying after halt (issue #45 regression)")
                    return 1
                if len(launches) < watchdog.MAX_CONSECUTIVE_FAILURES + 2:
                    print(f"FAIL: only {len(launches)} launches after halt, "
                          "expected the counter to reset and retry")
                    return 1

                # Phase 2: the marker must name the failure and the log must
                # describe the cool-down, not a handover to a human.
                marker = json.loads(watchdog.HALT_FILE.read_text(encoding="utf-8"))
                for field in ("kind", "fails", "first_fail_at", "halted_at", "reason"):
                    if not marker.get(field):
                        print(f"FAIL: halt marker missing {field}: {marker}")
                        return 1
                if marker["kind"] != "boot timeout" or marker["fails"] < watchdog.MAX_CONSECUTIVE_FAILURES:
                    print(f"FAIL: unexpected halt marker: {marker}")
                    return 1
                log_text = (watchdog.LOG).read_text(encoding="utf-8", errors="ignore")
                cooling = (f"halt: {watchdog.MAX_CONSECUTIVE_FAILURES} consecutive boot failures, "
                           f"cooling down {watchdog.HALT_SLEEP}s before one more try")
                if cooling not in log_text:
                    print(f"FAIL: log lacks the cool-down line, wanted: {cooling}")
                    return 1
                if "waiting for a human" in log_text:
                    print("FAIL: permanent-halt wording 'waiting for a human' is back")
                    return 1

                # Phase 3: healthy again -> marker cleared, no stale "down".
                state["healthy"] = True
                try:
                    watchdog.main()
                except _Stop as stop:
                    if str(stop) != "recovered":
                        print(f"FAIL: recovery pass ended with {stop!r} instead of clearing the marker")
                        return 1
                if watchdog.HALT_FILE.exists():
                    print("FAIL: halt marker survived recovery")
                    return 1

    print(f"PASS: boot-timeout halt cools down {watchdog.HALT_SLEEP}s then relaunches "
          f"({len(launches)} launches across {len(halts)} halt(s)); marker written with "
          "kind/fails/first_fail_at/halted_at and cleared on recovery.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
