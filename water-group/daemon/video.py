"""按需下载群里的视频/文件段并落盘（issue #200 P1）。

``look.py`` 是图片的补读口；本文件是视频的对应物。唤醒行标「可下载
video.py --event <id> --index <i>」时就跑它。

    video.py --event 10123 [--index 0] [--max-mb 40]
    video.py --recent 10
    video.py --list

**链接会过期**。QQ 多媒体 CDN 的地址带 ``rkey``，实测约 8 小时后返回
``retcode -5503``。所以下载分两级：

1. 先用事件段里存档的 ``url``（消息刚到、rkey 还新鲜时这一级就够）；
2. 存档地址失败则用 OneBot ``get_msg(onebot_message_id)`` 让 NapCat 现取一份
   新鲜地址再试一次——这是「群友几小时前发的视频」唯一可能救回来的路；
3. 两级都失败就**明说**链接已过期、需要群友重发。不编、不假装文件还在。

落盘目录与图片同构（``wg_paths.state_dir()`` 可被 ``IDLEJOT_WG_STATE`` 隔离）：:

    <state>/videos/<event_id>-<index>.<ext>
    <state>/videos/index.jsonl

同类内容只存一份（SHA256 复用），索引只留最近 `MAX_INDEX_ROWS` 行，
超出预算的旧文件按最久未用淘汰——视频比图片大得多，不淘汰就是慢性撑满磁盘。
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import wg_download  # noqa: E402
import wg_videos  # noqa: E402
import winconsole  # noqa: E402
from wg_paths import state_dir  # noqa: E402

VIDEOS = state_dir() / "videos"
INDEX = VIDEOS / "index.jsonl"
EVENTS = state_dir() / "wg_events.jsonl"

MAX_MB = 40                       # 单文件上限：够装群聊短视频，不装电影
MAX_BYTES = MAX_MB * 1024 * 1024
CACHE_BYTES = 512 * 1024 * 1024   # 缓存总预算，超出按最久未用淘汰
MAX_INDEX_ROWS = 500
DOWNLOAD_TIMEOUT = 25.0           # 与 wg_download.DEFAULT_TIMEOUT 同值，单次墙钟

MAGIC_EXT = (
    (b"\x1a\x45\xdf\xa3", "webm"),   # EBML：WebM / MKV
)


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
    """先查 `wg_events`，再查 `chat_inbox`。

    `wg_events.jsonl` 只收 @ 与关键词（mention_only），群里随手发的视频多半只在
    `chat_inbox` 里。`reply.py --show` 有同一条回退；`video.py` 少它就会出现
    「群里明明有视频，却报本地没有这条事件」——补读口读不到要补读的东西。
    """
    for row in reversed(events()):
        if row.get("id") == event_id:
            return row
    try:
        import wg_text  # noqa: PLC0415  延迟导入：只在 wg_events 落空时才碰库
        payload = wg_text._inbox_row(event_id)
    except Exception:  # noqa: BLE001  库不可读与「没有这条」在补读口上同义
        return {}
    if not isinstance(payload, dict):
        return {}
    return dict(payload, id=event_id)


def stamp(ts):
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime((ts or 0) / 1000))
    except (OSError, ValueError):
        return "--:--:--"


def load_index():
    """``{(event_id, index): row}``，后写覆盖先写。"""
    rows = {}
    try:
        lines = INDEX.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
            rows[(int(row["event_id"]), int(row.get("index") or 0))] = row
        except (ValueError, KeyError, TypeError):
            continue
    return rows


def upsert(rows):
    """追加索引行；超过上限时重写为最近 `MAX_INDEX_ROWS` 条。"""
    rows = [r for r in rows if r.get("event_id") is not None]
    if not rows:
        return
    VIDEOS.mkdir(parents=True, exist_ok=True)
    existing = INDEX.read_text(encoding="utf-8").splitlines() if INDEX.exists() else []
    existing.extend(json.dumps(r, ensure_ascii=False) for r in rows)
    if len(existing) > MAX_INDEX_ROWS:
        existing = existing[-MAX_INDEX_ROWS:]
    tmp = INDEX.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
    tmp.replace(INDEX)


def ext_for(name, raw):
    """扩展名：先看魔数，再看原文件名；都不像视频就留 ``bin``。"""
    if raw:
        for magic, ext in MAGIC_EXT:
            if raw.startswith(magic):
                return ext
        # MP4 家族：``....ftyp`` 在第 4-8 字节。
        if len(raw) > 12 and raw[4:8] == b"ftyp":
            return "mp4"
    lowered = str(name or "").lower()
    for ext in wg_videos.VIDEO_EXTS:
        if lowered.endswith(ext):
            return ext.lstrip(".")
    return "bin"


def prune(keep_path=None):
    """把缓存压回 `CACHE_BYTES` 以内：最久未用的先删，``keep_path`` 永不删。

    索引不动——文件没了之后下次读取会按 ``local_path`` 不存在的形态报出来，
    读者据索引仍知道「这条视频曾经存在过、现在被淘汰了」。
    """
    if not VIDEOS.is_dir():
        return
    files = []
    total = 0
    for path in VIDEOS.glob("*"):
        if not path.is_file() or path.suffix == ".jsonl" or path.suffix == ".tmp":
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        total += info.st_size
        files.append((info.st_mtime, info.st_size, path))
    if total <= CACHE_BYTES:
        return
    files.sort()
    keep = Path(keep_path).resolve() if keep_path else None
    for _, size, path in files:
        if total <= CACHE_BYTES:
            break
        if keep is not None and path.resolve() == keep:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size


def find_local_by_sha(sha):
    if not sha:
        return ""
    for row in load_index().values():
        if row.get("sha256") == sha and row.get("local_path"):
            path = Path(row["local_path"])
            if path.is_file():
                return str(path)
    return ""


def save(event_id, index, raw, *, name="", sender="", ts=None, source=""):
    """写盘 + 索引，返回索引行。同内容复用旧文件，不重复占空间。"""
    VIDEOS.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(raw).hexdigest()
    reused = find_local_by_sha(sha)
    if reused:
        local = reused
    else:
        target = VIDEOS / f"{event_id}-{index}.{ext_for(name, raw)}"
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)
        local = str(target)
    row = dict(event_id=int(event_id), index=int(index), state="downloaded",
               sha256=sha, bytes=len(raw), name=name, local_path=local,
               sender=sender, ts=ts, source=source, at=time.time())
    upsert([row])
    prune(keep_path=local)
    return row


def fresh_url(event, index):
    """向 NapCat 要一份新鲜地址：``get_msg`` 现取该消息的段。

    拿不到（栈没起 / NapCat 不答 / 段里没视频）一律返回空串——调用方据此说
    「需要群友重发」，而不是拿存档地址反复重试。
    """
    mid = event.get("onebot_message_id")
    if not mid:
        return ""
    try:
        import wg_up_wake  # noqa: PLC0415  延迟导入：只有重签时才需要 OneBot
        data = wg_up_wake._onebot_call("get_msg", {"message_id": int(mid)})
    except Exception:  # noqa: BLE001  重签失败与「没地址」同义，不能崩
        return ""
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    if not isinstance(message, list):
        return ""
    event_like = dict(id=event.get("id"), segments=[
        dict(type="unsupported", kind=seg.get("type"), raw=json.dumps(seg.get("data") or {},
                                                                      ensure_ascii=False))
        for seg in message if isinstance(seg, dict)])
    items = wg_videos.labels(event_like)
    if index >= len(items):
        return ""
    return items[index]["url"]


def fetch(event_id, index, max_mb):
    """下载第 ``index`` 个视频段。返回进程退出码。"""
    event = event_by_id(event_id)
    if not event:
        print(f"本地没有 id={event_id} 的事件（wg_events 里查不到），无法下载。")
        return 1
    items = wg_videos.labels(event)
    if index >= len(items):
        print(f"id={event_id} 只有 {len(items)} 个视频段，没有 index={index}。")
        return 1
    item = items[index]
    cached = load_index().get((event_id, index)) or {}
    if cached.get("local_path") and Path(cached["local_path"]).is_file():
        print(f"{cached['local_path']}")
        print("本地已缓存，未下载。")
        return 0
    limit = int(max_mb) * 1024 * 1024
    if item["bytes"] and item["bytes"] > limit:
        print(f"文件 {item['size'] or item['bytes']} 超过本次上限 {max_mb} MB；"
              "确实要下就加大 --max-mb。")
        return 1
    attempts = []
    if item["url"]:
        attempts.append(("存档地址", item["url"]))
    raw, error = None, ""
    tried = 0
    for label, url in attempts:
        tried += 1
        try:
            raw = wg_download.download(url, max_bytes=limit, timeout=DOWNLOAD_TIMEOUT)
            print(f"  {label}：{len(raw)} 字节")
            break
        except wg_download.DownloadError as exc:
            error = str(exc)
            print(f"  {label}失败：{error}")
    if raw is None:
        renewed = fresh_url(event, index)
        if renewed and renewed != item["url"]:
            tried += 1
            try:
                raw = wg_download.download(renewed, max_bytes=limit, timeout=DOWNLOAD_TIMEOUT)
                print(f"  重签地址：{len(raw)} 字节")
            except wg_download.DownloadError as exc:
                error = str(exc)
                print(f"  重签地址失败：{error}")
        elif not event.get("onebot_message_id"):
            print("  无法重签：事件上没有 onebot_message_id。")
        else:
            # NapCat 不答是**另一件事**：这说明重签根本没试成，不能说成「链接过期」。
            print("  未能重签：OneBot 不答（栈没起或 NapCat 离线），这条没试过重签。")
    if raw is None:
        if not tried:
            print("这条视频没有下载地址，也没有可重签的消息编号。")
        else:
            print(f"下载失败：{error or '未知原因'}。"
                  "存档链接已过期时，重签成功就能下；重签也失败就只能请群友重发——"
                  "这是 QQ 多媒体 CDN 的 rkey 寿命，不是本机问题。")
        return 1
    row = save(event_id, index, raw, name=item["name"],
               sender=event.get("sender") or "", ts=event.get("timestamp"),
               source="renewed" if tried > len(attempts) else "stored")
    print(f"{row['local_path']}")
    print(f"字节：{row['bytes']}，sha256：{row['sha256'][:12]}…")
    return 0


def recent(limit):
    rows = sorted(load_index().values(), key=lambda r: (r.get("at") or 0), reverse=True)
    if not rows:
        print("最近没有视频记录")
        return 0
    for row in rows[:max(1, limit)]:
        exists = row.get("local_path") and Path(row["local_path"]).is_file()
        state = "已缓存" if exists else "文件已淘汰"
        print(f"{row['event_id']} i={row['index']} [{state}] {stamp(row.get('ts'))} "
              f"{row.get('sender') or '?'} {row.get('name') or ''} "
              f"{row.get('local_path') or ''}")
    return 0


def list_candidates():
    """列出事件流里所有视频段及本地状态；只读，不下载。"""
    rows = events()
    shown = 0
    for event in reversed(rows):
        items = wg_videos.labels(event)
        if not items:
            continue
        for item in items:
            row = load_index().get((int(event.get("id") or 0), item["index"])) or {}
            local = row.get("local_path") if row.get("local_path") and Path(row["local_path"]).is_file() else ""
            state = f"已缓存 {local}" if local else ("可下载" if item["url"] else "无下载地址")
            print(f"{event.get('id')} i={item['index']} {stamp(event.get('timestamp'))} "
                  f"{event.get('sender') or '?'} {item['name'] or '(无名)'} "
                  f"{item['size'] or '?'} {state}")
            shown += 1
    if not shown:
        print("事件流里没有视频段")
    return 0


def main(argv=None):
    winconsole.ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="按需下载群聊视频/文件段并落盘")
    parser.add_argument("--event", type=int, help="事件 id（reply.py --context 里的行首编号）")
    parser.add_argument("--index", type=int, default=0, help="视频段下标，默认 0")
    parser.add_argument("--max-mb", type=int, default=MAX_MB, help=f"单文件上限 MB，默认 {MAX_MB}")
    parser.add_argument("--recent", type=int, help="列出最近 N 条下载记录")
    parser.add_argument("--list", action="store_true", help="列出事件流里的视频段，不下载")
    args = parser.parse_args(argv)
    if args.event is not None:
        return fetch(args.event, args.index, args.max_mb)
    if args.recent is not None:
        return recent(args.recent)
    if args.list:
        return list_candidates()
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
