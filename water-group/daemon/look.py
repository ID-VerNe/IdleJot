"""按需补读历史图片：把 MCP 像素落盘成可打开的文件。

collector 默认只预热 @ 我那条消息的图（auto_images=candidates），其余图片
只留元数据。需要看时用这个入口补读，二次查看直接命中本地索引，0 token、0 网络。

Usage:
  look.py --event 123 [--index 0] [--frames all]
  look.py --event q456 [--index 0]     # 追问行 / --context 的 q<n>
  look.py --recent 10
  look.py --pick
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import mcp_client  # noqa: E402
import session  # noqa: E402
import wg_event_ref  # noqa: E402  (id / qid 两套命名空间，issue #203)
import wg_images  # noqa: E402
import winconsole  # noqa: E402

EVENTS = wg_images.IMAGES.parent / "wg_events.jsonl"


def events():
    rows = []
    try:
        for line in EVENTS.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def event_by_id(event_id):
    return next((row for row in reversed(events()) if row.get("id") == event_id), {})


def stamp(ts):
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime((ts or 0) / 1000))
    except (OSError, ValueError):
        return "--:--:--"


def show(row):
    path = row.get("local_path") or ""
    state = row.get("state") or ""
    quote = f" 引自 {row['quote_of']}" if row.get("quote_of") else ""
    dims = "x".join(str(v) for v in row.get("dimensions") or [])
    print(f"{row['event_id']} i={row['image_index']} [{state}{quote}] "
          f"{stamp(row.get('ts'))} {row.get('sender') or '?'} "
          f"{dims + ' ' if dims else ''}{path}")


def recent(limit):
    rows = sorted(wg_images.load_index().values(), key=lambda r: (r.get("at") or 0), reverse=True)
    if not rows:
        print("最近没有图片记录")
        return 0
    for row in rows[:max(1, limit)]:
        show(row)
    return 0


def pick():
    """列出值得补读的图片候选；只读索引，不拉像素。"""
    index_rows = wg_images.load_index()
    unseen = [row for row in index_rows.values() if not row.get("local_path")]
    if not unseen:
        print("没有待补读的图片（0 候选，0 token）")
        return 0
    by_event = {}
    for row in events():
        by_event[row.get("id")] = row
    now = time.time()
    ranked = []
    for row in unseen:
        event = by_event.get(row.get("event_id")) or {}
        text = str(event.get("content") or "")
        score, why = 0.0, []
        age = max(0.0, now - float(row.get("at") or 0))
        score += max(0.0, 3 - age / 600)  # 十分钟内新图优先
        why.append("较新")
        if any(word in text for word in ("这图", "看图", "看这个", "像不像", "是什么", "这什")):
            score += 4
            why.append("指代/看图请求")
        if row.get("quote_of"):
            score += 1
            why.append("引用图")
        if text.strip() in ("[图片]", "[动画表情]"):
            score -= 1  # 纯图无上下文，优先级低
        ranked.append((score, row, "、".join(why)))
    ranked.sort(key=lambda item: item[0], reverse=True)
    for score, row, why in ranked[:3]:
        show(row)
        print(f"  候选分 {score:.1f}：{why}")
    return 0


def _inbox_id_of_events(event_id):
    """事件流 id -> 会话库 rowid（``wg_events`` 行上的 ``inbox_id``）。

    ``chat_inbox`` 是 AUTOINCREMENT 且跨会话共用，同一条消息在两套编号里的数字
    基本相同——但**只是基本相同**：别群先插的行会把库 rowid 推高，于是两者分叉。
    图片索引（``wg_images``）与 ``read_chat_image`` 都用库 rowid，所以补读必须按
    库口径去取，不能假设 ``wg_events.id`` 就等于它（issue #203）。
    """
    event = event_by_id(event_id)
    try:
        return int(event.get("inbox_id"))
    except (TypeError, ValueError):
        return None


def read_event(event_id, index, frames):
    row = wg_images.load_index().get((event_id, index)) or {}
    if row.get("local_path") and Path(row["local_path"]).is_file():
        show(row)
        print("本地已缓存，未调用 MCP。")
        return 0
    sid = session.current()
    if not sid:
        print("没有活动会话：先起栈（start-bot.pyw），或 wg_session.txt 缺失。")
        return 1
    data, blocks = mcp_client.call_tool_raw(
        "read_chat_image",
        {"session_id": sid, "event_id": event_id, "index": index, "frames": frames},
        timeout=120,
    )
    error = str(data.get("error") or "")
    if error:
        print(f"补读失败：{error}"
              + (f"（{data.get('error_code')}）" if data.get("error_code") else ""))
        return 1
    if not blocks:
        print(f"没有拿到像素：{json.dumps(data, ensure_ascii=False)[:300]}")
        return 1
    event = event_by_id(event_id)
    saved = wg_images.save_pixels(
        event_id, index, blocks,
        sender=event.get("sender") or row.get("sender") or "",
        ts=event.get("timestamp") or row.get("ts"),
        quote_of=wg_images.quote_of(event, index) or row.get("quote_of"),
        dimensions=data.get("dimensions"), frame_indices=data.get("frame_indices"),
    )
    show(saved)
    print(f"字节：{data.get('bytes', '?')}，帧下标：{data.get('frame_indices')}")
    return 0


def look(ref, index, frames):
    """按记号补读（issue #203）：``q<n>`` 直接是库 rowid，裸数字是事件流 id。

    两套编号会撞号：``--event 7`` 是唤醒行第 7 条，``--event q7`` 是追问行/上下文
    里那条闲聊。裸数字仍按改前的做法经 ``wg_events`` 反查库 rowid（历史行为不变），
    只是现在反查不到时**说出来**，不再拿那个数字去猜库。
    """
    try:
        namespace, number = wg_event_ref.parse(ref)
    except wg_event_ref.BadRef as exc:
        print(f"拒绝：{exc}")
        return 1
    if namespace == wg_event_ref.INBOX:
        return read_event(number, index, frames)
    inbox = _inbox_id_of_events(number)
    if inbox is None:
        print(f"事件流里没有 id={number} 的记录，对着它补读会取到无图的条目。")
        print(f"      要读追问行/上下文里的那条请写 --event q{number}")
        return 1
    return read_event(inbox, index, frames)


def main(argv=None):
    winconsole.ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="补读群聊图片并落盘")
    parser.add_argument(
        "--event",
        help="消息编号：唤醒行/事件流的裸数字（7）或追问行/上下文的 q7；也可写 id:7 / qid:7")
    parser.add_argument("--index", type=int, default=0, help="图片下标 image_index，默认 0")
    parser.add_argument("--frames", choices=["one", "all"], default="one", help="动图默认 1 帧；all 补 3 帧")
    parser.add_argument("--recent", type=int, help="列出最近 N 条图片记录")
    parser.add_argument("--pick", action="store_true", help="列出值得补读的图片候选，不拉像素")
    args = parser.parse_args(argv)
    if args.event is not None:
        return look(args.event, args.index, args.frames)
    if args.recent is not None:
        return recent(args.recent)
    if args.pick:
        return pick()
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
