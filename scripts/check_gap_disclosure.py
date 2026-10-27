"""钉住停机窗口缺口披露：漏了什么事后说得出来，不再报告一切正常（issue #114）。

2026-10-26 那条 08:04 的 @ 就是这样丢的：后端 + NapCat 停机跨过一段时间再拉起，
窗口内的群消息不进任何本地存储（`chat_inbox` 由服务端在收到 OneBot 事件时写、
`wg_events.jsonl` 由 collector 在 `mention_only` 投递时写，停机时两者都不动）。
`up.py --status` 的「最近唤醒」只列拉起之后的、`reply.py --list` 为空、`listen.py`
报 `NO_WORK`——「没人叫我」与「有人叫过但机器不在」无法区分，直到人肉翻
`get_group_msg_history` 才发现窗口内 33 条、其中 1 条 @ 本机。

这条门禁钉住下面这些契约（编号是分组），任何一处退回旧行为都红：

1. `wg_gap.compute()` 的 diff 正确：夹具 `chat_inbox` 有 a/b/c，假
   `get_group_msg_history` 返回 a/b/c/d/e（d/e 在停机窗口内、e 是 @ 本机），
   返回 `missing=2, at_me=1`，`at_me_lines` 含 e 的 sender + 时间 + 正文片段。
2. `wg_gap.describe()` 在缺口非空时报出 missing / at_me 计数；无记录或缺口为空
   时返回空串（排除型对照：恒加注脚的实现红在后半条，与 #112 同口径）。
3. `up.py --status` 的「缺口」行在缺口非空时出现、为空时不加字（只读 `wg_gap.json`，
   不联网、不写——保住 `--status`「只看不动」契约）。
4. `listen.py` 的 NO_WORK 路径在缺口非空时插一行提示，且 `RESTART:` 仍是末行
   （#69 / #112 契约 1/9 不变）。WAKE 路径不插提示（Agent 已被叫醒）。
5. `reply.py --list` 在缺口非空时列出漏掉的 @ 本机消息（行首 `id=-`，区别于真
   唤醒行），为空时不加段。
6. `get_group_msg_history` 单次 200 上限语义（候选 4）：返回的最老一条仍新于
   本地最后一条时 `truncated=True`，`describe()` 含「只能核对最近 200 条」；
   最老一条老于本地最后一条时 `truncated=False` 且无该句。
7. NapCat 未响应（`fetch` 抛错）→ 该群 `unreachable=True`，`describe()` 说
   「无法核对」，不崩。
8. @ 本机判定用 `self_account`：非本机 @ 不计入 `at_me`。
9. 本地一条都没有（首次部署 / 换群）不报缺口——与「停机漏收」区分，否则新部署
   会把群里全部历史都算成「漏的」。
10. 窗口右边界取 `napcat_launch.json:started_epoch`，读不到时回落 `computed_at`。
11. 缺口非空时 `describe()` 必须带上**窗口起止**与**账本计算时刻**，且明写「仅供参考」
    （issue #148）：账本只在 collector 启动时算一次，不带时刻时三小时前的早晨停机
    账本与几分钟前的新缺口同形，Agent 会把旧账读成现刻事故。无缺口时仍返回空串
    （排除型对照：恒加注脚的实现红在后半条）。
12. 窗口**跨天**时两端补 `MM-DD`，同一天**不**补（issue #148）：只写 `07:21–09:30`
    的话，下午读起来像今天早上漏的，而它可能是前一夜。对照：恒带日期的实现红在
    同一天那条，恒不带的红在跨天那条。
13. `at_me_lines()` 的时间戳带日期（`YYYY-MM-DD HH:MM:SS`），与唤醒行的
    `HH:MM:SS` 不同形（issue #148）——漏掉的 @ 可能是昨夜停机时段的。
14. `reply.py --list` 的缺口段标题下面紧跟窗口上下文（与 `--status` 同一份措辞），
    且排在 `id=-` 行之前（issue #148）：只列漏掉的 @ 而不给窗口时，读者仍然分不清
    这是昨夜停机账本还是刚发生的事故。

全程临时 `WATER_GROUP_HOME` + `IDLEJOT_WG_DB`，不碰实机状态：

    .venv/Scripts/python.exe scripts/check_gap_disclosure.py
"""
import contextlib
import datetime
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_fixture_support  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import temp_dir  # noqa: E402

# issue #145：本门禁有两处 `reply.list_wakes()`，而它现在会顺手复查发送失败账本
# （`wg_send_failure.LEDGER` 是 import 期求值的 `state_dir() / ...`）。不钉
# `IDLEJOT_WG_STATE` 的话，外部环境带着它跑时这里会**写实机账本**
# （`rechecked_at` / `recheck_result`），还会吃掉 60 秒复查窗口，让真正的
# `reply.py --list` 跳过复查。`state_dir()` 的优先级是 IDLEJOT 先于 TULPA，
# 只靠 `check_fixture_support` 的 `WATER_GROUP_HOME` 钉不住它。
os.environ["IDLEJOT_WG_STATE"] = str(
    Path(tempfile.mkdtemp(prefix="wg_gap_state_")) / "state")

