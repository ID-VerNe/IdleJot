"""Exec a local child with parent-death cleanup, without preexec_fn in threads."""
import argparse
import ctypes
import os
import signal
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent', type=int, required=True)
    parser.add_argument('--memory', type=int, default=0)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        return 2
    if sys.platform.startswith('linux'):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            return 125
        # The original parent might have died before prctl took effect.
        if os.getppid() != args.parent:
            return 125
    if args.memory:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (args.memory, args.memory))
    os.execvpe(command[0], command, os.environ)


if __name__ == '__main__':
    raise SystemExit(main())
