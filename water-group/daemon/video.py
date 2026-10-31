"""按需下载群里的视频/文件段并落盘（issue #200 P1）。

``look.py`` 是图片的补读口；本文件是视频的对应物。唤醒行标「可下载
video.py --event <id> --index <i>」时就跑它。

    video.py --event 10123 [--index 0] [--max-mb 40]
    video.py --event q10123 [--index 0]              # 上下文行 / 追问行里的那条
    video.py --frames --event 10123 [--index 0]      # 抽首/中/尾 3 帧（需要 ffmpeg）
    video.py --recent 10
    video.py --list

**两套记号（issue #203 / #216）**：唤醒行与 ``--list`` 打的是事件流 id（裸数字），
``--context`` 与追问行打的是会话库 rowid（``q<n>``）。两套编号会撞号，所以
``--event`` 认 ``7`` / ``id=7`` / ``q7`` / ``qid=7`` 四种写法，解析与「事件流优先、
会话库兜底」的先后顺序全部交给 ``wg_event_ref``——本文件不再自己写一遍。

改前这里靠 ``wg_text._inbox_row()`` 兜底，而 #216 已把这个私有函数删掉，调用又包在
``except Exception`` 里，于是**静默**返回空：群里随口发的视频（不进 ``wg_events``，
只在 ``chat_inbox`` 里）一律报「本地没有这条事件」——恰好是 #200 要解决的大多数场景。

**链接会过期**。QQ 多媒体 CDN 的地址带 ``rkey``，实测约 8 小时后返回
``retcode -5503``。所以下载分两级：

1. 先用事件段里存档的 ``url``（消息刚到、rkey 还新鲜时这一级就够）；
2. 存档地址失败则用 OneBot ``get_msg(onebot_message_id)`` 让 NapCat 现取一份
   新鲜地址再试一次——这是「群友几小时前发的视频」唯一可能救回来的路；
3. 两级都失败就**明说**链接已过期、需要群友重发。不编、不假装文件还在。

落盘目录与图片同构（``wg_paths.state_dir()`` 可被 ``IDLEJOT_WG_STATE`` 隔离）：:

    <state>/videos/<记号>-<index>.<ext>
    <state>/videos/<记号>-<index>-frames/<记号>-<index>-<n>.png
    <state>/videos/index.jsonl

文件名与索引键都用**记号**（``7`` / ``q7``）而不是裸 id：两套编号会撞号，用裸 id
会让后下载的那条覆盖先下载的。同类内容只存一份（SHA256 复用），索引只留最近
`MAX_INDEX_ROWS` 行，超出预算的旧文件按最久未用淘汰——视频与抽帧图都比图片大得多，
不淘汰就是慢性撑满磁盘。
"""
import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import wg_download  # noqa: E402
import wg_event_ref  # noqa: E402
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


def resolve_event(ref):
    """记号 -> ``(事件, 抄回去的记号, 来源, 失败原因)``；读不到时前两项为空。

    解析口径与 ``look.py`` / ``reply.py --show`` 逐条一致（issue #203 / #216）：
    裸数字只认事件流，``q<n>`` 只认会话库，撞号时各指各的。事件流里没有这个裸
    数字时**明说**去看 ``q<n>``，不拿这个数字去猜库——猜一次就是补读到别群那条
    （#203 的 P0 判级）。

    返回的 ``事件`` 补上了 ``id`` 键：``chat_inbox`` 的 payload 里没有它，而
    ``wg_videos.render`` 要靠它写进命令（改前 ``--show`` 打出来的就是
    ``--event 0``）。
    """
    try:
        namespace, number = wg_event_ref.parse(ref)
    except wg_event_ref.BadRef as exc:
        return {}, str(ref), "", str(exc)
    found = wg_event_ref.resolve_id(number, namespace=namespace)
    if found is None:
        if namespace == wg_event_ref.EVENT:
            return {}, str(ref), "", (
                f"事件流里没有 id={number} 的记录（事件流只收 @ 与关键词命中的消息）；"
                f"要读上下文行 / 追问行里的那条请写 --event q{number}")
        return {}, str(ref), "", (
            f"会话库里没有 q{number} 这条（可能超过 1000 条被挤掉，或这个 id 从未存在）")
    token = wg_event_ref.format_ref(found.namespace, number)
    return dict(found.payload, id=number), token, found.source, ""