DAEMON = ROOT / "water-group" / "daemon"
sys.path.insert(0, str(DAEMON))

import up  # noqa: E402
import wg_clock  # noqa: E402
import wg_config  # noqa: E402  (config.json 的唯一读取口；CONFIG 在 import 期求值，需打桩)
import wg_gap  # noqa: E402
import wg_listener_state  # noqa: E402  (与 #112 账本同构，对照用)
import reply  # noqa: E402  (list_wakes 用例需要；模块级 import 触发不进 daemon loop)

ACCOUNT = "10001"
GROUP = "123"

# 漏收行的形态（契约 13）：`id=- msg_id=<id> YYYY-MM-DD HH:MM:SS [漏] <人>: <正文>`。
# 唤醒行是 `id=<正数> HH:MM:SS ...`——带日期是两者的分界线，不靠「有没有时分秒」判。
# 两个捕获组（issue #151 复核）：`(\\S+)` 是消息 id、`(...)` 是行首那个时间戳字段。
# 判据必须比对**捕获到的那个字段**，不能在整行里找子串——子串在行尾也能命中，
# 「头字段日期是错的、行尾补一句正确时刻」的实现在子串判据下是绿的。
STAMPED_GAP_LINE = re.compile(
    r"^id=- msg_id=(\S+) (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[漏\] ")


def fixture_home(home):
    """最小身份配置 + 隔离的 napcat_launch.json 与 wg_gap.json。"""
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"self_account": ACCOUNT, "onebot_token": "gap-fixture",
                    "groups": {GROUP: {"enabled": True}}}, ensure_ascii=False),
        encoding="utf-8")


def gap_at(home):
    """把 wg_gap.STATE / DB 钉到 home 下，返回还原函数（与 #112 ledger_at 同款）。

    `wg_gap` 在模块级求值 `DB`，`wg_config` 在 import 期求 `CONFIG`——这两处都得在用例
    里临时改写指向夹具目录，与 `wg_listener_state.STATE` 的打桩同款。

    `state_dir()` 读 `IDLEJOT_WG_STATE`（`wg_paths.py:35`），它**不**随 `WATER_GROUP_HOME`
    走——`check_fixture_support` 只钉了 `WATER_GROUP_HOME`。`wg_gap._launch_epoch()`
    从 `state_dir()` 找 `napcat_launch.json`，所以这里必须连 `IDLEJOT_WG_STATE` 也指到
    夹具的 `state/`，否则它读的是 import 期被 `_PINNED_HOME` 钉住的那份（夹具目录），
    永远找不到用例写的 launch 文件——`window_end` 就回落成 `now`，右边界断言假红。
    """
    original_state = wg_gap.STATE
    original_db = wg_gap.DB
    original_config = wg_config.CONFIG
    original_wgs = os.environ.get("IDLEJOT_WG_STATE")

    def restore():
        wg_gap.STATE = original_state
        wg_gap.DB = original_db
        wg_config.CONFIG = original_config
        if original_wgs is None:
            os.environ.pop("IDLEJOT_WG_STATE", None)
        else:
            os.environ["IDLEJOT_WG_STATE"] = original_wgs
    wg_gap.STATE = home / "state" / "wg_gap.json"
    wg_gap.DB = home / "data" / "mcp-access.sqlite3"
    wg_config.CONFIG = home / "config.json"
    os.environ["IDLEJOT_WG_STATE"] = str(home / "state")
    return restore


def ledger_at(home):
    """把 wg_listener_state 的账本路径钉到 home 下，返回还原函数（与 #112 同款）。

    #119 之后监听账本改成 `wg_listeners/<pid>.json` 一槽一份，模块级绑定是
    `STATE_DIR`（新）与 `LEGACY_STATE`（旧，只读兼容）；更早的 #112 布局只有
    `STATE`。三套名字都用 `hasattr` 探测后改写：跑新模块时 `STATE` 不存在，
    跑旧基线时 `STATE_DIR` 不存在——基线复跑（新门禁跑未打补丁的模块）两套
    路径都指到 `home`，否则旧模块会读写实机账本。与 `check_listener_rehang.py`
    的 `ledger_at` 同款（同一次 #119 改动）。
    """
    names = ("STATE", "STATE_DIR", "LEGACY_STATE")
    saved = {name: getattr(wg_listener_state, name) for name in names
             if hasattr(wg_listener_state, name)}
    owner_env = getattr(wg_listener_state, "OWNER_ENV", "WG_LISTENER_PID")
    owner = os.environ.get(owner_env)
    if "STATE" in saved:
        wg_listener_state.STATE = home / "state" / "wg_listener.json"
    if "STATE_DIR" in saved:
        wg_listener_state.STATE_DIR = home / "state" / "wg_listeners"
    if "LEGACY_STATE" in saved:
        wg_listener_state.LEGACY_STATE = home / "state" / "wg_listener.json"

    def restore():
        for name, value in saved.items():
            setattr(wg_listener_state, name, value)
        if owner is None:
            os.environ.pop(owner_env, None)
        else:
            os.environ[owner_env] = owner
    return restore


def write_launch(home, epoch):
    (home / "state" / "napcat_launch.json").write_text(
        json.dumps({"launcher_pid": 1, "started_epoch": int(epoch)}, ensure_ascii=False),
        encoding="utf-8")


