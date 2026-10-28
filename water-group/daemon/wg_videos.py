"""群聊视频/文件段的识别与唤醒行标记（issue #200 P1）。

背景：群友发来的短视频（mp4）在 OneBot 里是 ``video`` / ``file`` 段。当前
water-group 授权没有 ``chat_video`` / ``chat_files``，于是它们走到
``chatlocal/mcp_chat.py:452`` 的 **保真降级**分支，落成::

    {"type": "unsupported", "kind": "video",
     "raw": "{\"file\": \"…mp4\", \"file_size\": \"3418642\", \"url\": \"https://…\"}"}

``raw`` 里有下载地址，但唤醒行只印一个裸 ``[视频]``，Agent 无从补读——这正是
issue #200 报的那一屏。本模块只做两件事，**不下载、不解析、不执行**：

* :func:`video_segments` —— 从事件里找出视频/文件段（含转发节点与引用原文）；
* :func:`render` —— 产出一行可执行标记，形如::

      [视频 name=cat.mp4 size=3.3M 可下载 video.py --event 10123 --index 0]

与 ``wg_images.render`` 同一层、同一纪律（issue #35）：**「可补读」必须带具体
命令**，缺信息就明说缺什么，绝不印一个裸占位让 Agent 现场试错。

**rkey 寿命（issue #200 探索结论）**：QQ 多媒体 CDN 的下载地址带 ``rkey``，
实测约 8.8 小时后就返回 ``retcode -5503``。所以「段里有 URL」不等于「现在下
得到」——本模块只负责**说清楚有没有地址**，能不能下、下不到怎么补救由
``video.py`` 在真正下载时判定并在失败时说出来。这里不预判，预判会把「8 小时
前存的链接」当成「现在不可下」，反而挡住 ``get_msg`` 重签那条路。
"""
import json

# 与 ``chatlocal/mcp_qq_media.py:69`` 的容器判据一致：MP4 家族（含 .mov 的
# ``ftyp``）/ WebM 的 EBML 头。不做完整解码，只用于「这是不是视频」的第一次
# 分拣；真正的完整性由 ``video.py`` 在落盘后按需验证。
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
FILE_KINDS = ("video", "file")
NAME_CHARS = 60          # 文件名进唤醒行，够认出是哪个，不占正文额度
SIZE_UNITS = ("B", "K", "M", "G")


def _as_int(value):
    """``file_size`` 在 OneBot 里有 int 也有 str 两种形状，统一成 int 或 0。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def human_size(size):
    """字节数 → ``3.3M`` 这种短形态；0 表示不知道，返回空串。"""
    size = _as_int(size)
    if size <= 0:
        return ""
    value = float(size)
    for unit in SIZE_UNITS:
        if value < 1024 or unit == SIZE_UNITS[-1]:
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}"
        value /= 1024
    return ""


def _payload(segment):
    """段上那份 ``data``：``raw`` 串优先（#165 的保真形态），退回 ``data`` 字典。"""
    raw = segment.get("raw")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    data = segment.get("data")
    return data if isinstance(data, dict) else {}


def _is_video_like(segment, data):
    """是不是「值得下载的视频/文件段」。

    ``kind`` 只认 ``video`` / ``file`` 两种；``file`` 还要求名字像视频，否则
    那是一个普通文档（Agent 有别的路），不该在唤醒行冒充视频。``video`` 段
    不看名字——QQ 的手机视频有时根本没有 ``name`` 字段。
    """
    if segment.get("kind") not in FILE_KINDS:
        return False
    if segment.get("kind") == "file":
        name = str(data.get("name") or data.get("file") or "").lower()
        return name.endswith(VIDEO_EXTS) if name else False
    return True


def video_segments(value):
    """递归产出视频/文件段，顺序与 ``wg_images.image_segments`` 一致。

    引用原文与转发节点都要看：群友常常引用一条视频再问「这是啥」，只看顶层
    段就会把这种情况判成「没有视频」。
    """
    for segment in value.get("segments", []) or []:
        if not isinstance(segment, dict):
            continue
        if _is_video_like(segment, _payload(segment)):
            yield segment
        for node in segment.get("nodes", []) or []:
            yield from video_segments(node)
    for detail in value.get("referenced_messages", []) or []:
        yield from video_segments(detail)
    for node in value.get("nodes", []) or []:
        yield from video_segments(node)


def describe(segment):
    """单个段的元信息：名字、大小、有没有下载地址。找不到就说空串。"""
    data = _payload(segment)
    name = str(data.get("name") or data.get("file") or "").strip()
    return dict(
        kind=segment.get("kind") or "video",
        name=name[:NAME_CHARS],
        size=human_size(data.get("file_size") or data.get("size")),
        bytes=_as_int(data.get("file_size") or data.get("size")),
        url=str(data.get("url") or ""),
        file_id=str(data.get("file_id") or ""),
        file=str(data.get("file") or ""),
    )


def labels(event):
    """一条事件里所有视频/文件段的元信息，按发现顺序编号。"""
    found = []
    for index, segment in enumerate(video_segments(event)):
        item = describe(segment)
        item["index"] = index
        found.append(item)
    return found


def render(event):
    """渲染成一行视频标记；没有视频段返回空串（不长出噪音）。

    三种形态都说出事实：

    * 有地址 → ``可下载 video.py --event <id> --index <i>``
    * 没地址 → ``无下载地址``（明说下不了，不让 Agent 反复试）
    * 名字/大小缺失 → 缺哪项不印哪项，但命令始终在
    """
    event_id = int(event.get("id") or 0)
    parts = []
    for item in labels(event):
        who = f" name={item['name']}" if item["name"] else ""
        size = f" size={item['size']}" if item["size"] else ""
        hint = f"video.py --event {event_id} --index {item['index']}"
        if item["url"] or item["file_id"]:
            parts.append(f"[视频{who}{size} 可下载 {hint}]")
        else:
            parts.append(f"[视频{who}{size} 无下载地址]")
    return "".join(parts)
