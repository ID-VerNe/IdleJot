"""issue #35 gate: 唤醒行的图片标记必须可执行，不许出现裸 [图片]。

症状是群里发图并 @ 我，唤醒行只给一个 `[图片]`：不落盘、不说明怎么补读，Agent
每次都要现场试错（读 wg_events 段 → 猜 images 目录 → 翻 chat_inbox.media →
试 read_chat_forward）才能想起有个 `look.py`。文档侧同样零说明。

本门禁钉住三件事：

  1. 「可补读」必须带上具体命令（`look.py --event <id> --index <i>`）——状态词不是
     可执行指引；
  2. 段里缺 image_index、但索引里有该事件图片行时（旧数据/部分投递），渲染必须经
     索引反查发现图片，而不是当没看见；
  3. 任何状态都要由标记说出来：路径、无权限、未知状态、无记录，各有各的印法，
     唯独不许「有图却什么都不印」。

    .venv/Scripts/python.exe scripts/check_image_visibility.py
"""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False)

TMP = scratch_dir(prefix="wg_image_")
DB = TMP / "fixture.sqlite3"
STATE = TMP / "state"
(STATE / "images").mkdir(parents=True)
SID = "session-fixture"
SELF = "10001"

os.environ["TULPA_WG_DB"] = str(DB)
os.environ["TULPA_WG_STATE"] = str(STATE)

import followup  # noqa: E402
import up  # noqa: E402
import watch  # noqa: E402
import wg_images  # noqa: E402
import wg_up_wake  # noqa: E402  (issue #37 PC: config 的真实调用点；up.config 只是门面引用)

# 事件 id 段：9001+ 与真实数据不撞车；每条用例一个 id，索引行按需写入。
EID_SEG = 9001        # 段有 image_index、无 pixels（实机 6026/5274 的形状）
EID_NO_SEG = 9002     # 段里什么都没有，只有索引行（裸 [图片] 的直接根因）
EID_LOCAL = 9003      # 已落盘
EID_DENIED = 9004     # 无权限
EID_WEIRD = 9005      # 未知状态（失败必须可见）
EID_NONE = 9006       # 占位在、记录全无
EID_PLAIN = 9007      # 没有图
EID_MEDIA = 9008      # 段缺失、media 兜底（reply.py --context 那条路）
EID_WAKE = 9009       # watch 唤醒行端到端
NOW_MS = int(time.time() * 1000)


def index_row(event_id, state, local_path="", index=0, quote_of=None):
    return dict(event_id=event_id, image_index=index, state=state, sha256="",
                local_path=local_path, files=[local_path] if local_path else [],
                dimensions=[], frame_indices=[], seen=bool(local_path),
                at=time.time(), sender="群友丁", ts=NOW_MS, quote_of=quote_of)


def build():
    wg_images.upsert([
        index_row(EID_SEG, "deferred"),
        index_row(EID_NO_SEG, "deferred"),
        index_row(EID_LOCAL, "pixels_delivered", local_path=str(STATE / "9003-0.jpg")),
        index_row(EID_DENIED, "permission_denied"),
        index_row(EID_WEIRD, "budget_exhausted"),
        index_row(EID_MEDIA, "deferred"),
        index_row(EID_WAKE, "deferred"),
    ])
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    db.execute("CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id TEXT, message_id TEXT,"
               " payload TEXT, media TEXT NOT NULL DEFAULT '[]', details TEXT NOT NULL DEFAULT '[]',"
               " delivered INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL DEFAULT 0)")
    db.execute("INSERT INTO chat_sessions VALUES (?,?,1)", (SID, f"{SELF}:group:123"))
    # followup.since 夹具：一条段内带图的追问窗消息。at 给个非零值，对齐实机
    # chat_inbox.at（实机由 mcp_chat 写入 time.time()）；since() 现在要 SELECT at。
    db.execute("INSERT INTO chat_inbox (id,session_id,message_id,payload,media,at) VALUES (?,?,?,?,?,?)",
               (EID_MEDIA, SID, "m-media", json.dumps({
                   "sender": "群友丁", "content": "[图片]", "timestamp": NOW_MS,
                   "is_self": False, "segments": []}, ensure_ascii=False), "[]", float(NOW_MS / 1000)))
    db.commit()
    db.close()


