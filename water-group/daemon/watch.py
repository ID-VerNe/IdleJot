"""Block until someone wakes the bot, then print it and exit.

The collector already captures a mention within about a second of it arriving.
What used to add a minute was the agent: it had no way to be told, so it looked
at wg_events.jsonl on its own schedule and the gap between two looks was dead
time. This closes that gap — the call returns the moment a wake lands, so the
only remaining delay is composing the reply.

Two wake paths meet here: @ and keywords arrive in wg_events.jsonl, and while a
follow-up window is open (追问窗口插件，issue #4 PD2a) ordinary chatter after the
last reply counts too, read straight from chat_inbox. They never overlap — a
mention always wins — and the window closes itself once the group goes quiet.

A wake that aged out while nobody was listening is not reported as a wake (the
cold start would replay days of dead mentions), but it is no longer silent
either: the timeout block ends with an ``EXPIRED:`` count and its ids, so
"nobody watched for two hours" and "the group said nothing" stop looking the
same (issue #69).

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

import plugins  # noqa: E402  (事件型能力派发口：追问窗口，issue #4 PD2a)
import up  # noqa: E402  (wake_reason / events / replied_ids live there)
import wg_aliases  # noqa: E402  (别名指向渲染，issue #30)
import wg_clock  # noqa: E402  (「现在」的唯一出口，issue #95)
import wg_images  # noqa: E402  (图片标记渲染)
import wg_quotes  # noqa: E402  (引用原文渲染，issue #94)
import wg_text  # noqa: E402  (正文渲染：段占位不占额度，issue #32)
import winconsole  # noqa: E402  (stdout 编码 + 控制台代码页，一次收口)

ROOT = HERE.parents[1]
from wg_paths import state_dir
STATE = state_dir()
CURSOR = STATE / "wg_watch.txt"
POLL = 1.5
# 过期未回唤醒的报告里最多列几个 id：一行报得下，又不至于把整块输出撑成清单。
EXPIRED_SHOWN = 5


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


def report_expired(rows, start_cursor):
    """把「因过期被丢掉、且从没回过」的唤醒说出来（issue #69）。

    它们不写账本、不进 fresh（冷启动后整批陈年 @ 会变成「新唤醒」，见
    check_pending_reply 的契约），但也**不能什么都不说**：监听掉线超过
    pending_reply_ttl 后，那段时间的 @ 在输出里与「从没来过」完全一样，Agent
    没有任何线索知道自己错过了什么。计数带上 id，补不补由 Agent 决定（
    reply.py <id> --force）。

    只算 `start_cursor` 之后新到的：本轮开始时就已在文件里的历史积压，正是
    冷启动不该被翻出来的那批，保持静默。游标归零就是冷启动——那时「过期」
    只能说明文件里有陈年旧账，不能说明这次监听错过了什么。
    """
    if not start_cursor:
        return
    ledger = up.replied_ids()
    expired = [e for e in rows
               if (e.get("id") or 0) > start_cursor
               and up.wake_reason(e)
               and not up.replied_after(e.get("id"), rows, ledger)
               and up.wake_expired(e)]
    if not expired:
        return
    ids = "、".join(str(e.get("id")) for e in expired[:EXPIRED_SHOWN])
    if len(expired) > EXPIRED_SHOWN:
        ids += f" 等 {len(expired)} 条"
    print(f"EXPIRED: {len(expired)} 条未回唤醒已超过待回时效"
          f"（TTL {up.pending_reply_ttl()}s），未计入唤醒：{ids}")
    print("         要补旧话题就单独回：reply.py <id> --force")


def require_wake_identity():
    """缺 self_account 的监听必须拒绝启动，而不是假装在工作（issue #51）。

    config.json 丢失时 ``self_account()`` 读空：``wake_reason`` 的 @ 匹配
    （``str(seg.qq) == ""``）永假、关键词列表为空——监听进程活着、collector 在收
   消息、游标在推进，只有「Agent 不回应」一个症状，而 Agent 侧看到的是「没有
    唤醒」，与「群里没人说话」完全无法区分。一个永远唤不醒的监听比没有监听更糟。

    返回 0 放行；返回 1 并打印缺什么、怎么恢复（config.example.json）。
    """
    if up.self_account():
        return 0
    print("[拒绝] water-group/config.json 缺失或未设置 self_account：")
    print("       @ 判定永假、self_names 关键词为空，这个监听不可能被唤醒。")
    print("       按 water-group/config.example.json 恢复配置后重试；")
    print("       当前缺什么可以用 up.py --status 的「配置」行核对。")
    return 1


def _plugin_settings():
    """追问窗口插件的归一化设置（三态门控要它，issue #4 PD2a）。

    延迟 import commands：等唤醒这条路径不碰指令树，别让 CLI 白拖约 80ms
    进来。读不到配置时回落空设置——插件按 default_enabled 走。
    """
    try:
        import commands  # noqa: PLC0415

        return commands.load_settings()
    except Exception:  # noqa: BLE001  设置坏了不该让等唤醒这条链路崩
        return {}


def main():
    parser = argparse.ArgumentParser(description="等一条唤醒（@ 或关键词）")
    parser.add_argument("--timeout", type=int, default=240, help="最多等多少秒")
    parser.add_argument("--after", type=int, default=None, help="从这个事件 id 之后开始找")
    parser.add_argument("--replay", action="store_true", help="忽略游标，重扫所有未回唤醒")
    args = parser.parse_args()

    winconsole.ensure_utf8_stdio()

    # 先验身份再等唤醒：缺 self_account 时每一条 @ 都判不中，挂着也是白挂
    # （issue #51）。listen.py 经 run_passthrough 透传这个退出码。
    if require_wake_identity():
        return 1

    cursor = 0 if args.replay else (args.after if args.after is not None else read_cursor())
    # 本轮起点：过期统计只算这之后新到的唤醒。冷启动（0）不报，历史积压
    # 仍按 check_pending_reply 的契约静默（issue #69）。
    start_cursor = cursor
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
                # 正文按正文字数裁、截断了带 --show 指引（issue #32）：
                # 80 字额度不再被 [引用 QQ 消息 <id>]@<qq> 这类段占位吃掉。
                body, _, _ = wg_text.render_body(e.get("content"), wg_text.WAKE_BODY_CHARS,
                                                  e.get("id"))
                # 人名指向：发送者附档案锚，正文命中的外号指出是谁（issue #30）。
                gid = wg_aliases.group_of(e.get("conversation_id"))
                tag = wg_aliases.sender_tag(e.get("sender"), gid)
                hits = wg_aliases.body_hits(e.get("content"), gid,
                                            sender=e.get("sender"))
                print(f"id={e.get('id')} {stamp} [{up.wake_reason(e)}] "
                      f"{e.get('sender')}{tag}: {body}"
                      f"{('  ' + quotes) if quotes else ''}"
                      f"{('  ' + images) if images else ''}"
                      f"{''.join('  ' + h for h in hits)}")
            write_cursor(max(latest_id(rows), max(e.get("id") or 0 for e in fresh)))
            print("WAKE")
            return 0

        # 没有新的 @/关键词，但追问窗口可能还开着：回复之后群里继续聊的几句
        # 全在 chat_inbox 里（mention_only 根本没把它们投递出来），顺带查一眼。
        # 窗口自己会过期；路上撞见了就顺手关掉，不用另起进程打扫。
        # 查窗口、判三态、收窗都在插件里（issue #4 PD2a）；宿主只负责渲染。
        scan = plugins.emit("wake_scan", {"settings": _plugin_settings()})
        if scan and scan.get("closed"):
            print(f"[追问] 窗口关闭（{scan['closed']}）")
        elif scan and scan.get("woke"):
            woke = scan["woke"]
            print(wg_clock.now_line())
            for r in woke:
                stamp = wg_clock.clock_ms(r.get("ts"))
                # 追问行与唤醒行同一份渲染口径（issue #32）。
                body, _, _ = wg_text.render_body(r.get("text"),
                                                  wg_text.WAKE_BODY_CHARS,
                                                  r.get("id"))
                # 人名指向与唤醒行同款（issue #30）；追问行没有
                # conversation_id 字段时退回跨群主档别名。
                gid = wg_aliases.group_of(r.get("conversation_id"))
                tag = wg_aliases.sender_tag(r.get("sender"), gid)
                hits = wg_aliases.body_hits(r.get("text"), gid,
                                            sender=r.get("sender"))
                print(f"id={r.get('id')} {stamp} [追问] {r.get('sender')}{tag}: "
                      f"{body}"
                      f"{('  ' + r['quote']) if r.get('quote') else ''}"
                      f"{('  ' + r['images']) if r.get('images') else ''}"
                      f"{''.join('  ' + h for h in hits)}")
            # 报告文案由 followup 决定：额度用完的那一批不说「还剩 0 条」
            # （issue #98）。收窗也在 consume() 里做完，不再留到下一轮。
            print(scan["report"])
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
            report_expired(rows, start_cursor)
            print("TIMEOUT")
            return 2
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
