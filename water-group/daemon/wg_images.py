"""群聊图片的本地归属——接收侧落盘索引 + 发送侧出段（issue #27 / #128）。

MCP 的图片只在 content 里以 base64 交付，collector 收到后必须自己落盘，
否则 Agent 只能看到 ``[图片]`` 占位，看不了图（issue #27）。

目录布局（与 wg_paths 一致，测试可用 IDLEJOT_WG_STATE 隔离）::

    <state>/images/<event_id>-<image_index>.<ext>
    <state>/images/index.jsonl

index.jsonl 每次追加一行，读取时后写覆盖先写；事件与图片下标是主键。
像素只写一次：同一 sha256 已有文件时直接复用原路径，二次查看不重复下载。

发送侧（issue #128）：``read_for_send``（校验 + 读字节）、``image_problem``（整张
解码，解不出就给出原因）、``image_segment``（字节 → OneBot 段）、``attach_images``
（把图挂到气泡列表上）。放在这里而不是 ``reply.py``，一是 ``MAX_BYTES`` 本来就在
这，二是 ``reply.py`` 已经贴着 ``check_module_size`` 的 1000 行红线。
"""
import base64
import hashlib
import io
import json
import stat
import time
import warnings
from pathlib import Path

from wg_paths import state_dir

IMAGES = state_dir() / "images"
INDEX = IMAGES / "index.jsonl"
MAX_BYTES = 6 * 1024 * 1024       # 与 MCP 单张图上限一致
# 一次回复最多几张图：每条气泡之间 `reply.BUBBLE_GAP` 睡 1.2s，25 张就吃满
# `wg_budget.SEND_BUDGET`，宿主 120s 超时会把整条调用连同输出一起砍掉——那种
# 失败一个字都不打印。9 张的最坏形状是 3 条文字 + 9 张图 = 12 条气泡 = 11 个间隔
# 13.2s，再加 12 次 POST，仍在 30s 预算内。
MAX_IMAGES = 9
# 解码上限。6 MiB 的压缩包能塞进几百兆像素：10000x10000 的单色 PNG 只有 12 KiB，
# 整张解出来要 400 MiB。上限按**总像素**算（单帧也一样算进去），实测 80 MP 的图
# 解一遍 105ms、峰值 400 MiB；48 MP 的手机照片 60ms、253 MiB。再大就解不起。
# 帧数单独封顶：60000 帧的 16x16 GIF 只有 3.5 MiB、15 MP，像素上限拦不住，
# 但逐帧解码要 1.5s。
MAX_TOTAL_PIXELS = 80_000_000
MAX_FRAMES = 300
MAX_INDEX_ROWS = 4000             # 索引只保留最近若干条，避免无限增长
SEND_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")

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


def render(event, media=None, ref_name=None):
    """渲染成一行图片标记，例如 ``[图片 i=0 path=C:\\...\\12-0.jpg]``。

    「可补读」必须带上具体命令（issue #35）：裸「可补读」三个字不是可执行指引，
    Agent 每次都要现场试错才能想起 look.py。同理，占位在、记录全无也要明说
    「未登记」并给出命令——不靠不印表达失败。

    ``ref_name`` 是命令里要打给 Agent 抄的记号（issue #203）：事件流的行仍打裸
    ``--event 7``；会话库的行（追问行 / 上下文行）必须打 ``--event q7``，否则同一
    个数字在撞号时会指向另一次事件的图。省略时退回裸 id，与改前一致。
    """
    parts = []
    event_id = int(event.get("id") or 0)
    ref = event_id if ref_name is None else ref_name
    for item in labels(event, media):
        suffix = f" 引自 {item['quote']}" if item.get("quote") else ""
        hint = f"look.py --event {ref} --index {item['index']}"
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
            parts.append(f"[图片 未登记 look.py --event {ref} 补读]")
    return "".join(parts)


# ---------------------------------------------------------------- 发送侧（issue #128）


class ImageError(ValueError):
    """本地图片不能发：原因已经是可以直接给 Agent 看的一句话。"""