def stamp(ts):
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime((ts or 0) / 1000))
    except (OSError, ValueError):
        return "--:--:--"


def load_index():
    """``{(记号, index): row}``，后写覆盖先写。

    键用记号而不是裸 id（issue #203）：``7`` 与 ``q7`` 是两条不同的消息，用裸 id
    当键时后下载的会覆盖先下载的，``--recent`` 于是报出一个不属于它的路径。
    老行没有 ``ref`` 字段，按裸 ``event_id`` 折算——那时也还没有两套编号。
    """
    rows = {}
    try:
        lines = INDEX.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
            token = str(row.get("ref") or row["event_id"])
            rows[(token, int(row.get("index") or 0))] = row
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


def looks_like_video(raw):
    """魔数像不像视频容器。**只用于说实话**，不用于拒收。

    名字是群友给的（``abc123.mp4`` 也可能是个 zip），所以「存成什么扩展名」按名字
    走、让人打得开；「它到底是不是视频」按魔数说，两者分开，才不会出现
    「明明是压缩包，却一路被当成视频去抽帧」这种自欺。
    """
    if not raw:
        return False
    if any(raw.startswith(magic) for magic, _ in MAGIC_EXT):
        return True
    return len(raw) > 12 and raw[4:8] == b"ftyp"


def _cache_entries():
    """缓存里每一项：``(最久未用时刻, 字节数, 路径)``；抽帧目录算**一项**。

    一帧一帧地淘汰只会留下半个视频的两张图，读者分不清「这视频就两帧」还是
    「第三帧被删了」——所以目录整体算一项、整体删。
    """
    items = []
    if not VIDEOS.is_dir():
        return items
    for path in VIDEOS.iterdir():
        if path.name == "index.jsonl" or path.name.endswith(".tmp"):
            continue
        if path.is_dir():
            size, newest = 0, 0.0
            for frame in path.rglob("*"):
                try:
                    info = frame.stat()
                except OSError:
                    continue
                if not frame.is_file():
                    continue
                size += info.st_size
                newest = max(newest, info.st_mtime)
            items.append((newest, size, path))
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        items.append((info.st_mtime, info.st_size, path))
    return items


def _holds(path, keep):
    """``keep`` 是这一项本身，或落在这一项里面。"""
    try:
        keep.relative_to(path.resolve())
        return True
    except ValueError:
        return False


def prune(keep_path=None):
    """把缓存压回 `CACHE_BYTES` 以内：最久未用的先删，``keep_path`` 永不删。

    索引不动——文件没了之后下次读取会按 ``local_path`` 不存在的形态报出来，
    读者据索引仍知道「这条视频曾经存在过、现在被淘汰了」。

    改前只 ``glob("*")`` 顶层文件、``is_file()`` 把目录滤掉，P3 的抽帧产物
    因此从不进预算（``videos/<记号>-<i>-frames/`` 一直涨）；issue #200 要求的
    「自动淘汰、防止磁盘膨胀」对它完全没生效。
    """
    entries = _cache_entries()
    total = sum(size for _, size, _ in entries)
    if total <= CACHE_BYTES:
        return
    keep = Path(keep_path).resolve() if keep_path else None
    for _, size, path in sorted(entries):
        if total <= CACHE_BYTES:
            break
        if keep is not None and _holds(path, keep):
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
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


def save(ref, event_id, index, raw, *, name="", sender="", ts=None, source=""):
    """写盘 + 索引，返回索引行。同内容复用旧文件，不重复占空间。"""
    VIDEOS.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(raw).hexdigest()
    reused = find_local_by_sha(sha)
    if reused:
        local = reused
    else:
        target = VIDEOS / f"{ref}-{index}.{ext_for(name, raw)}"
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)
        local = str(target)
    row = dict(ref=str(ref), event_id=int(event_id), index=int(index),
               state="downloaded", sha256=sha, bytes=len(raw), name=name,
               local_path=local, sender=sender, ts=ts, source=source, at=time.time())
    upsert([row])
    prune(keep_path=local)
    return row