def seed_inbox(home, rows):
    """rows: [(message_id, at_epoch, conversation_id)]。建表并插入。"""
    db = sqlite3.connect(str(home / "data" / "mcp-access.sqlite3"))
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS chat_inbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                message_id TEXT NOT NULL, payload TEXT NOT NULL, at REAL NOT NULL,
                UNIQUE(session_id,message_id));
            CREATE INDEX IF NOT EXISTS chat_inbox_session ON chat_inbox(session_id,id);
        """)
        for mid, at, cid in rows:
            payload = {"onebot_message_id": mid, "conversation_id": cid}
            db.execute(
                "INSERT OR IGNORE INTO chat_inbox(session_id,message_id,payload,at) "
                "VALUES (?,?,?,?)",
                ("sid", mid, json.dumps(payload, ensure_ascii=False), at))
        db.commit()
    finally:
        db.close()


def onebot_msg(message_id, epoch, sender, text, at_qq=None):
    """OneBot 11 原始消息（`get_group_msg_history` 返回的格式）。"""
    segs = []
    if at_qq:
        segs.append({"type": "at", "data": {"qq": str(at_qq)}})
    segs.append({"type": "text", "data": {"text": text}})
    return {"message_id": str(message_id), "time": int(epoch),
            "sender": {"nickname": sender, "user_id": 1},
            "message": segs}


def make_fetcher(msgs):
    """假 get_group_msg_history：返回固定的 msgs 列表（不联网）。"""
    def fetch(group_id, count=200):
        return list(msgs)
    return fetch


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---------- 契约 1：compute() 的 diff 正确 ----------


@case
def compute_diff正确():
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            now = int(time.time())
            write_launch(home, now)
            seed_inbox(home, [("a", now - 3600, f"1:group:{GROUP}"),
                              ("b", now - 3500, f"1:group:{GROUP}"),
                              ("c", now - 3400, f"1:group:{GROUP}")])
            msgs = [
                onebot_msg("b", now - 3500, "甲", "在"),
                onebot_msg("c", now - 3400, "甲", "好"),
                onebot_msg("d", now - 600, "乙", "在吗", at_qq=ACCOUNT),
                onebot_msg("e", now - 300, "丙", "你好"),
            ]
            entry = wg_gap.compute(group_ids=[GROUP], fetch=make_fetcher(msgs), now=now)
        finally:
            restore()
    g = entry["groups"][GROUP]
    return (g["missing"] == 2 and g["at_me"] == 1
            and len(g["at_me_lines"]) == 1
            and g["at_me_lines"][0]["sender"] == "乙"
            and "在吗" in g["at_me_lines"][0]["body"])


# ---------- 契约 2：describe() 有缺口/无缺口两侧 ----------


@case
def describe_缺口非空时报计数():
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            now = int(time.time())
            write_launch(home, now)
            seed_inbox(home, [("a", now - 3600, f"1:group:{GROUP}")])
            msgs = [onebot_msg("d", now - 600, "乙", "在吗", at_qq=ACCOUNT),
                    onebot_msg("e", now - 300, "丙", "路过")]
            wg_gap.record(wg_gap.compute(group_ids=[GROUP],
                                        fetch=make_fetcher(msgs), now=now))
            note = wg_gap.describe()
        finally:
            restore()
    return ("缺口" in note and "漏 2 条" in note and "@ 本机 1 条" in note)


@case
def describe_无缺口时返回空串():
    """无缺口时不加字——恒加注脚的实现红在这条（排除型对照）。"""
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            now = int(time.time())
            write_launch(home, now)
            seed_inbox(home, [("a", now - 600, f"1:group:{GROUP}")])
            msgs = [onebot_msg("a", now - 600, "甲", "在")]
            wg_gap.record(wg_gap.compute(group_ids=[GROUP],
                                        fetch=make_fetcher(msgs), now=now))
            note_present = wg_gap.describe()
            # 无记录文件时也要空串
            wg_gap.clear()
            note_absent = wg_gap.describe()
        finally:
            restore()
    return note_present == "" and note_absent == ""


# ---------- 契约 3：--status 的缺口行 ----------


def status_lines(gap_record=None):
    """跑 `up.report()`，换掉会碰真机的探针，返回输出行列表。"""
    names = ("listener_specs", "listener_pids", "supervisor_pids", "commands_pids",
             "login_info", "port_open", "config_problems", "group_msg_history")
    saved = {name: getattr(up, name, None) for name in names}
    up.listener_specs = lambda *a, **k: []
    up.listener_pids = lambda *a, **k: []
    up.supervisor_pids = lambda *a, **k: []
    up.commands_pids = lambda *a, **k: []
    up.login_info = lambda *a, **k: None
    up.port_open = lambda *a, **k: False
    up.config_problems = lambda *a, **k: []
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            up.report()
    finally:
        for name, value in saved.items():
            if value is None:
                if hasattr(up, name):
                    delattr(up, name)
            else:
                setattr(up, name, value)
    return [ln for ln in buf.getvalue().splitlines() if ln.strip()]


@case
def status_缺口非空时出缺口行():
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            now = int(time.time())
            write_launch(home, now)
            seed_inbox(home, [("a", now - 3600, f"1:group:{GROUP}")])
            msgs = [onebot_msg("d", now - 600, "乙", "在吗", at_qq=ACCOUNT)]
            wg_gap.record(wg_gap.compute(group_ids=[GROUP],
                                        fetch=make_fetcher(msgs), now=now))
            lines = status_lines()
        finally:
            restore()
    gap_line = next((ln for ln in lines if ln.startswith("缺口")), "")
    return ("缺口" in gap_line and "漏 1 条" in gap_line and "@ 本机 1 条" in gap_line)


@case
def status_无缺口时不加缺口行():
    """无缺口时不出现「缺口」行——与 #112 `状态行报出上一轮监听与空窗` 同口径。"""
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            # 不写 wg_gap.json：无记录
            lines = status_lines()
        finally:
            restore()
    return not any(ln.startswith("缺口") for ln in lines)