def event_of(event_id, *, segments, content="[图片]"):
    return {"id": event_id, "sender": "群友丁", "content": content,
            "timestamp": NOW_MS, "segments": segments}


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def 段内缺_pixels_也带补读命令():
    """实机 6026/5274 的形状：段有 image_index、无 pixels.state、索引里 deferred。"""
    event = event_of(EID_SEG, segments=[{"type": "image", "image_index": 0}])
    text = wg_images.render(event)
    return ("可补读" in text and f"look.py --event {EID_SEG} --index 0" in text
            and text != "[图片]")


@case
def 段缺_image_index_时索引反查发现():
    """段里什么都没有、索引里有 deferred 行——以前这里输出空串，只剩裸 [图片]。"""
    event = event_of(EID_NO_SEG, segments=[])
    labels = wg_images.labels(event)
    text = wg_images.render(event)
    return (len(labels) == 1 and labels[0]["state"] == "deferred"
            and f"look.py --event {EID_NO_SEG} --index 0" in text)


@case
def 已落盘印路径():
    event = event_of(EID_LOCAL, segments=[{"type": "image", "image_index": 0,
                                           "pixels": {"state": "pixels_delivered"}}])
    text = wg_images.render(event)
    return "path=" in text and "可补读" not in text


@case
def 无权限明说():
    event = event_of(EID_DENIED, segments=[{"type": "image", "image_index": 0,
                                            "pixels": {"state": "permission_denied"}}])
    return "无权限" in wg_images.render(event)


@case
def 未知状态失败可见():
    """没见过的 state 不许静默：印出来才能知道该修什么。"""
    event = event_of(EID_WEIRD, segments=[{"type": "image", "image_index": 0,
                                           "pixels": {"state": "budget_exhausted"}}])
    return "未交付:budget_exhausted" in wg_images.render(event)


@case
def 无记录但有占位_也给出命令():
    """段、media、索引全无，但 content 里有占位 = 确有图。不靠不印表达失败。"""
    event = event_of(EID_NONE, segments=[])
    text = wg_images.render(event)
    return "未登记" in text and f"look.py --event {EID_NONE}" in text


@case
def 没有图不长出标记():
    event = event_of(EID_PLAIN, segments=[{"type": "text", "text": "在吗"}], content="在吗")
    return wg_images.render(event) == ""


@case
def media_兜底也带命令():
    """reply.py --context 那条路：段缺失时用 chat_inbox.media 兜底。"""
    event = event_of(EID_MEDIA, segments=[])
    media = [{"kind": "image", "status": "available", "url": "https://example/x.jpg"}]
    text = wg_images.render(event, media)
    return f"look.py --event {EID_MEDIA} --index 0" in text


@case
def 追问行带图片提示():
    """追问窗里收到的图以前零信息（watch 追问分支不渲染图片）。

    补读命令打 ``q<n>``：追问行是 ``chat_inbox`` 的 rowid，与事件流 id 撞号时
    裸数字会指向另一次事件的图（issue #203）。
    """
    rows = followup.since(0, SID)
    hit = [r for r in rows if r.get("id") == EID_MEDIA]
    text = (hit[0].get("images") or "") if hit else ""
    return (bool(hit) and f"look.py --event q{EID_MEDIA} --index 0" in text
            and f"look.py --event {EID_MEDIA} " not in text)


