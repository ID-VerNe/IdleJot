"""Park until there is something to do, then exit so whoever called this wakes up.

The collector writes every message to disk but only a mention wakes anyone, so
the interesting question is what to do with the quiet stretches. This script
spends them in ``watch.py --timeout`` and treats each timeout as an opening to
digest the backlog that piled up while nobody was looking.

Exiting instead of staying resident is the whole design. A background process
that never returns is invisible: it can be digesting for an hour and the agent
that started it will never know. Returning turns "something happened" into a
completion the caller actually observes.

Exit codes:
  0  there is work -- a wake was printed, or the dossier is due
  1  the event stream is unreadable, or a child could not be started
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

sys.path.insert(0, str(HERE))
import winconsole  # noqa: E402

# Same family as the window suppression: if a console does attach, retag it as
# UTF-8 (and point Python's own streams at UTF-8) so the scripts' output is not
# read through the OEM code page.
winconsole.ensure_utf8_stdio()


def python_exe():
    candidate = ROOT / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.exists() else sys.executable


def run(script, *args):
    """Run a sibling daemon script, relay its output, and return its code.

    watch.py and dossier.py are console programs, and this loop calls them every
    few minutes from a parent that has no console of its own -- each call used to
    put a console window on the desktop. winconsole.run_passthrough is that fix.

    Relaying is the other half (issue #91). CREATE_NO_WINDOW gives the child a
    console of its own, so "inherit the caller's stdout" was never true: the wake
    lines, the follow-up lines and the TIMEOUT verdict all went to that console
    and the agent saw only the bare WAKE this script printed afterwards. The
    child's text is piped back and written out here instead, in one block.
    """
    return winconsole.run_passthrough([python_exe(), str(HERE / script), *args],
                                      cwd=str(ROOT))


def main():
    parser = argparse.ArgumentParser(description="等消息；闲下来就沉淀积压，有事就退出叫人")
    parser.add_argument("--timeout", type=int, default=300, help="每轮静默等待多少秒")
    parser.add_argument("--cycles", type=int, default=0, help="最多循环几轮，0 表示不限（调试用）")
    parser.add_argument("--no-digest", action="store_true",
                        help="超时后不沉淀、不查档案，只等（给 reply.py --wait 用）")
    args = parser.parse_args()

    cycles = 0
    while True:
        cycles += 1

        rc = run("watch.py", "--timeout", str(args.timeout))
        if rc == 0:
            # watch.py already printed the wake lines and the closing WAKE, and
            # run() wrote that block out in one piece. Printing WAKE again here
            # would show two of them, which reads like two wakes (issue #91).
            return 0
        if rc != 2:
            # 1 is "stream unreadable"; anything else means the script itself broke.
            print(f"WATCH_FAILED rc={rc}")
            return 1

        if args.no_digest:
            # A caller waiting on a bounded clock needs the bound to hold. The
            # digest pass takes tens of seconds on a backlog, which turns
            # "--wait 6" into a minute and makes the timeout meaningless.
            if args.cycles and cycles >= args.cycles:
                print("NO_WORK")
                return 0
            continue

        # Timed out: the group has been quiet. Rule-based digestion is free, so it
        # runs on every quiet stretch; the model-backed pass is left to whoever
        # picks up the DOSSIER_DUE below.
        run("dossier.py", "--digest")
        if run("dossier.py", "--check") == 0:
            print("DOSSIER_DUE")
            return 0

        if args.cycles and cycles >= args.cycles:
            print("NO_WORK")
            return 0


if __name__ == "__main__":
    sys.exit(main())