# ---------- 契约 4：listen.py NO_WORK 路径插缺口提示 ----------


@case
def listen_NO_WORK路径带缺口提示且RESTART仍是末行():
    """缺口非空时 NO_WORK 与 RESTART: 之间插一行 ⚠ 缺口提示。

    复用 `check_listener_rehang.py` 已钉的「NO_WORK 后跟 RESTART:」契约的现场，
    但这里多验一桩：两者之间有缺口提示。`_no_work` 是新拆出的 NO_WORK 路径收尾，
    直接调它（不真跑 watch.py）；WAKE 路径仍走 `_finish`，不带提示。
    """
    import listen  # noqa: E402  模块级 import 会让 listen.main() 跑起来，函数内 import 不会
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore_gap = gap_at(home)
        restore_listener = ledger_at(home)
        try:
            now = int(time.time())
            write_launch(home, now)
            seed_inbox(home, [("a", now - 3600, f"1:group:{GROUP}")])
            msgs = [onebot_msg("d", now - 600, "乙", "在吗", at_qq=ACCOUNT)]
            wg_gap.record(wg_gap.compute(group_ids=[GROUP],
                                        fetch=make_fetcher(msgs), now=now))
            # 造一个最小 args：_no_work 只读 args.timeout
            args = type("A", (), {"timeout": 90})()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = listen._no_work(args)
        finally:
            restore_gap()
            restore_listener()
    out = buf.getvalue()
    work_idx = out.find("NO_WORK")
    gap_idx = out.find("⚠")
    restart_idx = out.find("RESTART:")
    return (code == 0
            and work_idx >= 0 and gap_idx > work_idx
            and "漏 1 条" in out and "@ 本机 1 条" in out
            and restart_idx > gap_idx)


@case
def listen_WAKE路径不插缺口提示():
    """WAKE 路径 Agent 已被叫醒，不需要缺口提示——只 NO_WORK 路径插。"""
    import listen  # noqa: E402  模块级 import 会让 listen.main() 跑起来，函数内 import 不会
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore_gap = gap_at(home)
        restore_listener = ledger_at(home)
        try:
            now = int(time.time())
            write_launch(home, now)
            seed_inbox(home, [("a", now - 3600, f"1:group:{GROUP}")])
            msgs = [onebot_msg("d", now - 600, "乙", "在吗", at_qq=ACCOUNT)]
            wg_gap.record(wg_gap.compute(group_ids=[GROUP],
                                        fetch=make_fetcher(msgs), now=now))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                listen._finish("WAKE", 0)
        finally:
            restore_gap()
            restore_listener()
    return "⚠" not in buf.getvalue()


# ---------- 契约 5：reply.py --list 列漏掉的 @ ----------


@case
def reply_list_缺口非空时列漏掉的at():
    """行首 id=- 区别于真唤醒行（id=<正数>），列出 msg_id / 时间 / 发件人 / 片段。"""
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            now = int(time.time())
            write_launch(home, now)
            seed_inbox(home, [("a", now - 3600, f"1:group:{GROUP}")])
            msgs = [onebot_msg("d", now - 600, "乙", "在吗", at_qq=ACCOUNT)]
            wg_gap.record(wg_gap.compute(group_ids=[GROUP],
                                        fetch=make_fetcher(msgs), now=now))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reply.list_wakes()
        finally:
            restore()
    out = buf.getvalue()
    return ("停机窗口漏掉的 @" in out
            and "id=-" in out and "msg_id=d" in out
            and "乙" in out and "在吗" in out)


@case
def reply_list_无缺口时不加段():
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            # 不写 wg_gap.json：无记录
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reply.list_wakes()
        finally:
            restore()
    return "停机窗口漏掉的 @" not in buf.getvalue()