def read_for_send(path):
    """读一张待发图片，返回 (bytes, 显示名)。

    闸门按顺序：路径非空 → 存在且是普通文件 → ≤ ``MAX_BYTES`` → 读回来的字节数
    与 ``stat`` 一致（防下载器写到一半）→ **Pillow 整张解得开**。

    最后一道是判据本身，细节见 ``image_problem``：``ext_for`` 的魔数只看前 4 个
    字节，一个 4 字节的 ``\\x89PNG`` 也能过；``Image.open()`` 只看文件头；而
    ``verify()`` 在 JPEG/GIF/WebP 上是空操作。三者都会放半张图过关，所以这里真的
    逐帧解一遍。扩展名从头到尾不参与判断——``.jfif`` / 大写 ``.PNG`` / 改了名的
    文本都骗不过去。

    依赖方向仍是「只用已有依赖」：Pillow 早在 ``pyproject.toml`` 里，MCP 侧的
    ``mcp_media_pixels.pixels()`` 用它做同一件事，这里不另写解码器。上限之内的图
    整张解 15~105ms，够不上瓶颈。

    ``stat`` 与 ``read`` 之间文件可能被换掉（下载器正在写），所以按上限 +1 读回来
    再核长度：变大了撞上限、变小了说明读的是写了一半的内容，两种都不发。

    只回显示名（``Path.name``），完整路径不进群也不进日志——它常带用户名。
    """
    if not str(path).strip():
        raise ImageError("图片路径是空的")
    target = Path(path).expanduser()
    try:
        info = target.stat()
    except OSError:
        raise ImageError(f"读不到 {target.name}：路径不存在或没有权限") from None
    if not stat.S_ISREG(info.st_mode):
        raise ImageError(f"{target.name} 不是普通文件")
    if info.st_size <= 0:
        raise ImageError(f"{target.name} 是空文件")
    if info.st_size > MAX_BYTES:
        raise ImageError(f"{target.name} 超过单张上限 {MAX_BYTES // (1024 * 1024)} MiB")
    try:
        with open(target, "rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
    except OSError:
        raise ImageError(f"读不到 {target.name}：路径不存在或没有权限") from None
    if not raw:
        raise ImageError(f"{target.name} 是空文件")
    if len(raw) > MAX_BYTES:
        raise ImageError(f"{target.name} 超过单张上限 {MAX_BYTES // (1024 * 1024)} MiB")
    if len(raw) != info.st_size:
        raise ImageError(f"{target.name} 在读取时被改动（{info.st_size} → {len(raw)} 字节），"
                         "确认文件写完再发")
    problem = image_problem(raw)
    if problem:
        raise ImageError(f"{target.name} 不是能解码的 PNG/JPEG/GIF/WebP 图片（{problem}）")
    return raw, target.name


def image_problem(raw):
    """把这张图整张解一遍：能发返回 None，不能发返回一句原因。

    ``Image.open()`` 只读文件头，而 ``verify()`` 只对 PNG 真的校验结构——JPEG /
    GIF / WebP 用的是 ``ImageFile.verify``，它只是关掉文件句柄（Pillow 13.0.0）。
    半截的 JPEG 于是能一路过关，发进群就是一张裂图。所以判据必须是**逐帧解码**：
    真解得出才算能发。

    边界说清楚：**解不出整张图**的都在这里落马（截断的 JPEG / WebP、坏结构、
    只有文件头）；但「解得出、只是少了尾部」的会过——丢 IEND 的 PNG 照样解出完整
    画面，帧边界处截断的 GIF 只是帧数变少（60 帧截到 95% 报 57 帧）。这两种发进群
    是一张完整但可能短一点的图，不是裂图，所以不算缺陷；要拦它们得自己解析容器
    尾部，代价与收益不成比例。

    解码顺带把资源钉住：总像素与帧数两个上限。压缩率高的图能把几百兆像素塞进
    几十 KiB，不设限就是拿发图当解压炸弹的入口。

    异常不按类型枚举：Pillow 各插件的抛法没有清单（截断抛 ``OSError``、坏结构抛
    ``SyntaxError``、坏帧表抛 ``IndexError``、坏 GIF 子块抛 ``struct.error``），
    漏一种就是崩在 Agent 面前。解码范围内任何异常都等价于「解不开」。
    """
    from PIL import Image, UnidentifiedImageError  # 延迟导入：接收侧不碰图片解码

    try:
        with warnings.catch_warnings():
            # 超限的图下面按像素数自己拒，这里不需要 Pillow 再往 stderr 喊一句。
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as im:
                fmt = (im.format or "").upper()
                if fmt not in SEND_FORMATS:
                    return f"识别为 {fmt or '未知'}，只能发 PNG/JPEG/GIF/WebP"
                width, height = im.size
                frames = getattr(im, "n_frames", 1)
                if frames > MAX_FRAMES:
                    return f"{frames} 帧，超过 {MAX_FRAMES} 帧上限"
                pixels = width * height * frames
                if pixels > MAX_TOTAL_PIXELS:
                    return (f"{width}×{height} 共 {frames} 帧 = {pixels / 1e6:.0f} 兆像素，"
                            f"超过 {MAX_TOTAL_PIXELS // 1_000_000} 兆像素上限")
                for index in range(frames):
                    im.seek(index)
                    im.load()
        return None
    except UnidentifiedImageError:
        return "内容不像图片"
    except Exception as exc:  # noqa: BLE001 —— 见上面那段：解码异常没有清单
        return f"解不开：{type(exc).__name__}: {str(exc)[:60]}"


def image_segment(raw):
    """字节 → OneBot 图片段，与 ``qr_login.qr_image_segment()`` 同形状。

    ``base64://`` 是 NapCat 已经实机验证过的形状（``doc/SLASH_COMMANDS.md:323``，
    ``send_group_msg`` 回 ``retcode=0``），不需要退 ``file://`` 落盘方案。
    """
    return {"type": "image",
            "data": {"file": "base64://" + base64.b64encode(raw).decode("ascii")}}


def attach_images(bubble_segments, paths, cap=None):
    """把本地图片挂成额外的气泡，返回新的气泡列表。

    先校验**全部**路径再动手：一条坏路径不该让前面几张图发出去、后面几张不发，
    那正是「发了一半」的失败模式。多个坏路径合成一条 ``ImageError``，Agent 改一次
    就能跑通，不用修一个跑一次。

    每张图独占一条气泡，排在文字之后——混在文字气泡里的话，QQ 那边图片会把文字
    挤成一句半截话。纯图时 ``split_bubbles("")`` 会给出一个空文字气泡，这里丢掉它，
    否则群里会多一条空消息。

    张数上限 ``MAX_IMAGES``：气泡之间要睡 ``BUBBLE_GAP``，图越多越接近宿主的工具
    超时，而超时被砍时这条路径上一个字都不打印。

    ``cap`` 是「这次一共几条气泡」的上限（调用方传 ``reply --max-bubbles`` 的实际
    取值）：图也是气泡，正文 20 行 + 9 张图 = 29 条 = 33.6s 间隔，已经越过
    ``wg_budget.SEND_BUDGET=30``。所以超了要在发之前拒——不是把图悄悄截掉。
    """
    if not paths:
        return bubble_segments
    if len(paths) > MAX_IMAGES:
        raise ImageError(f"一次最多发 {MAX_IMAGES} 张图（这次给了 {len(paths)} 张），"
                         "分几次发")
    raws, problems = [], []
    for path in paths:
        try:
            raws.append(read_for_send(path))
        except ImageError as exc:
            problems.append(str(exc))
    if problems:
        raise ImageError("；".join(problems))
    kept = [segs for segs in bubble_segments if segs]
    if cap is not None and len(kept) + len(raws) > int(cap):
        raise ImageError(f"正文 {len(kept)} 条气泡 + {len(raws)} 张图 = "
                         f"{len(kept) + len(raws)} 条，超过这次的气泡上限 {int(cap)} 条"
                         "（气泡间隔 1.2s 会吃掉发送预算）；减少正文行数或图再发")
    for raw, name in raws:
        print(f"  image: {name} {len(raw)} 字节")
    return kept + [[image_segment(raw)] for raw, _ in raws]
