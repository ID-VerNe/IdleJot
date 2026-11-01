"""issue #144 gate: 唤醒账本 / 追问攒批 / 监听去抖。

issue #144 的核心症状是「turn 的数量随群活跃度线性膨胀」，动手前先撞上一个更根本的
问题：**被叫醒了多少次、从哪条路叫的，无处可查**。本门禁钉三件事（对应 issue 的
P1 / P2 / P3），每条都带对照组——只断言「有输出」会被「怎么改都输出」的坏实现骗过
（#153 教训）。

## 1. 唤醒账本（P1，`water-group/daemon/wg_wake_audit.py`）

  a. `@` 唤醒落一行 `source=at`，`keyword` 落 `source=keyword`（明细
     `keyword:<词>` 不进账本，否则账本能反推群内用语）；
  b. 追问唤醒落 `source=followup`——它来自 `chat_inbox`，从不进 `wg_events.jsonl`，
     改前连「统计不到」都算不上；
  c. 禁言解禁（`UNMUTED`）落 `source=unmute`，且 `batch=1`（它不带走任何消息）；
  d. 一次唤醒只记一行：三条消息并成一批时是一行 `batch=3`，不是三行；
  e. 群号不落盘，只留 12 位哈希；同一群两次哈希相同、另一群不同；
  f. 坏行（半行 / 非对象 / `at` 不是数）跳过而不抛，好行照读；
  g. 写失败（目录被占成文件）静默返回 `None`，`rows()` 读空——**账本是观测件，
     不该因为磁盘满就让一次真实唤醒发不出去**；
  h. `--status` 的「唤醒」行三种说法分开：没开始记 / 过去 24h 无唤醒 / 按来源计数。

## 2. 追问攒批（P2，`followup.QUIET_SECONDS`）

  i. 群里连发、最后一条还没安静够 `quiet_seconds` → `wake_scan` 不唤醒**且不推进
     `consume()` 游标**（顺序不能反：先 consume 就会把还没等到的这一批提前吃掉，
     之后 quiet 判据永远看得到空行、窗口再也不醒）；
  j. 安静够之后同一批一次唤醒，`batch` 就是这一批的条数；
  k. `quiet_seconds=0` 回到改前行为：一条就唤醒（对照组：证明 i 不是「反正不醒」）；
  l. 攒批参数在开窗时定死（与 `budget` / `idle_seconds` 同一口径），之后改配置不影响
     已开的窗。

## 3. 监听去抖（P3，`watch.settle`）

  m. 静默窗口内攒：窗口里新到的一条被并进同一批，只算一次唤醒；
  n. **不突破等待预算（#83）**：窗口从 deadline 里扣而不是额外加，预算不够就缩窗口；
     连一秒都等不起时原地返回、一秒不睡；
  o. 不超过 `DEBOUNCE_CAP` 倍：群刷屏时每来一条就推后会让监听永远醒不过来；
  p. 重读失败不吞唤醒：`refetch()` 抛异常时照旧返回手上这一批（已经确定要叫醒的人，
     不能因为一次读盘失败变成「没叫」）；
  q. `--debounce 0` 关闭：`watch.py --debounce 0` 的 WAKE 不晚于改前。

**已知边界**：不连真实群、不调 OneBot、不测 `reply.py`（#144 明确「发送链路一行不改」）；
去抖的真实收益只有 1% 量级（issue 追评实测 0.5–0.8%），本门禁钉的是「不做坏事」而不是
「省了多少」——省了多少由账本自己回答。

跑法：

    .venv/Scripts/python.exe scripts/check_wake_audit.py
"""
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import pin_conversation, pin_state_env, scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False, root=False)

DAEMON = ROOT / "water-group" / "daemon"

# 数据根 / 状态目录 / 配置 / 库全部钉进临时目录，且在 import 任何 daemon 模块
# **之前**完成：wg_paths.state_dir() / wg_config.CONFIG 都是 import 期求值。
SCRATCH = scratch_dir(prefix="wg_wake_audit_")
STATE = SCRATCH / "state"
STATE.mkdir(parents=True, exist_ok=True)
CONFIG = SCRATCH / "config.json"
DB = SCRATCH / "chat.sqlite3"
os.environ["WATER_GROUP_HOME"] = str(SCRATCH)
pin_state_env(STATE)
os.environ["IDLEJOT_WG_CONFIG"] = str(CONFIG)
os.environ["IDLEJOT_WG_DB"] = str(DB)