@case
def reply_list_段标题带窗口时刻():
    """issue #148：`--list` 的缺口段也要说清「哪段时间、账本何时算的」。

    段标题下面紧跟一行窗口上下文，与 `--status` 的缺口行同一份措辞
    （`wg_gap.window_note()`）。只列 `id=-` 行不给窗口的实现红在这里。
    """
    start = local_epoch(2026, 10, 27, 7, 21, 40)
    end = local_epoch(2026, 10, 27, 9, 30, 26)
    computed = local_epoch(2026, 10, 27, 9, 30, 47)
    line = {"msg_id": "d", "time": int(local_epoch(2026, 10, 27, 8, 2, 11)),
            "sender": "乙", "body": "在吗"}
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            wg_gap.record(fixed_record(computed, start, end, missing=1, at_me=1,
                                      at_me_lines=[line]))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reply.list_wakes()
        finally:
            restore()
    out = buf.getvalue()
    head_idx = out.find("停机窗口漏掉的 @")
    note_idx = out.find("07:21:40–09:30:26")
    line_idx = out.find("id=-")
    return (head_idx >= 0 and note_idx > head_idx and line_idx > note_idx
            and "2026-10-27 09:30:47" in out and "仅供参考" in out)


# ---------- 契约 6：200 上限语义（候选 4）----------


@case
def 上限截断时报最近200条():
    """返回的最老一条新于本地最后一条 → truncated=True + describe 说 200 条。"""
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            now = int(time.time())
            write_launch(home, now)
            # 本地最后一条在 now-1000；history 最老一条在 now-500（新于本地最后一条）
            seed_inbox(home, [("a", now - 1000, f"1:group:{GROUP}")])
            msgs = [onebot_msg("d", now - 500, "乙", "在吗", at_qq=ACCOUNT)]
            wg_gap.record(wg_gap.compute(group_ids=[GROUP],
                                        fetch=make_fetcher(msgs), now=now))
            note = wg_gap.describe()
            rec = wg_gap.read()
        finally:
            restore()
    g = rec["groups"][GROUP]
    return (g["truncated"] is True
            and "只能核对最近 200 条" in note)


@case
def 上限覆盖时不说截断():
    """返回的最老一条老于本地最后一条 → truncated=False，describe 不含 200 条句。"""
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            now = int(time.time())
            write_launch(home, now)
            # 本地最后一条在 now-300；history 最老一条在 now-3600（老于本地最后一条）
            seed_inbox(home, [("a", now - 300, f"1:group:{GROUP}")])
            msgs = [onebot_msg("b", now - 3600, "甲", "古早"),
                    onebot_msg("d", now - 100, "乙", "在吗", at_qq=ACCOUNT)]
            wg_gap.record(wg_gap.compute(group_ids=[GROUP],
                                        fetch=make_fetcher(msgs), now=now))
            note = wg_gap.describe()
            rec = wg_gap.read()
        finally:
            restore()
    g = rec["groups"][GROUP]
    return (g["truncated"] is False and "只能核对最近 200 条" not in note)


# ---------- 契约 7：NapCat 未响应 ----------


@case
def napcat未响应时不崩且报无法核对():
    def boom(group_id, count=200):
        raise ConnectionError("NapCat not answering")
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            now = int(time.time())
            write_launch(home, now)
            seed_inbox(home, [("a", now - 3600, f"1:group:{GROUP}")])
            entry = wg_gap.compute(group_ids=[GROUP], fetch=boom, now=now)
            wg_gap.record(entry)
            note = wg_gap.describe()
        finally:
            restore()
    g = entry["groups"][GROUP]
    return (g["unreachable"] is True and g["missing"] == 0
            and "无法核对" in note and "NapCat 未响应" in note)


# ---------- 契约 8：@ 本机判定用 self_account ----------


@case
def 非本机at不计入at_me():
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            now = int(time.time())
            write_launch(home, now)
            seed_inbox(home, [("a", now - 3600, f"1:group:{GROUP}")])
            # @ 的是别人（99999），不是本机
            msgs = [onebot_msg("d", now - 600, "乙", "@别人", at_qq="99999")]
            entry = wg_gap.compute(group_ids=[GROUP], fetch=make_fetcher(msgs), now=now)
        finally:
            restore()
    g = entry["groups"][GROUP]
    return g["missing"] == 1 and g["at_me"] == 0 and g["at_me_lines"] == []


# ---------- 契约 9：本地一条都没有不报缺口 ----------


@case
def 本地为空不报缺口():
    """首次部署 / 换群：本地 chat_inbox 一条都没有，不算停机缺口。"""
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            now = int(time.time())
            write_launch(home, now)
            # 不 seed_inbox：本地空
            msgs = [onebot_msg("d", now - 600, "乙", "在吗", at_qq=ACCOUNT)]
            entry = wg_gap.compute(group_ids=[GROUP], fetch=make_fetcher(msgs), now=now)
        finally:
            restore()
    g = entry["groups"][GROUP]
    return g["missing"] == 0 and g["at_me"] == 0 and g["window_start"] is None


# ---------- 契约 10：窗口右边界取 launch_epoch ----------