def fresh_url(event, index, name=""):
    """向 NapCat 要一份新鲜地址，返回 ``(url, 拿不到的原因)``。

    **先按名字匹配，不按下标猜**：事件流的 ``labels()`` 还会遍历引用原文与转发
    节点，而 ``get_msg`` 只回这一条消息自己的段——两边的下标集合并不相同。名字
    对不上时**返回空串并说明**，而不是退回下标：拿另一条视频的地址顶替，比读不到
    更糟（Agent 会把别的内容当成这条的结论）。只有段上根本没有名字时才退回下标，
    那时段数至少对得上，比凭位置猜强。

    失败原因要分得清（「栈没起」与「链接过期」是两件事）：重签根本没试成时，
    不能说成「地址已过期」。
    """
    mid = event.get("onebot_message_id") or event.get("message_id")
    if not mid:
        return "", "事件上没有 onebot_message_id，这条没试过重签"
    try:
        import wg_up_wake  # noqa: PLC0415  延迟导入：只有重签时才需要 OneBot
        data = wg_up_wake._onebot_call("get_msg", {"message_id": int(mid)})
    except Exception:  # noqa: BLE001  重签失败与「没地址」同义，不能崩
        return "", "OneBot 不答（栈没起或 NapCat 离线），这条没试过重签"
    if not isinstance(data, dict):
        return "", "OneBot 返回的不是消息体，这条没试过重签"
    message = data.get("message")
    if not isinstance(message, list):
        return "", "OneBot 没给这条消息的段，这条没试过重签"
    event_like = dict(id=event.get("id"), segments=[
        dict(type="unsupported", kind=seg.get("type"), raw=json.dumps(seg.get("data") or {},
                                                                      ensure_ascii=False))
        for seg in message if isinstance(seg, dict)])
    items = wg_videos.labels(event_like)
    if not items:
        return "", "这条消息现在没有视频段（可能已撤回或被替换）"
    wanted = str(name or "").strip().lower()
    if wanted:
        for item in items:
            if wanted in (item["name"].lower(), item["file"].lower()):
                return item["url"], ""
        return "", f"重签拿到的视频与这条对不上（找 {name}），不拿别的视频顶替"
    if index >= len(items):
        return "", f"这条消息只有 {len(items)} 个视频段，没有 index={index}"
    return items[index]["url"], ""


def fetch(ref, index, max_mb):
    """下载第 ``index`` 个视频段。返回进程退出码。"""
    event, token, _, problem = resolve_event(ref)
    if not event:
        print(problem or f"本地没有 {ref} 这条事件，无法下载。")
        return 1
    items = wg_videos.labels(event)
    if index >= len(items):
        print(f"{token} 只有 {len(items)} 个视频段，没有 index={index}。")
        return 1
    item = items[index]
    cached = load_index().get((token, index)) or {}
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
        renewed, why = fresh_url(event, index, item["name"])
        if renewed and renewed != item["url"]:
            tried += 1
            try:
                raw = wg_download.download(renewed, max_bytes=limit, timeout=DOWNLOAD_TIMEOUT)
                print(f"  重签地址：{len(raw)} 字节")
            except wg_download.DownloadError as exc:
                error = str(exc)
                print(f"  重签地址失败：{error}")
        else:
            # 重签没试成与「重签了也过期」是两件事，话必须分开说。
            print(f"  未能重签：{why or '重签拿到的地址与存档地址相同'}。")
    if raw is None:
        if not tried:
            print("这条视频没有下载地址，也没有可重签的消息编号。")
        else:
            print(f"下载失败：{error or '未知原因'}。"
                  "存档链接已过期时，重签成功就能下；重签也失败就只能请群友重发——"
                  "这是 QQ 多媒体 CDN 的 rkey 寿命，不是本机问题。")
        return 1
    row = save(token, event.get("id") or 0, index, raw, name=item["name"],
               sender=event.get("sender") or "", ts=event.get("timestamp"),
               source="renewed" if tried > len(attempts) else "stored")
    print(f"{row['local_path']}")
    print(f"字节：{row['bytes']}，sha256：{row['sha256'][:12]}…")
    if not looks_like_video(raw):
        print("注意：下回来的字节不像视频（MP4/WebM 魔数都对不上），已按原文件名存下来。"
              "抽帧大概率会失败，但文件本身可以直接打开看它到底是什么。")
    else:
        print(f"下一步看内容：video.py --frames --event {token} --index {index}")
    return 0