ACCOUNT = "10001"
SELF = ACCOUNT
GROUP_A = "111"
GROUP_B = "222"
CONV_A = f"{ACCOUNT}:group:{GROUP_A}"
CONV_B = f"{ACCOUNT}:group:{GROUP_B}"
WAKES = STATE / "wg_wakes.jsonl"
WINDOW = STATE / "wg_followup.json"
# 会话身份必须钉出来（与 check_trigger_mode 同一理由，issue #316）。
pin_conversation(CONV_A, STATE)
os.environ.pop("IDLEJOT_WG_CONVERSATION", None)
NOW_MS = int(time.time() * 1000)
# 每个用例一个 id 段，互不干扰。
AT_ID = 9301
KW_ID = 9302
MUTE_ID = 9303
BATCH_IDS = (9311, 9312, 9313)
INBOX_IDS = (9401, 9402, 9403)

WATCH = DAEMON / "watch.py"
ID_RE = re.compile(r"^id=(\d+)\b")


def write_config(groups=None, **extra):
    payload = {"self_account": ACCOUNT, "onebot_token": "wake-audit-fixture",
               "self_names": ["小鲸鱼"], "default_trigger_mode": "mention_only",
               "groups": groups if groups is not None else
               {GROUP_A: {"enabled": True, "trigger_mode": "mention_only"}}}
    payload.update(extra)
    CONFIG.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def clear_state():
    for name in ("wg_events.jsonl", "wg_replied.jsonl", "wg_watch.txt",
                 "wg_session.txt", "wg_state.json"):
        (STATE / name).unlink(missing_ok=True)


def clear_ledger():
    WAKES.unlink(missing_ok=True)


def event(event_id, content, *, at=False, conversation=CONV_A, is_self=False,
          conversation_field=True):
    segs = [{"type": "at", "qq": SELF}] if at else []
    row = {"id": event_id, "sender": "群友", "content": content,
           "timestamp": NOW_MS, "is_self": is_self, "segments": segs,
           "onebot_message_id": f"m{event_id}"}
    if conversation_field:
        row["conversation_id"] = conversation
    return row


