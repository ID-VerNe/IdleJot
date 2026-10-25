"""Park until there is something to do, then exit so whoever called this wakes up.

The collector writes every message to disk but only a mention wakes anyone, so
the interesting question is what to do with the quiet stretches. This script
spends them in ``watch.py --timeout`` and treats each timeout as an opening to
digest the backlog that piled up while nobody was looking.

Exiting instead of staying resident is the whole design. A background process
that never returns is invisible: it can be digesting for an hour and the agent
that started it will never know. Returning turns "something happened" into a
completion the caller actually observes.

Bounded, not just exiting (issue #88): one call may wait for the whole budget,
but not more -- the budget is a total shared by every round, not a fresh
allowance per round. An unbounded loop was the same silent failure as a
never-exiting process: the host cut the call at its own timeout and the whole
process tree went with it, so the listener was gone while everything still
looked fine.

That design only works if the caller knows the next step, which it used to have
to remember (issue #69). So every exit ends with a ``RESTART:`` line the caller
can paste as-is: the exit itself stays the same, and "the listener is gone"
stops looking exactly like "nothing happened".

Exit codes:
  0  there is work -- a wake was printed, or the dossier is due -- or the
     wait is over and nothing happened
  1  the event stream is unreadable, or a child could not be started
"""
import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

sys.path.insert(0, str(HERE))
import winconsole  # noqa: E402
import wg_budget  # noqa: E402  (等待预算的唯一出口，issue #79/#83)

# Same family as the window suppression: if a console does attach, retag it as
# UTF-8 (and point Python's own streams at UTF-8) so the scripts' output is not
# read through the OEM code page.
winconsole.ensure_utf8_stdio()

# 常驻监听的默认静默周期。`reply.py --wait N` 会用一个很小的 N 把监听临时当
# 有界等待用；那种参数不该被 RESTART 行继承成新的常驻周期（issue #69）。
# 取值对齐等待预算（issue #83）：一个周期必须能装进一次 Agent 工具调用，否则
# 宿主超时会把本进程连 watch.py 一起杀掉，而这轮等待期间不会打印任何东西。
DEFAULT_TIMEOUT = wg_budget.WAIT_BUDGET
SHORT_WAIT = 60


def python_exe():
    candidate = ROOT / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.exists() else sys.executable


