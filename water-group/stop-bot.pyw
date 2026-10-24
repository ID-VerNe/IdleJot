"""Stop the water-group stack from a double-click: the sibling of start-bot.pyw.

Starting has to happen through Explorer because the agent sandbox injects
``tsbx.dll`` into every process it spawns and QQ dies of it about twenty seconds
later (see start-bot.pyw). Stopping has no such hazard -- killing a process tree
does not hand the injection to the next generation -- so this may be launched from
anywhere. It is still a ``.pyw`` so that a double-click does not flash a console
for the half second the taskkill calls take.

All of the logic lives in ``up.stop_stack()``; this file is only a shell, on
purpose. A second copy of the stop rules would drift from the one `up.py --stop`
and ``stop.ps1`` use, and the whole point of issue #11 is that there should be one.

Outcome goes to ``~/.water-group/logs/stopper.log`` -- with pythonw there is no
console, so that line is the only record of what a double-click did. Failures are
an exit code and a log line, never a message box.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent / "daemon"))
import up  # noqa: E402


def main():
    # Same reason as up.py: the host proxy intercepts loopback, and bootstrap (which
    # owns kill_stale_instances) is imported from inside the stop path.
    up.strip_proxy()
    return 0 if up.stop_stack().ok else 1


if __name__ == "__main__":
    sys.exit(main())