def seed(rows):
    (STATE / "wg_events.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def child_env(conversation=CONV_A):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    pin_state_env(STATE, home=SCRATCH, env=env)  # WATER_GROUP_HOME + state 两腿（#281）
    env["IDLEJOT_WG_CONFIG"] = str(CONFIG)
    env["IDLEJOT_WG_DB"] = str(DB)
    for name in ("IDLEJOT_WG_CONVERSATION", "TULPA_WG_CONVERSATION"):
        env.pop(name, None)
    if conversation:
        env["IDLEJOT_WG_CONVERSATION"] = conversation
    return env


def run(script, *args, conversation=CONV_A, timeout=120):
    return subprocess.run([sys.executable, str(script), *args], cwd=str(ROOT),
                          env=child_env(conversation), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def inprocess(main, *argv):
    """在本进程里跑一个入口的 `main()`，返回 (退出码, 输出)。

    进程内跑是为了给 `--debounce` 计时（子进程启动就要两秒，会把「窗口有没有真的
    生效」淹掉）；数据根与 state 已在 import 期钉死，与子进程同一份。
    """
    saved = sys.argv
    buf = io.StringIO()
    sys.argv = list(argv)
    try:
        with contextlib.redirect_stdout(buf):
            code = main()
    finally:
        sys.argv = saved
    return code, buf.getvalue()


def ledger():
    """账本按行读成 dict；文件不存在返回 []。"""
    if not WAKES.exists():
        return []
    out = []
    for line in WAKES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def listed_ids(out):
    ids = set()
    for line in out.splitlines():
        match = ID_RE.match(line.strip())
        if match:
            ids.add(int(match.group(1)))
    return ids


def wake_reason_of(row):
    """一条事件该记成什么来源——与 `watch.py` 的记账口径同一个函数。"""
    import up

    return up.wake_reason(row)


# ---------- 追问窗口夹具（chat_inbox） ----------


def build_db():
    """建最小 chat_inbox / chat_sessions，只够 `followup.since()` 读。"""
    if DB.exists():
        DB.unlink()
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    db.execute("CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id TEXT,"
               " payload TEXT, at REAL NOT NULL, media TEXT NOT NULL DEFAULT '[]')")
    db.execute("INSERT INTO chat_sessions VALUES ('s-mine', ?, 1)", (CONV_A,))
    db.commit()
    db.close()


def append_inbox(rows):
    """往 chat_inbox 追加：(id, 正文)。

    `ts` 取「现在」，`at` 也取现在：`consume()` 拿消息自己的时间判 pre-reply，
    给一个老时间会让它整批算成 pre-reply、不占额度（issue #117/#218）。
    """
    now_ms = int(time.time() * 1000)
    db = sqlite3.connect(DB)
    for event_id, text in rows:
        payload = {"sender": "群友", "content": text, "timestamp": now_ms,
                   "conversation_id": CONV_A, "onebot_message_id": f"m{event_id}"}
        db.execute("INSERT INTO chat_inbox(id,session_id,payload,at,media)"
                   " VALUES (?, 's-mine', ?, ?, '[]')",
                   (event_id, json.dumps(payload, ensure_ascii=False), time.time()))
    db.commit()
    db.close()


def open_window(**kwargs):
    import followup

    return followup.open(INBOX_IDS[0] - 1, max_messages=kwargs.pop("max_messages", 10),
                         session="s-mine", **kwargs)


def wake_scan(settings=None):
    import plugins

    payload = {"settings": settings if settings is not None else
               {"plugins": {"followup": {"enabled": True, "options": {}}}}}
    return plugins.emit("wake_scan", payload)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---------- 1a：@ 与关键词来源分级 ----------


@case
def at唤醒落账为at():
    """一条 @ 我的消息走 watch 唤醒，账本落一行 `source=at`、`ids` 带上它。"""
    clear_state()
    clear_ledger()
    write_config()
    seed([event(AT_ID, "@机器人 在吗", at=True)])
    proc = run(WATCH, "--timeout", "5", "--debounce", "0", "--replay")
    rows = ledger()
    ok = (len(rows) == 1 and rows[0]["source"] == "at"
          and rows[0]["ids"] == [AT_ID] and rows[0]["batch"] == 1
          and "WAKE" in proc.stdout)
    return ok or f"ledger={rows}\n{proc.stdout}\n{proc.stderr}"


@case
def keyword唤醒落账为keyword且不带明细():
    """`wake_reason()` 返回 `keyword:小鲸鱼`，账本只记 `keyword`。

    明细是群内用语，随黑话漂移，统计用不上——落盘就等于把群内词表写进一份
    要能在 issue 里公开讨论的文件。**先校验 `wake_reason()` 的原始返回值再看账本**：
    改前它就已经是 `keyword:…`，只断账本会把「压根没落账」漏成 pass（基线实测
    过这一点，issue #153 的假绿教训）。
    """
    clear_state()
    clear_ledger()
    write_config()
    seed([event(KW_ID, "小鲸鱼 你说说")])
    reason = wake_reason_of(event(KW_ID, "小鲸鱼 你说说"))
    if not reason.startswith("keyword:"):
        return f"前置失败：wake_reason={reason!r}（期望 keyword:...）"
    proc = run(WATCH, "--timeout", "5", "--debounce", "0", "--replay")
    rows = ledger()
    ok = (len(rows) == 1 and rows[0]["source"] == "keyword"
          and "小鲸鱼" not in WAKES.read_text(encoding="utf-8")
          and "WAKE" in proc.stdout)
    return ok or f"reason={reason!r} ledger={rows}\n{proc.stdout}"


# ---------- 1b / 1d：追问来源与「一批只记一行」 ----------


@case
def 追问唤醒落账为followup且一批一行():
    """连着三条追问一次唤醒：账本一行 `source=followup`、`batch=3`。

    三种实现会被这条抓住：不记追问（改前状态，一眼就看得出统计缺了最大的一块）、
    每条各记一行（`batch` 全是 1，去抖收益被高估）、把 `batch` 写死成 1。
    """
    clear_state()
    clear_ledger()
    write_config()
    build_db()
    append_inbox([(INBOX_IDS[0], "接一句"), (INBOX_IDS[1], "再接一句"),
                  (INBOX_IDS[2], "还有")])
    open_window(quiet_seconds=0)
    out = (STATE / "wg_replied.jsonl")
    if out.exists():
        out.unlink()
    proc = run(WATCH, "--timeout", "5", "--debounce", "0", "--replay")
    rows = ledger()
    ok = (len(rows) == 1 and rows[0]["source"] == "followup"
          and rows[0]["batch"] == 3
          and sorted(rows[0]["ids"]) == sorted(INBOX_IDS)
          and "[追问]" in proc.stdout)
    return ok or f"ledger={rows}\n{proc.stdout}\n{proc.stderr}"


# ---------- 1c：解禁唤醒 ----------


@case
def 解禁唤醒落账为unmute且batch为1():
    """禁言账本指向一个已过的解禁时刻时，`poll_unmute()` 唤醒并落一行 `unmute`。

    它不带走任何消息，所以 `batch` 必须是 1 而不是 0——`batch` 是「这一批叫醒了
    几条」的度量，0 会让统计把这次叫醒算成「没叫醒」。
    """
    import wg_send_failure

    clear_state()
    clear_ledger()
    write_config()
    ledger_file = STATE / "wg_send_failure.json"
    ledger_file.write_text(json.dumps({
        "muted": True, "kind": "shut_up", "group_id": GROUP_A,
        "shut_up_timestamp": int(time.time()) - 5, "reported_at": time.time() - 3600,
    }, ensure_ascii=False), encoding="utf-8")
    try:
        yielded = wg_send_failure.probe(min_interval=0)
        row = wg_wake_audit_record("unmute")
    finally:
        ledger_file.unlink(missing_ok=True)
    rows = ledger()
    ok = (len(rows) == 1 and rows[0]["source"] == "unmute" and rows[0]["batch"] == 1
          and rows[0]["ids"] == [])
    return ok or f"probe={yielded} record={row} ledger={rows}"


def wg_wake_audit_record(source):
    import wg_wake_audit

    return wg_wake_audit.record(source, batch=1)


# ---------- 1e：群号只留哈希 ----------


@case
def 群号不落盘只留哈希():
    """账本里没有真实群号，`group_hash` 同一群相同、不同群不同、空群号为空串。"""
    import wg_wake_audit

    clear_ledger()
    wg_wake_audit.record("at", ids=[1], group_id=GROUP_A)
    wg_wake_audit.record("at", ids=[2], group_id=GROUP_A)
    wg_wake_audit.record("at", ids=[3], group_id=GROUP_B)
    wg_wake_audit.record("at", ids=[4])
    text = WAKES.read_text(encoding="utf-8")
    rows = ledger()
    hashes = [r["group_hash"] for r in rows]
    ok = (GROUP_A not in text and GROUP_B not in text and len(hashes) == 4
          and hashes[0] == hashes[1] and hashes[0] != hashes[2]
          and hashes[3] == "" and len(hashes[0]) == 12
          and wg_wake_audit.describe().startswith("过去 24h 4 次"))
    return ok or f"hashes={hashes}\ntext={text}"


# ---------- 1f / 1g：坏行与写失败 ----------


@case
def 坏行跳过好行照读():
    """半行、非对象、`at` 不是数的行一律跳过，不抛；好行照读且按时间排序。"""
    import wg_wake_audit

    clear_ledger()
    good_late = json.dumps({"at": time.time(), "source": "at", "batch": 1,
                            "ids": [2], "group_hash": "", "pid": 1})
    good_early = json.dumps({"at": time.time() - 60, "source": "keyword:详",
                             "batch": 2, "ids": [1, 2], "group_hash": "", "pid": 1})
    WAKES.write_text("\n".join(["{坏行", "[1,2]", '{"at": "昨天"}', good_late,
                                '{"source": "at"}', good_early]) + "\n",
                     encoding="utf-8")
    rows = wg_wake_audit.rows()
    ok = (len(rows) == 2
          and [r["ids"] for r in rows] == [[1, 2], [2]]
          and rows[0]["source"] == "keyword"      # 明细被归一掉
          and rows[1]["source"] == "at")
    return ok or f"rows={rows}"


@case
def 写失败静默返回None():
    """状态目录被占成文件 → `record()` 返回 None、`rows()` 读空，不抛异常。"""
    import wg_wake_audit

    saved = wg_wake_audit.STATE
    blocked = SCRATCH / "blocked_state"
    blocked.write_text("not a directory", encoding="utf-8")
    wg_wake_audit.STATE = blocked
    try:
        got = wg_wake_audit.record("at", ids=[1], group_id=GROUP_A)
        read = wg_wake_audit.rows()
        exists = wg_wake_audit.exists()
    finally:
        wg_wake_audit.STATE = saved
    ok = got is None and read == [] and exists is False
    return ok or f"record={got} rows={read} exists={exists}"


# ---------- 1h：--status 的三种说法 ----------


@case
def 状态行三种说法分开():
    """没开始记 / 窗口内无记录 / 按来源计数——三者不能长成一个样子。"""
    import wg_wake_audit

    clear_ledger()
    fresh = wg_wake_audit.describe()
    if not fresh.startswith("账本还没开始记"):
        return f"空账本说成了 {fresh!r}"
    WAKES.write_text(json.dumps({"at": time.time() - 3 * 24 * 3600, "source": "at",
                                 "batch": 1, "ids": [1], "group_hash": "", "pid": 1}) + "\n",
                     encoding="utf-8")
    empty = wg_wake_audit.describe()
    if empty != "过去 24h 无唤醒":
        return f"窗口内无记录说成了 {empty!r}（旧记录不该算进 24h）"
    wg_wake_audit.record("at", ids=[1], group_id=GROUP_A)
    wg_wake_audit.record("followup", ids=[2, 3], group_id=GROUP_A)
    both = wg_wake_audit.describe()
    ok = both == "过去 24h 2 次（at 1 / followup 1）"
    return ok or f"both={both!r}"


@case
def 状态入口打出唤醒行():
    """`up.py --status` 里有一行以「唤醒」开头，且文案来自同一个 describe()。"""
    import up

    clear_ledger()
    write_config()
    code, out = inprocess(up.main, "up.py", "--status")
    if code != 0:
        return f"up.py --status 退出码 {code}\\n{out[-1500:]}"
    lines = [ln for ln in out.splitlines() if ln.startswith("唤醒")]
    ok = (len(lines) == 1 and "账本还没开始记" in lines[0]
          and "过去 24h 无唤醒" not in out)
    return ok or f"lines={lines}\n{out[-1500:]}"


# ---------- 2i–2l：追问攒批 ----------


@case
def 攒批未静默不唤醒也不推进游标():
    """最后一条还没安静够 `quiet_seconds` → 不唤醒，且窗口游标原封不动。"""
    import followup

    clear_state()
    clear_ledger()
    write_config()
    build_db()
    append_inbox([(INBOX_IDS[0], "刚说完")])
    open_window(quiet_seconds=60)
    before = followup.load()
    got = wake_scan()
    after = followup.load()
    ok = (got is None
          and before is not None and after is not None
          and after.get("after_id") == before.get("after_id")
          and after.get("budget") == before.get("budget")
          and after.get("wakes") == 0)
    return ok or f"scan={got}\nbefore={before}\nafter={after}"


@case
def 安静够之后一批一次唤醒():
    """`quiet_seconds` 已过 → 三条并成一次唤醒，窗口照常推进并关窗（额度用完）。"""
    import followup

    clear_state()
    clear_ledger()
    write_config()
    build_db()
    append_inbox([(i, f"第 {i} 句") for i in INBOX_IDS])
    open_window(quiet_seconds=0.2)
    time.sleep(0.35)
    got = wake_scan()
    woke = [r["id"] for r in (got or {}).get("woke") or []]
    after = followup.load()
    # 额度用完的那一批要当场收窗（issue #98），攒批不许把这个行为带坏。
    ok = sorted(woke) == sorted(INBOX_IDS) and after is None
    return ok or f"scan={got}\nwoke={woke}\nafter={after}"


@case
def 攒批为0退回改前行为():
    """对照组：`quiet_seconds=0` 时同一批立刻唤醒——证明上一条不是「反正不醒」。"""
    clear_state()
    clear_ledger()
    write_config()
    build_db()
    append_inbox([(INBOX_IDS[0], "立刻说")])
    open_window(quiet_seconds=0)
    got = wake_scan()
    woke = [r["id"] for r in (got or {}).get("woke") or []]
    ok = woke == [INBOX_IDS[0]]
    return ok or f"scan={got} woke={woke}"


@case
def 攒批参数开窗时定死():
    """开窗时给的 `quiet_seconds` 写进窗口状态，之后改配置不影响这个窗。

    与 `budget` / `idle_seconds` 同一口径：同一个窗口前后行为不能因为配置热改而
    变一半，否则「等多久」取决于中途有没有人改过配置。
    """
    import followup

    build_db()
    open_window(quiet_seconds=7.5)
    state = followup.load()
    write_config(followup={"quiet_seconds": 0})
    try:
        got = followup.open(INBOX_IDS[0] - 1, max_messages=1, session="s-mine")
        direct = got.get("quiet_seconds") if isinstance(got, dict) else None
    finally:
        write_config()
    ok = state.get("quiet_seconds") == 7.5 and direct == 0
    return ok or f"state={state} direct={direct}"


# ---------- 3m–3q：去抖 ----------


@case
def 去抖把窗口内新到的并成一批():
    """窗口里新到的一条被并进同一批：只算一次唤醒，两条都在 `ids` 里。"""
    clear_state()
    clear_ledger()
    write_config()
    seed([event(BATCH_IDS[0], "@机器人 先说", at=True)])
    late = event(BATCH_IDS[1], "@机器人 又说", at=True)

    def refetch():
        # 窗口期间群里又来了一条：落进事件流，下一次重读就能看见。
        seed([event(BATCH_IDS[0], "@机器人 先说", at=True), late])
        return [late]

    import watch

    deadline = time.time() + 30
    first = [event(BATCH_IDS[0], "@机器人 先说", at=True)]
    spent, fresh = watch.settle(first, first, 1.0, deadline, refetch,
                                lambda more: more)
    ids = [e["id"] for e in fresh]
    ok = (spent >= 0.9 and ids == [BATCH_IDS[0], BATCH_IDS[1]])
    return ok or f"spent={spent:.2f} fresh={ids}"


@case
def 去抖不突破等待预算():
    """deadline 只剩 0.3s 时窗口缩到 0.3s，绝不睡满请求的 5s（#83）。"""
    import watch

    deadline = time.time() + 0.3
    started = time.time()
    spent, fresh = watch.settle([{"id": 1}], [], 5.0, deadline, lambda: [], lambda m: m)
    elapsed = time.time() - started
    ok = elapsed < 1.0 and spent <= 0.45 and fresh == [{"id": 1}]
    return ok or f"elapsed={elapsed:.2f} spent={spent:.2f}"


@case
def 预算耗尽时原地返回一秒不睡():
    """deadline 已过 → 返回 (0.0, fresh)，连一秒都不睡。"""
    import watch

    started = time.time()
    spent, fresh = watch.settle([{"id": 1}], [], 5.0, time.time() - 1, lambda: [],
                                lambda m: m)
    elapsed = time.time() - started
    ok = spent == 0.0 and elapsed < 0.1 and fresh == [{"id": 1}]
    return ok or f"spent={spent} elapsed={elapsed:.3f}"


@case
def 去抖不超过上限倍数():
    """一直有新唤醒进来时，截止最多推到 `DEBOUNCE_CAP` 倍。"""
    import watch

    counter = {"n": 0}

    def refetch():
        counter["n"] += 1
        return [{"id": 100 + counter["n"]}]

    deadline = time.time() + 60
    started = time.time()
    spent, fresh = watch.settle([{"id": 1}], [], 0.3, deadline, refetch, lambda m: m)
    elapsed = time.time() - started
    ceiling = watch.DEBOUNCE_CAP * 0.3
    ok = (counter["n"] > 1 and elapsed < ceiling + 0.5
          and len(fresh) == counter["n"] + 1)
    return ok or f"elapsed={elapsed:.2f} ceiling={ceiling:.2f} refetches={counter['n']}"


@case
def 重读失败不吞唤醒():
    """`refetch()` 抛异常 → 照旧返回手上这一批（已确定要叫醒的人不能被吞）。"""
    import watch

    def boom():
        raise OSError("读盘失败")

    deadline = time.time() + 30
    spent, fresh = watch.settle([{"id": 1}], [], 0.2, deadline, boom, lambda m: m)
    ok = fresh == [{"id": 1}]
    return ok or f"spent={spent:.2f} fresh={fresh}"


@case
def 去抖关闭时不额外等待():
    """`--debounce 0`：`settle()` 立刻返回，`watch.py` 的 WAKE 不晚于改前。"""
    import watch

    spent, fresh = watch.settle([{"id": 1}], [], 0, time.time() + 30, lambda: [],
                                lambda m: m)
    if spent != 0.0 or fresh != [{"id": 1}]:
        return f"settle spent={spent} fresh={fresh}"
    clear_state()
    clear_ledger()
    write_config()
    seed([event(AT_ID, "@机器人 在吗", at=True)])
    started = time.time()
    code, out = inprocess(watch.main, "watch.py", "--timeout", "5",
                          "--debounce", "0", "--replay")
    elapsed = time.time() - started
    ok = code == 0 and "WAKE" in out and elapsed < 2.5
    return ok or f"code={code} elapsed={elapsed:.2f}\n{out}"


def main():
    return fs.run_cases(CASES, setup=lambda: (write_config(), clear_ledger()))


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