def restart_hint(timeout):
    """打印一行可直接粘贴的重挂命令。

    listen.py 按设计有界：默认跑完一轮就退出（issue #88 之前默认是无限轮，
    整次调用装不进一次工具调用）。Agent 读到 WAKE 之后去回消息、读图，下一次
    挂载全靠记性——2026-10-24 一晚上因此掉了 3 次（issue #69）。把「下一步」
    写在退出输出里，就不需要任何记忆：唤醒与重挂指令出现在同一块文本里。

    短等待（`--wait 6` 这类）不透传：那是有界等待的临时边界，重挂应回到
    常驻周期。`--cycles` / `--no-digest` 同理，它们是调试/有界等待开关。

    超预算的取值压回预算（issue #83）：宿主工具超时撑不住 300s，原样推荐会让
    Agent 照抄一次、再被截断一次，而截断路径上不打印任何东西——RESTART 行于是
    变成自己喂养自己的循环。
    """
    if timeout < SHORT_WAIT or wg_budget.over_budget(timeout):
        shown = DEFAULT_TIMEOUT
    else:
        shown = timeout
    exe = Path(python_exe())
    try:
        command = exe.relative_to(ROOT).as_posix()
    except ValueError:
        command = exe.as_posix()
    script = (HERE / "listen.py").relative_to(ROOT).as_posix()
    print(f"RESTART: {command} {script} --timeout {shown}")


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
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"每轮静默等待多少秒（默认 {DEFAULT_TIMEOUT}，即等待预算）")
    parser.add_argument("--cycles", type=int, default=1,
                        help="最多循环几轮（默认 1；0 表示不限轮数，但仍受整次调用"
                             "共享的等待额度约束）")
    parser.add_argument("--no-digest", action="store_true",
                        help="超时后不沉淀、不查档案，只等（给 reply.py --wait 用）")
    args = parser.parse_args()

    # 显式的超预算取值自己说一声（issue #86）。`reply.py --wait` 早就有这行 WARN，
    # `listen.py` 没有——同一个预算、两种静默，而这里正是那个会被静默截断的进程。
    # 必须打在等待之前：被宿主截断的路径上一个字都不会再打印（issue #83）。
    #
    # 后半句是按「会不会真被截断」写的（issue #88 合并上游后改）：超预算的取值
    # 会被压回等待预算（`clamp_wait` 管单轮），所以这里**不会**因此被截断——
    # 照旧写「很可能被截断」就是一句假话，而输出里的假话比没有输出更坏。
    if wg_budget.over_budget(args.timeout):
        print(f"WARN: --timeout {args.timeout}s 加上发送余量 {wg_budget.SEND_BUDGET}s 已超过"
              f"常见工具调用超时 {wg_budget.AGENT_TOOL_TIMEOUT}s；本次已压回等待预算 "
              f"{wg_budget.WAIT_BUDGET}s（issue #88），不会因此被截断",
              flush=True)

    cycles = 0
    started = time.monotonic()
    while True:
        # 这一轮实际能等多久：额度 WAIT_BUDGET 是整次调用共享的**总额**
        # （issue #88），由所有轮次分摊，不是每轮领一份新的。
        wait = wg_budget.clamp_wait(args.timeout, time.monotonic() - started)
        if wait <= 0:
            # 额度用尽：自己收手。等宿主来砍的话，进程树会连同 watch.py 一起死，
            # 监听就此静默消失，而 `--status` 在那之前一直报「已挂」（issue #88）。
            print("NO_WORK")
            print(f"这一轮等不下去了（每轮要等 {args.timeout}s、等待额度 "
                  f"{wg_budget.WAIT_BUDGET}s，已跑 {cycles} 轮），本次调用到此为止")
            restart_hint(args.timeout)
            return 0
        cycles += 1
        # 等待开始前把边界说出来（issue #83）。边界这句话只能打在等待之前——
        # 一旦被截断，run_passthrough 不会回写任何输出。
        print(f"等最多 {wait}s（第 {cycles} 轮）…"
              f"若宿主超时短于此值，本进程会连同 watch.py 一起被杀，本行之后不会再有输出",
              flush=True)
        rc = run("watch.py", "--timeout", str(wait))
        if rc == 0:
            # watch.py already printed the wake lines and the closing WAKE, and
            # run() wrote that block out in one piece. Printing WAKE again here
            # would show two of them, which reads like two wakes (issue #91).
            restart_hint(args.timeout)
            return 0
        if rc != 2:
            # 1 is "stream unreadable"; anything else means the script itself broke.
            print(f"WATCH_FAILED rc={rc}")
            restart_hint(args.timeout)
            return 1

        if args.no_digest:
            # A caller waiting on a bounded clock needs the bound to hold. The
            # digest pass takes tens of seconds on a backlog, which turns
            # "--wait 6" into a minute and makes the timeout meaningless.
            if args.cycles and cycles >= args.cycles:
                print("NO_WORK")
                restart_hint(args.timeout)
                return 0
            continue

        # Timed out: the group has been quiet. Rule-based digestion is free, so it
        # runs on every quiet stretch; the model-backed pass is left to whoever
        # picks up the DOSSIER_DUE below.
        run("dossier.py", "--digest")
        if run("dossier.py", "--check") == 0:
            print("DOSSIER_DUE")
            restart_hint(args.timeout)
            return 0

        if args.cycles and cycles >= args.cycles:
            print("NO_WORK")
            restart_hint(args.timeout)
            return 0


if __name__ == "__main__":
    sys.exit(main())
