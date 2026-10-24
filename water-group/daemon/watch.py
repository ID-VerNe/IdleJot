"""Block until someone wakes the bot, then print it and exit.

The collector already captures a mention within about a second of it arriving.
What used to add a minute was the agent: it had no way to be told, so it looked
at wg_events.jsonl on its own schedule and the gap between two looks was dead
time. This closes that gap — the call returns the moment a wake lands, so the
only remaining delay is composing the reply.

Two wake paths meet here: @ and keywords arrive in wg_events.jsonl, and while a
follow-up window is open (followup.py) ordinary chatter after the last reply
counts too, read straight from chat_inbox. They never overlap — a mention always
wins — and the window closes itself once the group goes quiet.

Exit codes:
  0  a wake was printed
  2  timed out with nothing new (call again)
  1  the event stream is unreadable
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import followup  # noqa: E402  (追问窗口：回复之后继续听闲聊)
import up  # noqa: E402  (wake_reason / events / replied_ids live there)
import wg_clock  # noqa: E402  (「现在」的唯一出口，issue #95)
import wg_images  # noqa: E402  (图片标记渲染)
import wg_quotes  # noqa: E402  (引用原文渲染，issue #94)
import winconsole  # noqa: E402  (stdout 编码 + 控制台代码页，一次收口)

ROOT = HERE.parents[1]
from wg_paths import state_dir
STATE = state_dir()
CURSOR = STATE / "wg_watch.txt"
POLL = 1.5


def read_cursor():
    try:
        return int(CURSOR.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def write_cursor(value):
    try:
        CURSOR.write_text(str(value), encoding="utf-8")
    except OSError:
        pass


def latest_id(rows):
    return max((r.get("id") or 0) for r in rows) if rows else 0


def main():
    parser = argparse.ArgumentParser(description="等一条唤醒（@ 或关键词）")
    parser.add_argument("--timeout", type=int, default=240, help="最多等多少秒")
    parser.add_argument("--after", type=int, default=None, help="从这个事件 id 之后开始找")
    parser.add_argument("--replay", action="store_true", help="忽略游标，重扫所有未回唤醒")
    args = parser.parse_args()

    winconsole.ensure_utf8_stdio()

    cursor = 0 if args.replay else (args.after if args.after is not None else read_cursor())
    deadline = time.time() + args.timeout
    ledger = up.replied_ids()

    while True:
        rows = up.events()
        ledger = up.replied_ids()
        # 过期唤醒和 --list 一样滤掉：冷启动后 .tmp 是新的、游标归零，陈年
        # 未回唤醒会整批变成「新唤醒」打印出来，Agent 据此回头回复旧话题。
        fresh = [e for e in rows
                 if (e.get("id") or 0) > cursor
                 and up.wake_reason(e)
                 and not up.replied_after(e.get("id"), rows, ledger)
                 and not up.wake_expired(e)]

        if fresh:
            # 一睁眼先给基准：下面每行是消息时间戳，不是现在（issue #95）。
            print(wg_clock.now_line())
            for e in fresh:
                stamp = wg_clock.clock_ms(e.get("timestamp"))
                images = wg_images.render(e)
                quotes = wg_quotes.render_event(e)
                print(f"id={e.get('id')} {stamp} [{up.wake_reason(e)}] "
                      f"{e.get('sender')}: {(e.get('content') or '')[:80]}"
                      f"{('  ' + quotes) if quotes else ''}"
                      f"{('  ' + images) if images else ''}")
            write_cursor(max(latest_id(rows), max(e.get("id") or 0 for e in fresh)))
            print("WAKE")
            return 0

        # 没有新的 @/关键词，但追问窗口可能还开着：回复之后群里继续聊的几句
        # 全在 chat_inbox 里（mention_only 根本没把它们投递出来），顺带查一眼。
        # 窗口自己会过期；路上撞见了就顺手关掉，不用另起进程打扫。
        window = followup.load()
        if window:
            reason = followup.stale(window)
            if reason:
                followup.close()
                print(f"[追问] 窗口关闭（{reason}）")
            else:
                woke = followup.consume(window,
                                        followup.since(window.get("after_id"),
                                                       window.get("session"),
                                                       window.get("not_before")))
                if woke:
                    print(wg_clock.now_line())
                    for r in woke:
                        stamp = wg_clock.clock_ms(r.get("ts"))
                        print(f"id={r.get('id')} {stamp} [追问] {r.get('sender')}: "
                              f"{(r.get('text') or '')[:80]}"
                              f"{('  ' + r['quote']) if r.get('quote') else ''}")
                    # 报告文案由 followup 决定：额度用完的那一批不说「还剩 0 条」
                    # （issue #98）。收窗也在 consume() 里做完，不再留到下一轮。
                    print(followup.wake_report(window, len(woke)))
                    print("WAKE")
                    return 0

        # Nothing to report: move past everything seen so a later call does not
        # rescan the whole file, then wait for the collector to append.
        if rows:
            moved = max(cursor, latest_id(rows))
            if moved != cursor:
                cursor = moved
                write_cursor(cursor)

        if time.time() >= deadline:
            print("TIMEOUT")
            return 2
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