@case
def 窗口右边界取launch_epoch():
    """launch_epoch 之外的窗口内消息不算漏（晚于拉起时刻的是拉起后收的）。

    `e.time > launch`（拉起后才到 NapCat 的那条），不该进 `at_me_lines`：拉起后
    NapCat 收到了它，下一轮 collector 长轮询会取到，`chat_inbox` 会有它——不算
    停机漏收。只算 `d`：它 time 在窗口内（`window_start < d.time <= launch`）。
    """
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            now = int(time.time())
            launch = now - 1000
            write_launch(home, launch)
            seed_inbox(home, [("a", now - 3600, f"1:group:{GROUP}")])
            # d 在窗口内（local_last < d.time <= launch），
            # e 在拉起之后（e.time > launch）——不该算漏
            msgs = [onebot_msg("d", launch - 100, "乙", "窗口内", at_qq=ACCOUNT),
                    onebot_msg("e", launch + 900, "丙", "拉起后", at_qq=ACCOUNT)]
            entry = wg_gap.compute(group_ids=[GROUP], fetch=make_fetcher(msgs), now=now)
            g = entry["groups"][GROUP]
        finally:
            restore()
    return (g["window_end"] == launch and g["missing"] == 1 and g["at_me"] == 1
            and g["at_me_lines"][0]["msg_id"] == "d")


def local_epoch(year, month, day, hour, minute, second=0):
    """某个本地墙钟时刻的 epoch 秒（契约 11–13 要钉确定的日期，不用 `time.time()`）。"""
    return datetime.datetime(year, month, day, hour, minute,
                             second).astimezone().timestamp()


def fixed_record(computed, start, end, missing=29, at_me=0, at_me_lines=None):
    """手写一份缺口账本（绕过 compute()，只为把窗口/账本时刻钉成确定的日期）。"""
    return {"computed_at": int(computed),
            "groups": {GROUP: {"missing": missing, "at_me": at_me,
                               "window_start": int(start), "window_end": int(end),
                               "truncated": False, "unreachable": False,
                               "at_me_lines": list(at_me_lines or [])}}}


# ---------- 契约 11：describe() 带窗口起止 + 账本时刻 + 仅供参考 ----------


@case
def describe_缺口非空时带窗口时刻与账本时刻():
    """issue #148：只有「漏 29 条」的账本会被读成现刻事故。

    夹具钉死窗口 07:21:40–09:30:26、账本 09:30:47 计算。生产形态（`describe()`
    不带参数）就必须同时说出**窗口起止**与**账本计算时刻**，并明写「仅供参考」。
    未打补丁的 base 上这条红在「输出里没有窗口」上——正是 issue 的现象。
    """
    start = local_epoch(2026, 10, 27, 7, 21, 40)
    end = local_epoch(2026, 10, 27, 9, 30, 26)
    computed = local_epoch(2026, 10, 27, 9, 30, 47)
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            wg_gap.record(fixed_record(computed, start, end))
            note = wg_gap.describe()
        finally:
            restore()
    return ("07:21:40–09:30:26" in note
            and "2026-10-27 09:30:47" in note
            and "仅供参考" in note
            and "漏 29 条" in note)


@case
def window_note_距今多久可按时刻钉():
    """`window_note(now=...)` 的「多久前」可钉（issue #148）。

    与上一条分开：上一条钉生产形态的措辞，这条钉相对量本身——`now` 是 12:20:00，
    账本 09:30:47 计算，差 2 小时 49 分。相对量算错（拿消息时间当基准、或算成
    绝对时刻）的实现红在这里。
    """
    start = local_epoch(2026, 10, 27, 7, 21, 40)
    end = local_epoch(2026, 10, 27, 9, 30, 26)
    computed = local_epoch(2026, 10, 27, 9, 30, 47)
    now = local_epoch(2026, 10, 27, 12, 20, 0)
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            wg_gap.record(fixed_record(computed, start, end))
            note = wg_gap.window_note(now=now)
        finally:
            restore()
    return ("07:21:40–09:30:26" in note and "2 小时 49 分前" in note
            and "仅供参考" in note)


@case
def describe_无缺口时仍返回空串():
    """排除型对照（issue #148）：恒加窗口注脚的实现红在这条。

    与契约 2 的后半条同口径，但这里专门针对新加的注脚——把窗口无条件拼进
    `describe()` 的实现会在这里红。`window_note()` 自己也要空串（issue #151：
    早先 docstring 这么写但断言只查 `describe()`，恒返回非空的 `window_note()`
    能骗过；当前调用图下不可达，属措辞与断言不一致）。
    """
    start = local_epoch(2026, 10, 27, 7, 21, 40)
    end = local_epoch(2026, 10, 27, 9, 30, 26)
    computed = local_epoch(2026, 10, 27, 9, 30, 47)
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            wg_gap.record(fixed_record(computed, start, end, missing=0))
            empty = wg_gap.describe()
            note = wg_gap.window_note()
        finally:
            restore()
    return empty == "" and note == ""


# ---------- 契约 12：跨天窗口两端补日期 ----------


@case
def 跨天窗口两端补日期():
    """issue #148：夜间停机段的窗口只写时分秒，下午读起来像今天早上漏的。

    夹具：窗口 10-26 23:50:00 → 10-27 07:21:40。两端都要带 `MM-DD`。
    """
    start = local_epoch(2026, 10, 26, 23, 50, 0)
    end = local_epoch(2026, 10, 27, 7, 21, 40)
    computed = local_epoch(2026, 10, 27, 7, 22, 5)
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            wg_gap.record(fixed_record(computed, start, end))
            note = wg_gap.describe()
        finally:
            restore()
    return ("10-26 23:50:00–10-27 07:21:40" in note
            and "仅供参考" in note)


