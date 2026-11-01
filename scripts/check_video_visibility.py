"""issue #200 门禁：视频段必须可执行地到达 Agent，下载判据不得比服务端松。

背景：#200 报的是「群友发视频，Agent 只看到 [视频]」。采集层保真降级（#165）把
``url`` 留在了 ``unsupported.raw`` 里，但唤醒行只印中文标签——「可下载」不是一句
状态词，必须带具体命令，否则 Agent 每次都要现场试错。

本门禁钉住七件事：

  1. **四处入口**（唤醒行 / ``--list`` / ``--show`` / ``--context`` + 追问行）都带
     ``video.py --event <记号> --index <i>``，且记号与行首一致（``q<n>`` 就是 ``q<n>``）；
  2. 没有下载地址时明说「无下载地址」，不许留一个裸 ``[视频]``；
  3. ``file`` 段只有在名字像视频时才当视频，普通文档不冒充；
  4. daemon 侧的下载判据与 ``chatlocal/mcp_media_transport.py`` 同值（重定向上限、
     超时、拒绝非公网、只认 http/https）——判据漂移就是安全边界漂移；
  5. 抽帧：ffmpeg 缺失时**明说缺什么**、时长未知时只抽首帧而不是猜、
     短时长去重不抽三张一样的图；抽帧产物也进缓存预算（否则永不淘汰）；
  6. 记号口径（issue #203 / #216）：``q<n>`` 读会话库、裸数字只认事件流且不猜库，
     重签按名字匹配、对不上就说对不上，不拿别的视频顶替；
  7. 非视频字节落盘要说明（存成 ``.bin``，不假装它是视频）。

抽帧那组用假子进程桩（``winconsole.run_hidden``）与假 ffmpeg 可执行文件，所以
**CI 上没装 ffmpeg 也能验**——否则「本机有、CI 没有」会让这组静默跳过。

    .venv/Scripts/python.exe scripts/check_video_visibility.py
"""
import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import pin_conversation, pin_state_env, scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False)

TMP = scratch_dir(prefix="wg_video_")
DB = TMP / "fixture.sqlite3"
STATE = TMP / "state"
STATE.mkdir(parents=True)
SID = "session-fixture"
SELF = "10001"
GROUP = "123"

os.environ["TULPA_WG_DB"] = str(DB)
pin_state_env(STATE)  # state 两腿都钉（#281：IDLEJOT 先于 TULPA，单钉一条会被外部穿透）
# 本会话只管 GROUP：不钉的话，本机 config.json 恰好只有一个启用群时 wg_isolation
# 会落到 config:唯一启用群，唤醒行用例里没有 conversation_id 的事件被当成外群滤空
# （issue #316）。钉了之后本机与干净 checkout 走同一条路。
pin_conversation(f"{SELF}:group:{GROUP}", STATE)

import wg_download  # noqa: E402
import wg_event_ref  # noqa: E402
import wg_frames  # noqa: E402
import wg_videos  # noqa: E402
import video  # noqa: E402

NOW_MS = int(time.time() * 1000)

EID_OK = 9101        # video 段，带 url
EID_NOURL = 9102     # video 段，没有 url（明说无地址）
EID_FILE_MP4 = 9103  # file 段，名字 .mp4 → 当视频
EID_FILE_TXT = 9104  # file 段，名字 .txt → 不是视频
EID_PLAIN = 9105     # 纯文字
EID_WAKE = 9106      # watch 唤醒行端到端
EID_INBOX = 9201     # 只在会话库里的视频（q<n> 那条）
EID_CTX = 9202       # --context 行
EID_FUP = 9203       # 追问行
EID_BIN = 9204       # 下回来的字节不像视频
EID_RENEW = 9205     # 重签：名字对不上
EID_RENEW2 = 9206    # 重签：名字命中
EID_RENEW3 = 9207    # 重签：OneBot 不答

VIDEO_URL = "https://media.example.test/v.mp4"


def video_segment(url=VIDEO_URL, kind="video", name=None, size="3418642", file="abc123.mp4"):
    data = {"file": file, "file_size": size}
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


