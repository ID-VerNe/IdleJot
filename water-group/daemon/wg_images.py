"""群聊图片本地索引与落盘——水群侧唯一的图片文件归属。

MCP 的图片只在 content 里以 base64 交付，collector 收到后必须自己落盘，
否则 Agent 只能看到 ``[图片]`` 占位，看不了图（issue #27）。

目录布局（与 wg_paths 一致，测试可用 IDLEJOT_WG_STATE 隔离）::

    <state>/images/<event_id>-<image_index>.<ext>
    <state>/images/index.jsonl

index.jsonl 每次追加一行，读取时后写覆盖先写；事件与图片下标是主键。
像素只写一次：同一 sha256 已有文件时直接复用原路径，二次查看不重复下载。
"""
import base64
import hashlib
import json
import time
from pathlib import Path

from wg_paths import state_dir

IMAGES = state_dir() / "images"
INDEX = IMAGES / "index.jsonl"
MAX_BYTES = 6 * 1024 * 1024       # 与 MCP 单张图上限一致
MAX_INDEX_ROWS = 4000             # 索引只保留最近若干条，避免无限增长

MIME_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
            "image/gif": "gif", "image/webp": "webp"}


def ext_for(mime, raw=None):
    """优先用魔数判断扩展名，声明 mime 只作兜底。"""
    if raw:
        if raw.startswith(b"\x89PNG"):
            return "png"
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        if raw[:2] == b"\xff\xd8":
            return "jpg"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "webp"
    return MIME_EXT.get(str(mime or "").lower(), "bin")


def load_index():
    """返回 {(event_id, image_index): row}，后写覆盖先写。"""
    rows = {}
    try:
        lines = INDEX.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
            rows[(int(row["event_id"]), int(row.get("image_index") or 0))] = row
        except (ValueError, KeyError, TypeError):
            continue
    return rows


def find_local_by_sha(sha):
    """本机是否已有相同内容的图片；返回可打开的路径或空串。"""
    if not sha:
        return ""
    for row in load_index().values():
        if row.get("sha256") == sha and row.get("local_path"):
            path = Path(row["local_path"])
            if path.is_file():
                return str(path)
    return ""


def upsert(rows):
    """追加索引行；超过上限时重写为最近 MAX_INDEX_ROWS 条。"""
    rows = [r for r in rows if r.get("event_id") is not None]
    if not rows:
        return
    IMAGES.mkdir(parents=True, exist_ok=True)
    existing = INDEX.read_text(encoding="utf-8").splitlines() if INDEX.exists() else []
    existing.extend(json.dumps(r, ensure_ascii=False) for r in rows)
    if len(existing) > MAX_INDEX_ROWS:
        existing = existing[-MAX_INDEX_ROWS:]
    tmp = INDEX.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
    tmp.replace(INDEX)


def save_pixels(event_id, image_index, blocks, *, sender="", ts=None, quote_of=None,
                state="pixels_delivered", dimensions=None, frame_indices=None, index=True):
    """把一次图片交付的 base64 像素写成文件，返回索引行。

    blocks 是 mcp_client.call_tool_raw 返回的图片块列表，按 content_index 对齐；
    entry["content_indexes"] 指向它们在 MCP content 里的实际位置。
    """
    IMAGES.mkdir(parents=True, exist_ok=True)
    saved = []
    sha = ""
    for position, block in enumerate(blocks or []):
        encoded = str(block.get("data_b64") or "")
        if not encoded:
            continue
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            continue
        if not raw or len(raw) > MAX_BYTES:
            continue
        sha = hashlib.sha256(raw).hexdigest()
        ext = ext_for(block.get("mime"), raw)
        target = IMAGES / f"{event_id}-{image_index}.{ext}"
        if position:
            target = IMAGES / f"{event_id}-{image_index}-{position}.{ext}"
        # 内容寻址复用：同图已在别处落盘就指向旧文件，不重复写。
        reused = "" if position else (find_local_by_sha(sha) or "")
        if reused:
            saved.append(reused)
            continue
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)
        saved.append(str(target))
    row = dict(event_id=int(event_id), image_index=int(image_index), state=state,
               sha256=sha, local_path=saved[0] if saved else "", files=saved,
               sender=sender, ts=ts, quote_of=quote_of,
               dimensions=dimensions or [], frame_indices=frame_indices or [],
               seen=bool(saved), at=time.time())
    if index:
        upsert([row])
    return row


def quote_of(message, image_index):
    """引用图所属的原消息编号；直接图为空。"""
    for segment in image_segments(message):
        if segment.get("image_index") == image_index and segment.get("quote_message_id"):
            return segment["quote_message_id"]
    return None


