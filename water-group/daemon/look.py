"""按需补读历史图片：把 MCP 像素落盘成可打开的文件。

collector 默认只预热 @ 我那条消息的图（auto_images=candidates），其余图片
只留元数据。需要看时用这个入口补读，二次查看直接命中本地索引，0 token、0 网络。

Usage:
  look.py --event 123 [--index 0] [--frames all]
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
import wg_event_ref  # noqa: E402  (事件 id 的唯一解析入口，issue #216)
import wg_images  # noqa: E402
import winconsole  # noqa: E402


def events():
    """`wg_events.jsonl` 的行：来源顺序与读取口径归 `wg_event_ref`（issue #216）。

    改前这个文件自带一份事件流读取与 `event_by_id()`，只扫 `wg_events.jsonl`：
    `--context` 里的闲聊（mention_only 滤掉、只在 `chat_inbox`）补读到像素时，
    落盘那一行拿不到 sender / ts / quote_of，图片索引里就少一半信息。现在按 id
    取行走 `wg_event_ref.resolve()`，这里只留「整份事件流」这一个用途。
    """
    return wg_event_ref.events()


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


def read_event(event_id, index, frames):
    row = wg_images.load_index().get((event_id, index)) or {}
    if row.get("local_path") and Path(row["local_path"]).is_file():
        show(row)
        print("本地已缓存，未调用 MCP。")
        return 0
    # 先按同一个解析入口确认这条 id 在不在：两个来源都没有时，补读接口同样拿不到
    # 像素，先在这里说清是「没有这条」而不是让 MCP 回一句泛泛的失败（issue #216）。
    ref = wg_event_ref.resolve(event_id)
    if ref is None:
        print(f"本地没有 id={event_id} 的记录（wg_events 与 chat_inbox 都没有），无法补读。")
        print("可能已被 chat_inbox 的 1000 条上限挤掉，或这个 id 从来不存在。")
        return 1
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
    event = ref.payload
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


def main(argv=None):
    winconsole.ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="补读群聊图片并落盘")
    parser.add_argument("--event", type=int, help="事件 id（reply.py --context 里的行首编号）")
    parser.add_argument("--index", type=int, default=0, help="图片下标 image_index，默认 0")
    parser.add_argument("--frames", choices=["one", "all"], default="one", help="动图默认 1 帧；all 补 3 帧")
    parser.add_argument("--recent", type=int, help="列出最近 N 条图片记录")
    parser.add_argument("--pick", action="store_true", help="列出值得补读的图片候选，不拉像素")
    args = parser.parse_args(argv)
    if args.event is not None:
        return read_event(args.event, args.index, args.frames)
    if args.recent is not None:
        return recent(args.recent)
    if args.pick:
        return pick()
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