def build():
    """会话库夹具：两条只在 chat_inbox 里的视频（上下文行 / 追问行 / q<n> 都靠它）。"""
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    db.execute("CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id TEXT, message_id TEXT,"
               " payload TEXT, media TEXT NOT NULL DEFAULT '[]', details TEXT NOT NULL DEFAULT '[]',"
               " delivered INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL DEFAULT 0)")
    db.execute("INSERT INTO chat_sessions VALUES (?,?,1)", (SID, f"{SELF}:group:{GROUP}"))
    for event_id, message_id in ((EID_INBOX, "7101"), (EID_CTX, "7102"), (EID_FUP, "7103")):
        db.execute("INSERT INTO chat_inbox (id,session_id,message_id,payload,media,at)"
                   " VALUES (?,?,?,?,?,?)",
                   (event_id, SID, message_id, json.dumps({
                       "sender": "群友丁", "content": "[视频]",
                       "timestamp": NOW_MS, "is_self": False,
                       "segments": [video_segment()]}, ensure_ascii=False),
                    "[]", float(NOW_MS / 1000)))
    db.commit()
    db.close()
    (STATE / "wg_events.jsonl").write_text("", encoding="utf-8")


def append_event(event):
    with (STATE / "wg_events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


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


@contextlib.contextmanager
def pinned_conversation(group):
    """临时把本会话钉到 ``group``（反向自检用）：env 与 <state> 两腿一起改。"""
    import wg_isolation
    saved = {key: os.environ.get(key) for key in wg_isolation.CONVERSATION_ENV}
    file = STATE / wg_isolation.CONVERSATION_FILE
    existed = file.read_text(encoding="utf-8") if file.exists() else None
    try:
        pin_conversation(f"{SELF}:group:{group}", STATE)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if existed is None:
            file.unlink(missing_ok=True)
        else:
            file.write_text(existed, encoding="utf-8")


def run_watch(event_id, group=None, content="[视频] 看这个", segments=None):
    """写一条唤醒事件并进程内跑一次 ``watch.main()``，返回 ``(输出, 退出码)``。

    ``group`` 是反向自检用的临时会话群；默认走夹具钉死的那一个（issue #316）。
    事件自带 ``conversation_id``（#255：缺键一律当外群，夹具不该靠「缺键恰好
    被放行」通过），能不能被叫醒只取决于会话钉在哪个群。
    """
    (STATE / "wg_events.jsonl").write_text(json.dumps({
        "id": event_id, "sender": "群友丁", "content": content,
        "timestamp": NOW_MS, "is_self": False,
        "conversation_id": f"{SELF}:group:{GROUP}",
        "segments": segments if segments is not None
        else [video_segment(), {"type": "at", "qq": SELF}],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    import watch
    import wg_up_wake
    real = wg_up_wake.config
    wg_up_wake.config = lambda: {"self_account": SELF, "self_names": ["小鲸鱼"],
                                 "slash_commands": {"prefix": "#"}}
    argv = sys.argv
    sys.argv = ["watch.py", "--timeout", "2", "--replay"]
    buf = io.StringIO()
    pin = pinned_conversation(group) if group else contextlib.nullcontext()
    try:
        with pin, contextlib.redirect_stdout(buf):
            code = watch.main()
    finally:
        sys.argv = argv
        wg_up_wake.config = real
    return buf.getvalue(), code


@case
def 唤醒行端到端带命令():
    text, code = run_watch(EID_WAKE)
    if not (code == 0 and "WAKE" in text
            and f"video.py --event {EID_WAKE} --index 0" in text):
        return False
    # 反向自检（issue #316）：把会话钉到**别的群**时这条必须消失。否则等于把
    # #164 的读侧过滤关掉，本用例就再也不会红，门禁变成永远绿。
    # 退出码是 2（TIMEOUT）：没人 @ 本群，本来就没有唤醒可发——静默才是对的。
    other, _ = run_watch(EID_WAKE, group="999")
    return "WAKE" not in other and f"--event {EID_WAKE} " not in other


@case
def 列表入口读得到视频段():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        video.list_candidates()
    text = buf.getvalue()
    return f"{EID_WAKE} i=0" in text


# ---- 记号口径（issue #203 / #216）：四处入口的 --event 必须与行首一致 ----------


@case
def show_视频命令跟着首行记号走():
    """``--show q<n>`` 的 payload 里没有 ``id`` 键，改前这里打的是 ``--event 0``。"""
    import wg_text
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = wg_text.show_event(EID_INBOX, namespace=wg_event_ref.INBOX)
    text = buf.getvalue()
    return (code == 0 and f"video.py --event q{EID_INBOX} --index 0" in text
            and "--event 0 " not in text)


@case
def context_行带视频命令且打q号():
    """``--context`` 是补读口最常被用的入口，改前这里只剩裸 [视频]。"""
    import wg_context
    rows = wg_context.recent_chatter(10)
    hit = [r for r in rows if r.get("id") == EID_CTX]
    text = (hit[0].get("videos") or "") if hit else ""
    return (bool(hit) and f"video.py --event q{EID_CTX} --index 0" in text
            and f"video.py --event {EID_CTX} " not in text)


@case
def 追问行带视频命令且打q号():
    """追问窗口里发的视频同理：追问行 id 是库 rowid，命令必须打 ``q<n>``。"""
    import followup
    rows = followup.since(0, SID)
    hit = [r for r in rows if r.get("id") == EID_FUP]
    text = (hit[0].get("videos") or "") if hit else ""
    return (bool(hit) and f"video.py --event q{EID_FUP} --index 0" in text
            and f"video.py --event {EID_FUP} " not in text)


@case
def video_认q号读会话库那条():
    """``video.py --event q<n>`` 必须真的读到会话库里那条——#216 删掉
    ``wg_text._inbox_row`` 之后，改前的兜底是静默失效的：这里改前报「本地没有这条
    事件」，也就是群里随口发的视频全都补读不了。"""
    real = video.wg_download.download
    video.wg_download.download = lambda url, **kwargs: b"\x00\x00\x00\x18ftypmp42"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = video.fetch(f"q{EID_INBOX}", 0, 40)
    finally:
        video.wg_download.download = real
    text = buf.getvalue()
    return code == 0 and "字节：" in text and "本地没有" not in text


@case
def 裸数字不猜会话库():
    """裸数字只认事件流；没命中就明说去看 ``q<n>``，不拿这个数字去猜库。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = video.fetch(EID_INBOX, 0, 40)
    text = buf.getvalue()
    return code == 1 and f"q{EID_INBOX}" in text and "事件流" in text


@case
def 坏记号被拒绝():
    """``Q7`` 这类写错的记号要显式拒绝，不许猜成 7 去发。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = video.fetch("Q7", 0, 40)
    text = buf.getvalue()
    return code == 1 and ("拒绝" in text or "看不懂的记号" in text)


def _renew_fixture(event_id, name="cat.mp4", quoted=True):
    """造一条「存档地址已过期」的事件：下载只认重签回来的地址。"""
    event = dict(event_of(event_id, []), onebot_message_id="7201")
    segment = video_segment(url="", name=name)
    segment["raw"] = json.dumps({"file": name, "file_size": "3418642",
                                 "url": "https://media.example.test/stale.mp4"},
                                ensure_ascii=False)
    if quoted:
        event["referenced_messages"] = [{"segments": [segment]}]
    else:
        event["segments"] = [segment]
    append_event(event)


def _stale_download(fresh_url):
    """存档地址一律报过期（-5503 的形状），只有重签回来的地址给字节。"""
    def download(url, **kwargs):
        if url == fresh_url:
            return b"\x00\x00\x00\x18ftypmp42"
        raise wg_download.DownloadError("下载地址已过期或被拒（HTTP 400），需要重新取得链接")
    return download


@case
def 重签按名字匹配不拿别的视频顶替():
    """事件里的视频来自引用原文、``get_msg`` 只回本条的另一个视频：名字对不上就
    说对不上。改前按下标取，会把**另一条视频**当成这条的存下来。"""
    import wg_up_wake
    _renew_fixture(EID_RENEW)
    real_call = wg_up_wake._onebot_call
    real_dl = video.wg_download.download
    wg_up_wake._onebot_call = lambda action, payload: {
        "message": [{"type": "video", "data": {"file": "dog.mp4",
                                              "url": "https://media.example.test/dog.mp4"}}]}
    video.wg_download.download = _stale_download("https://media.example.test/dog.mp4")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = video.fetch(EID_RENEW, 0, 40)
    finally:
        wg_up_wake._onebot_call = real_call
        video.wg_download.download = real_dl
    text = buf.getvalue()
    return code == 1 and "对不上" in text and "dog.mp4" not in text


@case
def 重签按名字命中同一条():
    """名字对得上时重签要真的用上——这是「几小时前发的视频」唯一救得回来的路。"""
    import wg_up_wake
    _renew_fixture(EID_RENEW2)
    real_call = wg_up_wake._onebot_call
    real_dl = video.wg_download.download
    wg_up_wake._onebot_call = lambda action, payload: {
        "message": [{"type": "video", "data": {"file": "cat.mp4",
                                              "url": "https://media.example.test/fresh.mp4"}}]}
    video.wg_download.download = _stale_download("https://media.example.test/fresh.mp4")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = video.fetch(EID_RENEW2, 0, 40)
    finally:
        wg_up_wake._onebot_call = real_call
        video.wg_download.download = real_dl
    text = buf.getvalue()
    return code == 0 and "重签地址" in text


@case
def 栈没起时不说成链接过期():
    """重签根本没试成（OneBot 不答）与「重签了也过期」是两件事。"""
    import wg_up_wake
    _renew_fixture(EID_RENEW3)
    real_call = wg_up_wake._onebot_call
    real_dl = video.wg_download.download

    def boom(action, payload):
        raise OSError("connection refused")

    wg_up_wake._onebot_call = boom
    video.wg_download.download = _stale_download("https://media.example.test/never.mp4")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = video.fetch(EID_RENEW3, 0, 40)
    finally:
        wg_up_wake._onebot_call = real_call
        video.wg_download.download = real_dl
    text = buf.getvalue()
    return code == 1 and "OneBot 不答" in text


# ---- 缓存与落盘 ---------------------------------------------------------------


@case
def 抽帧目录也进缓存预算():
    """抽帧产物是目录，改前 ``glob("*")`` + ``is_file()`` 把它滤掉 → 永不淘汰。"""
    saved = (video.VIDEOS, video.CACHE_BYTES)
    room = TMP / "prune_room"
    room.mkdir(parents=True, exist_ok=True)
    frames = room / "9301-0-frames"
    frames.mkdir(exist_ok=True)
    (frames / "9301-0-0.png").write_bytes(b"x" * 4096)
    video.VIDEOS = room
    video.CACHE_BYTES = 1024
    try:
        video.prune()
        gone = not frames.exists()
        # keep_path 必须保住刚抽的那一组（否则抽完立刻被自己删掉）
        frames.mkdir(exist_ok=True)
        (frames / "9301-0-0.png").write_bytes(b"x" * 4096)
        video.prune(keep_path=frames)
        kept = frames.exists()
    finally:
        video.VIDEOS, video.CACHE_BYTES = saved
    return gone and kept


@case
def 非视频字节落盘要说明():
    """下回来的不是视频时要说出来，不许假装它是视频。"""
    append_event(event_of(EID_BIN, [video_segment()]))
    real = video.wg_download.download
    video.wg_download.download = lambda url, **kwargs: b"PK\x03\x04 this is a zip"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = video.fetch(EID_BIN, 0, 40)
    finally:
        video.wg_download.download = real
    text = buf.getvalue()
    return (code == 0 and "不像视频" in text and "--frames" not in text)


# ---- 抽帧（P3）：用假 ffmpeg/ffprobe，CI 上没装也能验 ----------------------


class FakeRun:
    """替换 ``winconsole.run_hidden``：按命令里有没有 ``-show_entries`` 区分
    ffprobe（报关时长）与 ffmpeg（写出一张假 PNG）。"""

    def __init__(self, duration="6.0", ffmpeg_ok=True, write=True):
        self.duration = duration
        self.ffmpeg_ok = ffmpeg_ok
        self.write = write
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if "-show_entries" in command:
            out = "" if self.duration is None else self.duration
            return type("R", (), {"returncode": 0 if self.duration else 1,
                                  "stdout": out, "stderr": ""})()
        if self.write:
            target = Path(command[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        if self.ffmpeg_ok:
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return type("R", (), {"returncode": 1, "stdout": "",
                              "stderr": "Invalid data found when processing input"})


@contextlib.contextmanager
def fake_tools(run, have=True):
    """临时替换 ``wg_frames.fftools`` 与 ``winconsole.run_hidden``。"""
    real_tools = wg_frames.fftools
    real_run = wg_frames.winconsole.run_hidden
    wg_frames.fftools = lambda: (("ffmpeg", "ffprobe") if have else (None, None))
    wg_frames.winconsole.run_hidden = run
    try:
        yield run
    finally:
        wg_frames.fftools = real_tools
        wg_frames.winconsole.run_hidden = real_run


@case
def ffmpeg_缺失时明说缺什么():
    """缺 ffmpeg 不许静默变成「没有内容」——那会让 Agent 以为视频是空的。"""
    with fake_tools(FakeRun(), have=False):
        result = wg_frames.extract(TMP / "whatever.mp4", TMP / "f0")
    text = wg_frames.render(result)
    return (not result["ok"]) and "ffmpeg" in result["problem"] and "PATH" in text


@case
def 抽帧三帧并带具体路径():
    src = TMP / "t.mp4"
    src.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    with fake_tools(FakeRun(duration="6.0")) as run:
        result = wg_frames.extract(src, TMP / "f1", stem="x")
    if not result["ok"] or len(result["frames"]) != 3:
        return False
    # 首/中/尾，且都真的落在候选时刻上
    if [f["at"] for f in result["frames"]] != [0.0, 3.0, 5.4]:
        print(f"    实际时刻：{[f['at'] for f in result['frames']]}")
        return False
    rendered = wg_frames.render(result, event_id=EID_OK, index=0)
    return all(f["path"] in rendered for f in result["frames"])


@case
def 时长未知只抽首帧不猜():
    """问不到时长就只抽首帧。猜一个时长去抽中间，等于编内容。"""
    src = TMP / "t2.mp4"
    src.write_bytes(b"x")
    with fake_tools(FakeRun(duration=None)):
        result = wg_frames.extract(src, TMP / "f2", stem="y")
    return result["ok"] and [f["at"] for f in result["frames"]] == [0.0]


@case
def 短时长不抽三张一样的图():
    """0.6 秒的视频：0/0.3/0.54 三帧几乎一样，去重后不该超过 2 帧。"""
    src = TMP / "t3.mp4"
    src.write_bytes(b"x")
    with fake_tools(FakeRun(duration="0.6")):
        result = wg_frames.extract(src, TMP / "f3", stem="z")
    times = [f["at"] for f in result["frames"]]
    return result["ok"] and len(times) < 3 and len(set(times)) == len(times)


@case
def 抽帧失败要说真实原因():
    """解码失败时把 ffmpeg 的那句话带出来，不许只说「失败」。"""
    src = TMP / "t4.mp4"
    src.write_bytes(b"not really a video")
    with fake_tools(FakeRun(ffmpeg_ok=False, write=False)):
        result = wg_frames.extract(src, TMP / "f4", stem="w")
    text = wg_frames.render(result)
    return (not result["ok"]) and "Invalid data" in result["problem"]


@case
def 子进程不开黑框():
    """抽帧走 winconsole.run_hidden（issue #39），不许自己手写 creationflags。"""
    source = (ROOT / "water-group" / "daemon" / "wg_frames.py").read_text(encoding="utf-8")
    return ("winconsole.run_hidden" in source
            and "CREATE_NO_WINDOW" not in source
            and "creationflags" not in source)


def main():
    return fs.run_cases(CASES, setup=build, prefix=False)


if __name__ == "__main__":
    sys.exit(main())