def ingest(packet, blocks):
    """保存一次 wait 包里的图片：已交付的落盘，其余只记元数据。

    返回索引行列表；像素只在交付过时写文件，deferred 项保留状态供 look.py 补读。
    """
    entries = (packet.get("materials") or {}).get("images") or []
    if not entries:
        return []
    messages = {m.get("id"): m for m in (packet.get("messages") or [])}
    by_content = {block.get("content_index"): block for block in (blocks or [])}
    rows = []
    for entry in entries:
        image_index = entry.get("image_index")
        if image_index is None:
            continue
        event_id = entry.get("event_id")
        message = messages.get(event_id) or {}
        picked = [by_content[c] for c in entry.get("content_indexes") or [] if c in by_content]
        common = dict(sender=message.get("sender") or "", ts=message.get("timestamp"),
                      quote_of=quote_of(message, image_index))
        if picked and entry.get("state") == "pixels_delivered":
            row = save_pixels(event_id, image_index, picked, dimensions=entry.get("dimensions"),
                              frame_indices=entry.get("frame_indices"), index=False, **common)
        else:
            row = dict(event_id=int(event_id), image_index=int(image_index),
                       state=entry.get("state"), sha256="", local_path="", files=[],
                       dimensions=entry.get("dimensions") or [],
                       frame_indices=entry.get("frame_indices") or [],
                       seen=False, at=time.time(), **common)
        rows.append(row)
    upsert(rows)
    for message in messages.values():
        attach(message, rows)
    return rows


def attach(event, rows):
    """把索引里的 local_path 写回事件段，供 wg_events.jsonl 的读者直接使用。"""
    by_index = {int(r["image_index"]): r for r in rows if int(r.get("event_id") or 0) == int(event.get("id") or 0)}
    if not by_index:
        return
    for segment in image_segments(event):
        index = segment.get("image_index")
        if index is None:
            continue
        row = by_index.get(int(index))
        if not row:
            continue
        if row.get("local_path"):
            segment["local_path"] = row["local_path"]
        if row.get("sha256"):
            segment["sha256"] = row["sha256"]


def image_segments(value):
    """递归遍历消息段、节点与引用消息，产出图片段。"""
    for segment in value.get("segments", []) or []:
        if segment.get("type") == "image":
            yield segment
        for node in segment.get("nodes", []) or []:
            yield from image_segments(node)
    for detail in value.get("referenced_messages", []) or []:
        yield from image_segments(detail)
    for node in value.get("nodes", []) or []:
        yield from image_segments(node)


def labels(event, media=None):
    """汇总一条消息里所有图片的本地状态，供命令行渲染。

    media 是 chat_inbox.media 列（顺序即 image_index）；消息段缺失时用它兜底，
    这样 reply.py --context 里的引用图也能显示。

    发现顺序：段内扫描 → 索引反查 → media 兜底。索引反查（issue #35）补的是
    「段里没有 image_index、但索引里有该事件图片行」的形状——旧数据或部分投递的
    wg_events 行，没有这一步唤醒行就只剩 content 里的裸 [图片] 占位。
    """
    index_rows = load_index()
    found = []
    for segment in image_segments(event):
        if "image_index" not in segment:
            continue
        found.append(dict(index=int(segment["image_index"]),
                          quote=segment.get("quote_message_id"),
                          state=(segment.get("pixels") or {}).get("state") or "available",
                          local_path=segment.get("local_path")))
    if not found:
        event_id = int(event.get("id") or 0)
        for (row_eid, row_idx), row in sorted(index_rows.items()):
            if row_eid != event_id:
                continue
            found.append(dict(index=row_idx, quote=row.get("quote_of"),
                              state=row.get("state") or "deferred",
                              local_path=row.get("local_path") or ""))
    if not found:
        for position, item in enumerate(media or []):
            if item.get("kind") not in (None, "image", "gif", "sticker"):
                continue
            if item.get("status") not in (None, "available"):
                continue
            found.append(dict(index=position, quote=item.get("quote_message_id"),
                              state="available", local_path=None))
    for item in found:
        row = index_rows.get((int(event.get("id") or 0), item["index"])) or {}
        item["state"] = row.get("state") or item["state"]
        item["local_path"] = item.get("local_path") or row.get("local_path") or ""
        item["quote"] = item.get("quote") or row.get("quote_of")
    return found


def render(event, media=None):
    """渲染成一行图片标记，例如 ``[图片 i=0 path=C:\\...\\12-0.jpg]``。

    「可补读」必须带上具体命令（issue #35）：裸「可补读」三个字不是可执行指引，
    Agent 每次都要现场试错才能想起 look.py。同理，占位在、记录全无也要明说
    「未登记」并给出命令——不靠不印表达失败。
    """
    parts = []
    event_id = int(event.get("id") or 0)
    for item in labels(event, media):
        suffix = f" 引自 {item['quote']}" if item.get("quote") else ""
        hint = f"look.py --event {event_id} --index {item['index']}"
        if item.get("local_path"):
            parts.append(f"[图片 i={item['index']}{suffix} path={item['local_path']}]")
        elif item["state"] in ("deferred", "available", "", None):
            parts.append(f"[图片 i={item['index']}{suffix} 可补读 {hint}]")
        elif item["state"] == "permission_denied":
            parts.append(f"[图片 i={item['index']}{suffix} 无权限]")
        else:
            parts.append(f"[图片 i={item['index']}{suffix} 未交付:{item['state']}]")
    if not parts:
        # content 里有占位 = 确有图片，但段、media、索引全无记录：给出补读命令，
        # 不让唤醒行只剩一个裸 [图片]（issue #35）。
        body = str(event.get("content") or "")
        if "[图片]" in body or "[动画表情]" in body:
            parts.append(f"[图片 未登记 look.py --event {event_id} 补读]")
    return "".join(parts)