@case
def 同一天窗口不补日期():
    """对照组（issue #148）：恒带日期的实现红在这条。

    同一天的窗口只写 `HH:MM:SS–HH:MM:SS`，多加日期只是噪声。窗口本身仍要在。
    """
    start = local_epoch(2026, 10, 27, 7, 21, 40)
    end = local_epoch(2026, 10, 27, 9, 30, 26)
    computed = local_epoch(2026, 10, 27, 9, 30, 47)
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            wg_gap.record(fixed_record(computed, start, end))
            note = wg_gap.describe()
        finally:
            restore()
    return ("07:21:40–09:30:26" in note and "10-27 07:21:40" not in note)


# ---------- 契约 13：漏收 @ 的时间戳带日期 ----------

# 契约 13 的固定夹具（issue #153 起是两组）。**必须多组**：单组只能证明「这一种窗口
# 形态下实现是对的」，而「按窗口是否跨天决定格式」的条件式实现在另一组上就退化了。
#   跨天窗口 —— #151 的夹具，抓「借窗口/账本日期冒充消息日期」
#   同日窗口 —— 生产里最常见的停机窗口（早上拉起、窗口 07:21–09:30），抓「跨天才
#               补日期」这类照抄 `wg_clock.window()` 习惯的实现（#153 L）
GAP_FIXTURES = (
    ("跨天窗口", (2026, 10, 25, 22, 0, 0), (2026, 10, 27, 7, 21, 40),
     (2026, 10, 26, 3, 5, 9)),
    ("同日窗口", (2026, 10, 27, 7, 21, 40), (2026, 10, 27, 9, 30, 26),
     (2026, 10, 27, 8, 2, 11)),
)


def _gap_line_ok(start, end, missed_at):
    """一组夹具跑一遍：漏收行的时间戳字段必须**等于消息自己的** `stamp()`。

    `computed_at` 取 `end + 60`（账本在窗口结束后算，早于 `window_end` 不合理）。
    """
    line = {"msg_id": "d", "time": int(missed_at), "sender": "乙", "body": "在吗"}
    with temp_dir(prefix="wg-gap-") as home:
        fixture_home(home)
        restore = gap_at(home)
        try:
            wg_gap.record(fixed_record(end + 60, start, end, missing=1, at_me=1,
                                      at_me_lines=[line]))
            lines = wg_gap.at_me_lines()
        finally:
            restore()
    if len(lines) != 1:
        return False
    text = lines[0]
    msg_stamp = wg_clock.stamp(missed_at)
    # 判据是**行首时间戳字段本身**，不是整行子串（#151 复核）：行尾补一句正确
    # 时刻不该算通过。唤醒行是 `id=<正数> HH:MM:SS `，这里钉 `id=- msg_id=<id>
    # YYYY-MM-DD HH:MM:SS `——两者第一眼就能分开。
    # 注意**不要**在这里加「消息日期必须与窗口两端不同」的通用前提：同日窗口那
    # 组（issue #153）消息必然落在窗口那一天，加了会让正确实现直接判红。那一类
    # 前提只在跨天组有意义，写在 `漏收at的时间戳带日期` 的循环里。
    m = STAMPED_GAP_LINE.match(text)
    return (m is not None
            and m.group(1) == "d"
            and m.group(2) == msg_stamp
            and "乙" in text and "在吗" in text)