@case
def 唤醒行端到端带图片提示():
    """watch.py 唤醒行：图片后缀必须跟在正文后面印出来。"""
    (STATE / "wg_events.jsonl").write_text(json.dumps({
        "id": EID_WAKE, "sender": "群友丁", "content": "[图片] 看这个",
        "timestamp": NOW_MS, "is_self": False,
        "segments": [{"type": "image", "image_index": 0}, {"type": "at", "qq": SELF}],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
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
            and f"look.py --event {EID_WAKE} --index 0" in text)


# ---------------------------------------------------------------- issue #228
# 索引 upsert 是读-改-写且无锁时，两个写者进程时间重叠会让后写者整份 replace 抹掉先写者
# 刚落盘的行。下面三组用真子进程钉死锁内「追加 / 压实」不丢行、硬杀后锁可接管。
# 子进程用一个独立的 state 隔离（复用本脚本已钉的 STATE 形状），避免污染上面的夹具索引。

CONCURRENCY_STATE = TMP / "concurrency_state"
CONCURRENCY_IMAGES = CONCURRENCY_STATE / "images"

# 子进程脚本：读满索引后睡 0.25s 再追加一行——复现 issue 复现命令里「读慢」的形状，
# 把读与写的时间窗口拉到足以被另一个写者撞上。event_id 由 argv 传入，与上面 9001+ 段
# 错开（用 2280x 段，issue 号命名）。
_UPSERT_CHILD = r'''
import os, sys, time, json
from pathlib import Path
import wg_images
eid = int(sys.argv[1])
real_read = Path.read_text
def slow_read(self, *a, **k):
    if self.name == "index.jsonl":
        text = real_read(self, *a, **k)
        time.sleep(0.25)
        return text
    return real_read(self, *a, **k)
Path.read_text = slow_read
wg_images.upsert([{"event_id": eid, "image_index": 0, "at": time.time()}])
'''


def _concurrency_env():
    """子进程用独立 state 跑 upsert，与本脚本的夹具索引隔离。"""
    CONCURRENCY_IMAGES.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["TULPA_WG_STATE"] = str(CONCURRENCY_STATE)
    env["IDLEJOT_WG_STATE"] = str(CONCURRENCY_STATE)
    env["PYTHONPATH"] = os.pathsep.join([str(STATE.parent), str(ROOT),
                                         str(ROOT / "water-group" / "daemon")])
    return env


def _child_upsert(eid, env):
    """起一个子进程带延迟 upsert 一行；返回 Popen（调用方自行 join）。"""
    return subprocess.Popen(
        [sys.executable, "-c", _UPSERT_CHILD, str(eid)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


@case
def 并发upsert不丢行():
    """两个写者进程带延迟各 upsert 一行：最终索引里两行都在（issue #228 复现形状）。"""
    # 干净起点：删掉隔离索引里残留的内容（上轮用例可能留过）。
    idx = CONCURRENCY_IMAGES / "index.jsonl"
    if idx.exists():
        idx.unlink()
    env = _concurrency_env()
    # 先种一行 (22801, 0) 进隔离 state，让两个并发写者都从「读到 1 行」开始。
    # 必须切到隔离 state 写——子进程读的是 CONCURRENCY_IMAGES，主进程默认 STATE/images。
    saved = (wg_images.IMAGES, wg_images.INDEX)
    wg_images.IMAGES = CONCURRENCY_IMAGES
    wg_images.INDEX = idx
    try:
        wg_images.upsert([{"event_id": 22801, "image_index": 0, "at": time.time()}])
    finally:
        wg_images.IMAGES, wg_images.INDEX = saved
    a = _child_upsert(22802, env)
    time.sleep(0.05)             # 让 A 先进入读-睡窗口，B 再撞上来
    b = _child_upsert(22803, env)
    a.communicate(timeout=20)
    b.communicate(timeout=20)
    # 子进程写的是隔离 state，这里把 state 指过去再读。
    wg_images.IMAGES = CONCURRENCY_IMAGES
    wg_images.INDEX = idx
    try:
        rows = {int(r["event_id"]) for r in wg_images.load_index().values()}
    finally:
        wg_images.IMAGES, wg_images.INDEX = saved
    return rows >= {22801, 22802, 22803}


@case
def 并发upsert两行都能按sha命中复用():
    """丢一行的下游症状：find_local_by_sha 认不出已有副本 → 重复落盘。
    锁修好后，两个并发写者各自落盘的图都该被 find_local_by_sha 命中。"""
    idx = CONCURRENCY_IMAGES / "index.jsonl"
    if idx.exists():
        idx.unlink()
    env = _concurrency_env()
    # 两个并发写者各写一张内容不同的图（不同 sha），都 index=True。
    sha_a = hashlib.sha256(b"image-A-bytes").hexdigest()
    sha_b = hashlib.sha256(b"image-B-bytes").hexdigest()
    child = _UPSERT_CHILD.replace(
        'wg_images.upsert([{"event_id": eid, "image_index": 0, "at": time.time()}])',
        'wg_images.upsert([{"event_id": eid, "image_index": 0, "sha256": '
        'sha, "local_path": str(wg_images.IMAGES / f"{eid}-0.jpg"), "at": time.time()}])')
    child = child.replace('eid = int(sys.argv[1])',
                          'eid = int(sys.argv[1]); sha = sys.argv[2]')
    a = subprocess.Popen([sys.executable, "-c", child, "22812", sha_a], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(0.05)
    b = subprocess.Popen([sys.executable, "-c", child, "22813", sha_b], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    a.communicate(timeout=20)
    b.communicate(timeout=20)
    # 在隔离 state 下写两个像素文件，让 find_local_by_sha 的 is_file() 判定成立。
    (CONCURRENCY_IMAGES / "22812-0.jpg").write_bytes(b"image-A-bytes")
    (CONCURRENCY_IMAGES / "22813-0.jpg").write_bytes(b"image-B-bytes")
    saved = (wg_images.IMAGES, wg_images.INDEX)
    wg_images.IMAGES = CONCURRENCY_IMAGES
    wg_images.INDEX = idx
    try:
        hit_a = bool(wg_images.find_local_by_sha(sha_a))
        hit_b = bool(wg_images.find_local_by_sha(sha_b))
    finally:
        wg_images.IMAGES, wg_images.INDEX = saved
    return hit_a and hit_b


@case
def 压实不丢新追加的行():
    """索引灌过 MAX_INDEX_ROWS 后，一个并发 upsert 与压实重叠：新行不能被压实的
    replace 整份覆盖吃掉。钉的是 issue #228 候选 1 的漏点——「压实 vs 并发追加」。"""
    idx = CONCURRENCY_IMAGES / "index.jsonl"
    if idx.exists():
        idx.unlink()
    env = _concurrency_env()
    saved = (wg_images.IMAGES, wg_images.INDEX)
    wg_images.IMAGES = CONCURRENCY_IMAGES
    wg_images.INDEX = idx
    try:
        # 灌到 MAX_INDEX_ROWS-1：再来一行就恰好越过上限触发压实。
        base = [{"event_id": 22820 + i, "image_index": 0, "at": float(i)}
                for i in range(wg_images.MAX_INDEX_ROWS - 1)]
        wg_images.upsert(base)
        before = len(idx.read_text(encoding="utf-8").splitlines())
    finally:
        wg_images.IMAGES, wg_images.INDEX = saved
    # 起一个子进程 upsert 一行（会触发压实），与此同时主进程也 upsert 一行（同样触发压实）。
    a = _child_upsert(22899, env)
    time.sleep(0.02)
    # 主进程切到隔离 state 直接写，与子进程并发。
    wg_images.IMAGES = CONCURRENCY_IMAGES
    wg_images.INDEX = idx
    try:
        wg_images.upsert([{"event_id": 22900, "image_index": 0, "at": 9.0}])
        a.communicate(timeout=20)
        lines = idx.read_text(encoding="utf-8").splitlines()
        # 压实后行数 <= MAX；但 22899 与 22900 两条新行都必须还在（它们是最新的，落在末尾 MAX 内）。
        eids = set()
        for line in lines:
            try:
                eids.add(int(json.loads(line)["event_id"]))
            except (ValueError, KeyError, TypeError):
                continue
        ok_len = len(lines) <= wg_images.MAX_INDEX_ROWS
    finally:
        wg_images.IMAGES, wg_images.INDEX = saved
    return ok_len and 22899 in eids and 22900 in eids


@case
def 硬杀持锁进程后新进程能接管():
    """持锁进程被 taskkill /F 硬杀时，Windows 关闭其句柄、字节锁自动释放——
    新进程 upsert 不卡死、照常落盘。钉的是 commands.py O_EXCL 那套的死锁文件
    不会在这里复发（issue #228 选 msvcrt.locking 而非 O_EXCL 的理由）。"""
    idx = CONCURRENCY_IMAGES / "index.jsonl"
    if idx.exists():
        idx.unlink()
    env = _concurrency_env()
    # 子进程拿到锁后睡很久不释放（模拟卡死），主进程随后 terminate() 它。
    holder = r'''
import os, sys, time
import wg_images
with wg_images._locked():
    time.sleep(999)
'''
    h = subprocess.Popen([sys.executable, "-c", holder], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(0.5)            # 让它拿到锁
    h.terminate()              # SIGKILL 等价：不给它跑 finally 释放
    h.wait(timeout=5)
    # 现在一个新子进程 upsert，必须在数秒内拿到锁、落盘、退出——不卡死。
    a = _child_upsert(22877, env)
    try:
        a.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        a.kill()
        return False
    saved = (wg_images.IMAGES, wg_images.INDEX)
    wg_images.IMAGES = CONCURRENCY_IMAGES
    wg_images.INDEX = idx
    try:
        rows = {int(r["event_id"]) for r in wg_images.load_index().values()}
    finally:
        wg_images.IMAGES, wg_images.INDEX = saved
    return 22877 in rows


def main():
    return fs.run_cases(CASES, setup=build, prefix=False)


if __name__ == "__main__":
    sys.exit(main())
