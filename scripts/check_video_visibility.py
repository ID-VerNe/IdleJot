"""issue #200 门禁：视频段必须可执行地到达 Agent，下载判据不得比服务端松。

背景：#200 报的是「群友发视频，Agent 只看到 [视频]」。采集层保真降级（#165）把
``url`` 留在了 ``unsupported.raw`` 里，但唤醒行只印中文标签——「可下载」不是一句
状态词，必须带具体命令，否则 Agent 每次都要现场试错。

本门禁钉住四件事：

  1. 唤醒行 / ``--list`` / ``--show`` 三处都带 ``video.py --event <id> --index <i>``；
  2. 没有下载地址时明说「无下载地址」，不许留一个裸 ``[视频]``；
  3. ``file`` 段只有在名字像视频时才当视频，普通文档不冒充；
  4. daemon 侧的下载判据与 ``chatlocal/mcp_media_transport.py`` 同值（重定向上限、
     超时、拒绝非公网、只认 http/https）——判据漂移就是安全边界漂移。

    .venv/Scripts/python.exe scripts/check_video_visibility.py
"""
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False)

TMP = scratch_dir(prefix="wg_video_")
STATE = TMP / "state"
STATE.mkdir(parents=True)
SID = "session-fixture"
SELF = "10001"

os.environ["TULPA_WG_STATE"] = str(STATE)

import wg_download  # noqa: E402
import wg_videos  # noqa: E402
import video  # noqa: E402

NOW_MS = int(time.time() * 1000)

EID_OK = 9101        # video 段，带 url
EID_NOURL = 9102     # video 段，没有 url（明说无地址）
EID_FILE_MP4 = 9103  # file 段，名字 .mp4 → 当视频
EID_FILE_TXT = 9104  # file 段，名字 .txt → 不是视频
EID_PLAIN = 9105     # 纯文字
EID_WAKE = 9106      # watch 唤醒行端到端

VIDEO_URL = "https://media.example.test/v.mp4"


def video_segment(url=VIDEO_URL, kind="video", name=None, size="3418642"):
    data = {"file": "abc123.mp4", "file_size": size}
    if url:
        data["url"] = url
    if name:
        data["name"] = name
    return {"type": "unsupported", "kind": kind,
            "raw": json.dumps(data, ensure_ascii=False)}


def event_of(event_id, segments, content="[视频]"):
    return {"id": event_id, "sender": "群友丁", "content": content,
            "timestamp": NOW_MS, "is_self": False, "segments": segments}


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def 有地址时带可执行命令():
    text = wg_videos.render(event_of(EID_OK, [video_segment()]))
    return (f"video.py --event {EID_OK} --index 0" in text
            and "可下载" in text and text != "[视频]")


@case
def 无地址时明说下不了():
    text = wg_videos.render(event_of(EID_NOURL, [video_segment(url="")]))
    return "无下载地址" in text and "可下载" not in text


@case
def file_段名字像视频才算():
    hit = wg_videos.render(event_of(EID_FILE_MP4, [
        video_segment(kind="file", name="cat.mp4")]))
    miss = wg_videos.render(event_of(EID_FILE_TXT, [
        video_segment(kind="file", name="readme.txt")]))
    return ("可下载" in hit) and miss == ""


@case
def 纯文字不长出标记():
    return wg_videos.render(event_of(EID_PLAIN, [{"type": "text", "text": "在吗"}])) == ""


@case
def 引用里的视频也认():
    """群友引用一条视频再问「这是啥」：只看顶层段会判成没有视频。"""
    event = event_of(EID_OK, [{"type": "reply", "onebot_message_id": "1"},
                              {"type": "text", "text": "这是啥"}],
                     content="[引用 QQ 消息 1]这是啥")
    event["referenced_messages"] = [{"onebot_message_id": "1",
                                     "segments": [video_segment()]}]
    return "可下载" in wg_videos.render(event)


@case
def 大小渲染与缺失():
    big = wg_videos.human_size(3418642)
    return big.endswith("M") and wg_videos.human_size(0) == "" and wg_videos.human_size("x") == ""


@case
def 下载判据与服务端同值():
    """安全边界漂移是 P1 级缺陷：这里的数必须与 chatlocal 那份一致。"""
    sys.path.insert(0, str(ROOT))
    from chatlocal import mcp_media_transport as server
    return (wg_download.MAX_REDIRECTS == server.MAX_REDIRECTS
            and wg_download.DEFAULT_TIMEOUT == server.DOWNLOAD_TIMEOUT_SECONDS)


@case
def 非公网地址一律拒绝():
    """拒绝还不够——必须说对原因。环回地址曾被报成「形态无法判定」，判据没错
    但话是错的，Agent 会据此去改 DNS 而不是放弃下载。"""
    for host in ("127.0.0.1", "localhost", "10.0.0.1", "169.254.1.1"):
        try:
            wg_download._public_addresses(host, 80)
        except wg_download.DownloadError as exc:
            if "非公网" not in str(exc):
                print(f"    {host} 的拒绝原因不对：{exc}")
                return False
            continue
        return False
    return True


@case
def 只认_http_https():
    for url in ("file:///C:/x.mp4", "ftp://example.test/x.mp4", ""):
        try:
            wg_download._target(url)
        except wg_download.DownloadError:
            continue
        return False
    return True


@case
def 超限文件不下载():
    """声明长度超限时在建连后立刻拒，不等读完。"""
    try:
        wg_download.download(VIDEO_URL, max_bytes=1, timeout=1)
    except wg_download.DownloadError:
        return True
    except Exception:  # noqa: BLE001  网络不可达也算「没下成」，但不该算通过
        return False
    return False


@case
def 唤醒行端到端带命令():
    (STATE / "wg_events.jsonl").write_text(json.dumps({
        "id": EID_WAKE, "sender": "群友丁", "content": "[视频] 看这个",
        "timestamp": NOW_MS, "is_self": False,
        "segments": [video_segment(), {"type": "at", "qq": SELF}],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    import watch
    import wg_up_wake
    real = wg_up_wake.config
    wg_up_wake.config = lambda: {"self_account": SELF, "self_names": ["小鲸鱼"],
                                 "slash_commands": {"prefix": "#"}}
    argv = sys.argv
    sys.argv = ["watch.py", "--timeout", "2", "--replay"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = watch.main()
    finally:
        sys.argv = argv
        wg_up_wake.config = real
    text = buf.getvalue()
    return (code == 0 and "WAKE" in text
            and f"video.py --event {EID_WAKE} --index 0" in text)


@case
def 列表入口读得到视频段():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        video.list_candidates()
    text = buf.getvalue()
    return f"{EID_WAKE} i=0" in text


def main():
    return fs.run_cases(CASES, setup=None, prefix=False)


if __name__ == "__main__":
    sys.exit(main())