@case
def 漏收at的时间戳带日期():
    """issue #148：漏掉的 @ 可能是昨夜停机时段的，只给时分秒会读成「刚漏的」。

    `at_me_lines()` 用 `stamp()`（带日期），与唤醒行的 `clock()`（`HH:MM:SS`）
    不同形——两者同形时 Agent 分不清哪条是现刻待回、哪条是账本历史。

    判据比对**行首时间戳字段本身**，不是在整行里找子串（#151 复核）：子串判据
    挡不住「头字段日期错、行尾补一句正确时刻」的实现。

    **四组夹具缺一不可**（issue #153），它们各堵一个维度的条件式退化：

    1. 跨天窗口（固定日期）—— #151 的夹具，抓「借窗口/账本日期冒充消息日期」。
    2. 同日窗口（固定日期）—— 生产里最常见的停机窗口（早上拉起、07:21–09:30）。
       夹具一直跨天时，「按窗口是否跨天决定格式」的实现有现成模仿对象
       `wg_clock.window()`（跨天才补日期），在同日窗口下它退回 `clock()`，输出
       与唤醒行同形，issue #148 的 bug 原样回来。
    3. 当天零点后（运行日构造）—— 消息落在「今天」但离 now 很远。抓「今天的
       消息不带日期」这类条件式 stamp：固定日期抓它只是「机器今天恰好等于夹具
       日」的巧合，换一天就漂移。
    4. 相对 now（消息 60 秒前）—— 消息紧贴 now。抓「最近 N 小时内退回
       `clock()`」这类**按消息新旧**判定的实现（理由通常是「反正就是刚才的」），
       这类实现的消息可能距 now 只有几十秒、却仍属「今天」，第 3 组够不着。

    第 3、4 组互补且不可互替：第 3 组的消息时刻是**固定**的（当天 00:01:01），
    距 now 多远完全取决于运行时刻——只有在 00:01–01:01 之间跑时才恰好紧贴
    now，其余时间它离 now 几小时到二十几小时；第 4 组的消息时刻是**相对 now
    构造**的（`now - 60`），永远紧贴 now。`57b8ccc` 曾把第 4 组整个换成第 3 组，
    结果 recency 类实现在 288 个时刻里 275 个假绿。

    本用例仍覆盖不到的（都是固定夹具的已知边界，base 与本 PR 同假绿，非本 PR
    引入，`#154` 复核逐条列出）：

    - 把日期**硬编码**成某一组固定夹具那一天的实现。多组夹具提高了它的成本但
      没根除（两组固定日期 + 两组运行日构造）。
    - `missing == 0` 时省掉日期的实现。`at_me_lines()` 的契约是「漏收行必须带
      日期」，与「本群是否真漏了消息」无关；夹具固定 `missing=1`，该分支不触发。
    - 把未来时刻 `min(msg, now)` 夹到 now 的实现。夹具消息恒在过去或恰在 now
      附近，夹取后与正确实现同形。

    所以本用例的判据是「这四类条件式退化必红」，不是「任何错日期都会红」——
    docstring 不这么声称。
    """
    for _name, s, e, missed in GAP_FIXTURES:
        start, end, msg_at = local_epoch(*s), local_epoch(*e), local_epoch(*missed)
        # 夹具前提（只对跨天组有意义）：消息日期必须与窗口两端都不同，否则「借
        # 窗口日期冒充消息日期」的坏实现与正确实现同形，本组退回零判别力。同日
        # 组里消息必然落在窗口那一天，这条不适用——所以按窗口形态判断，不能无
        # 条件加。
        if wg_clock.stamp(start)[:10] != wg_clock.stamp(end)[:10]:
            if wg_clock.stamp(msg_at)[:10] in {wg_clock.stamp(start)[:10],
                                               wg_clock.stamp(end)[:10]}:
                return False
        if not _gap_line_ok(start, end, msg_at):
            return False
    # 第 3 组（当天零点后，运行日构造）：专抓「今天的消息不带日期」这类条件式
    # stamp（#153 J）。
    #
    # 为什么这组必须存在：J 的坏行为只在「消息日 == 实现读到的今天」时出现，固定
    # 日期夹具只能靠「运行日恰好等于那个固定日」撞上，撞不上就假绿（`447ca55` 上
    # J 今天恰好 `False` 就是这个巧合）。
    #
    # 窗口必须**落在同一个自然日**，否则「跨天才补日期」那类实现会走 `stamp()`
    # 分支、输出恰好正确，这组就抓不住它。用固定偏移做不到（`now - 1800` 在
    # 00:29 跑就跨天），所以取「运行日当天零点后 1 秒 → +120 秒」，消息落在
    # 中间。三者必然同一天、必然都在「今天」，与运行时刻无关。
    now = int(time.time())
    day_start = int(time.mktime(tuple(time.localtime(now)[:3]) + (0, 0, 1, 0, 0, -1)))
    win_start, win_end = day_start, day_start + 120
    msg_at = day_start + 60
    # 消息时间必须真的落在「今天」，否则这组抓不住 J（J 只在消息是今天时才退化）
    if wg_clock.stamp(msg_at)[:10] != wg_clock.stamp(now)[:10]:
        return False
    if not _gap_line_ok(win_start, win_end, msg_at):
        return False

    # 第 4 组（相对 now）：专抓「最近 N 小时内退回 `clock()`」这类**按消息新旧**
    # 判定的实现（#153 复核 `recency-1h`）。第 3 组的消息离 now 最远可达 23 小时，
    # 一个「1 小时内算刚漏的」实现完全可以在那组上走 `stamp()` 分支、输出正确，
    # 却在真正紧贴 now 的消息上退回 `clock()`——而那正是生产里最常出现的情形。
    #
    # 偏移取 `now - 7200 → now`（消息 `now - 60`）而不是更小：窗口两端差 2 小时，
    # 消息落在其中，仍属「今天」的概率高；窗口跨午夜时「跨天才补日期」类实现会
    # 走 `stamp()` 分支输出正确，但那一类已被第 2 组（同日固定窗口）堵住，这里
    # 只需保证 recency 维度被覆盖，不必也不该再要求「窗口同一天」（那会让 00:00
    # 附近跑时这组直接失效，正是 `57b8ccc` 之前 `now-1800` 踩过的坑）。
    if not _gap_line_ok(now - 7200, now, now - 60):
        return False
    return True


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 72)
    print("        [测试] issue #114 停机窗口缺口披露：漏了什么事后说得出来")
    print("=" * 72)
    bad = 0
    for fn in CASES:
        try:
            got = fn()
        except Exception as exc:  # noqa: BLE001
            got = False
            print(f"✗ {fn.__name__}: {type(exc).__name__}: {exc}")
            bad += 1
            continue
        if got:
            print(f"✓ {fn.__name__}")
        else:
            bad += 1
            print(f"✗ {fn.__name__}")
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