def frames(ref, index):
    """抽帧：已缓存的视频就地抽，没缓存先下再抽。返回进程退出码。

    与 ``fetch`` 分开是刻意的——下载可能因 rkey 过期失败，而抽帧是在**已有文件**
    上做；把两件事混成一个函数会让「只想重抽一次」也必须走下载分支。
    """
    import wg_frames  # noqa: PLC0415  延迟导入：不抽帧的运行不需要探测 ffmpeg

    event, token, _, problem = resolve_event(ref)
    if not event:
        print(problem or f"本地没有 {ref} 这条事件，无法抽帧。")
        return 1
    cached = load_index().get((token, index)) or {}
    local = cached.get("local_path") if cached.get("local_path") else ""
    if not local or not Path(local).is_file():
        print(f"{token} i={index} 还没下载到本地，先下载。")
        code = fetch(ref, index, MAX_MB)
        if code != 0:
            return code
        cached = load_index().get((token, index)) or {}
        local = cached.get("local_path") or ""
    out = VIDEOS / f"{token}-{index}-frames"
    result = wg_frames.extract(local, out, stem=f"{token}-{index}")
    prune(keep_path=out)
    print(wg_frames.render(result, event_id=token, index=index))
    return 0 if result["ok"] else 1


def recent(limit):
    rows = sorted(load_index().values(), key=lambda r: (r.get("at") or 0), reverse=True)
    if not rows:
        print("最近没有视频记录")
        return 0
    for row in rows[:max(1, limit)]:
        exists = row.get("local_path") and Path(row["local_path"]).is_file()
        state = "已缓存" if exists else "文件已淘汰"
        print(f"{row.get('ref') or row['event_id']} i={row['index']} [{state}] "
              f"{stamp(row.get('ts'))} {row.get('sender') or '?'} "
              f"{row.get('name') or ''} {row.get('local_path') or ''}")
    return 0


def list_candidates():
    """列出事件流里所有视频段及本地状态；只读，不下载。"""
    rows = events()
    shown = 0
    for event in reversed(rows):
        items = wg_videos.labels(event)
        if not items:
            continue
        token = str(event.get("id") or 0)
        for item in items:
            row = load_index().get((token, item["index"])) or {}
            local = row.get("local_path") if row.get("local_path") and Path(row["local_path"]).is_file() else ""
            state = f"已缓存 {local}" if local else ("可下载" if item["url"] else "无下载地址")
            print(f"{token} i={item['index']} {stamp(event.get('timestamp'))} "
                  f"{event.get('sender') or '?'} {item['name'] or '(无名)'} "
                  f"{item['size'] or '?'} {state}")
            shown += 1
    if not shown:
        print("事件流里没有视频段")
    return 0


def main(argv=None):
    winconsole.ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="按需下载群聊视频/文件段并落盘")
    parser.add_argument(
        "--event",
        help="消息记号：唤醒行 / --list 的裸数字（7）或上下文行 / 追问行的 q7；"
             "也可写 id=7 / qid=7")
    parser.add_argument("--index", type=int, default=0, help="视频段下标，默认 0")
    parser.add_argument("--max-mb", type=int, default=MAX_MB, help=f"单文件上限 MB，默认 {MAX_MB}")
    parser.add_argument("--recent", type=int, help="列出最近 N 条下载记录")
    parser.add_argument("--list", action="store_true", help="列出事件流里的视频段，不下载")
    parser.add_argument("--frames", action="store_true",
                        help="抽首/中/尾三帧 PNG（需要 ffmpeg），配合 --event/--index")
    args = parser.parse_args(argv)
    if args.event is not None:
        if args.frames:
            return frames(args.event, args.index)
        return fetch(args.event, args.index, args.max_mb)
    if args.recent is not None:
        return recent(args.recent)
    if args.list:
        return list_candidates()
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
