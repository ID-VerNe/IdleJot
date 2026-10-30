"""钉住停机窗口缺口披露门禁的夹具层与状态渲染（issue #114 / #283）。

从旧 `scripts/check_gap_disclosure.py` 整体搬运，按 #169 PF-2（`scripts/check_listener_rehang/`）
与 #264（`scripts/check_dossier_cadence/`）先例拆成子模块。本模块只放数据根钉、daemon
import、常量、夹具函数（`fixture_home`/`gap_at`/`ledger_at`/`write_launch`/`seed_inbox`/
`onebot_msg`/`make_fetcher`）、`CASES`/`case` 收集器与 `status_lines` 模板渲染 helper，
所有 25 条用例按 issue 分组落进 `cases`。

与 `check_dossier_cadence/_common.py` 同口径：包式门禁经 `python -m scripts.check_gap_disclosure`
调起，此时 `sys.path[0]` 是仓库根（cwd）而非 `scripts/`——后者才是 `check_fixture_support`
所在地。手动把 `scripts/` 插进 `sys.path`，再进口 `check_fixture_support`，随后
`bootstrap_paths(scripts=False)` 收口 root + daemon（issue #283）。
"""
import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 包式门禁经 `python -m scripts.check_gap_disclosure` 调起，此时 `sys.path[0]` 是
# 仓库根（cwd）而非 `scripts/`——后者才是 `check_fixture_support` 所在地。无法先调
# `bootstrap_paths`（它自己要 import fs），故手动把 `scripts/` 插进 `sys.path`，
# 再进口 `check_fixture_support`，随后 `bootstrap_paths(scripts=False)` 收口 root + daemon
# （issue #283）。`scripts=False`：`scripts/` 已在上面手动插过，避免重复。
_scripts = Path(__file__).resolve().parents[1]
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import scratch_dir, temp_dir  # noqa: E402
fs.bootstrap_paths(scripts=False)

# issue #145：本门禁有两处 `reply.list_wakes()`，它现在会顺手复查发送失败账本
# （`wg_send_failure.LEDGER` 是 import 期求值的 `state_dir() / ...`）。不钉
# `IDLEJOT_WG_STATE`，外部环境带着它跑时会**写实机账本**（`rechecked_at`/
# `recheck_result`），还吃掉 60 秒复查窗口让真正的 `reply.py --list` 跳过复查。
# `state_dir()` 优先级 IDLEJOT 先于 TULPA，只靠 `check_fixture_support` 的
# `WATER_GROUP_HOME` 钉不住它。
_GAP_STATE = scratch_dir(prefix="wg_gap_state_")
os.environ["IDLEJOT_WG_STATE"] = str(_GAP_STATE / "state")

import up  # noqa: E402
import wg_clock  # noqa: E402
import wg_config  # noqa: E402  (config.json 的唯一读取口；CONFIG 在 import 期求值，需打桩)
import wg_gap  # noqa: E402
import wg_isolation  # noqa: E402  (group_of：认群口径的 oracle，issue #211)
import wg_listener_state  # noqa: E402  (与 #112 账本同构，对照用)
import reply  # noqa: E402  (list_wakes 用例需要；模块级 import 触发不进 daemon loop)

ACCOUNT = "10001"
GROUP = "123"

# 漏收行形态（契约 13）：`id=- msg_id=<id> YYYY-MM-DD HH:MM:SS [漏] <人>: <正文>`。
# 唤醒行是 `id=<正数> HH:MM:SS ...`——带日期是两者的分界线。两个捕获组（#151 复核）：
# `(\\S+)` 是消息 id、`(...)` 是行首时间戳字段。判据必须比对**捕获到的那个字段**，不能
# 在整行找子串——子串在行尾也命中，「头字段日期错、行尾补一句正确时刻」的实现子串下绿。
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

    `wg_gap` 在模块级求值 `DB`，`wg_config` 在 import 期求 `CONFIG`——两处都得在用例里
    临时改写指向夹具目录，与 `wg_listener_state.STATE` 打桩同款。`state_dir()` 读
    `IDLEJOT_WG_STATE`（`wg_paths.py:35`），**不**随 `WATER_GROUP_HOME` 走
    （`check_fixture_support` 只钉 `WATER_GROUP_HOME`）。`wg_gap._launch_epoch()` 从
    `state_dir()` 找 `napcat_launch.json`，所以这里必须连 `IDLEJOT_WG_STATE` 指到夹具
    `state/`，否则它读 import 期被 `_PINNED_HOME` 钉住的那份、永远找不到用例写的 launch
    文件——`window_end` 回落成 `now`，右边界断言假红。
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

    #119 后监听账本改成 `wg_listeners/<pid>.json` 一槽一份，模块级绑定是 `STATE_DIR`
    （新）与 `LEGACY_STATE`（旧，只读兼容）；更早的 #112 布局只有 `STATE`。三套名字都
    用 `hasattr` 探测后改写：跑新模块时 `STATE` 不存在，跑旧基线时 `STATE_DIR` 不存在
    ——基线复跑两套路径都指到 `home`，否则旧模块读写实机账本。与 `check_listener_rehang`
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
        db.executescript(
            "CREATE TABLE IF NOT EXISTS chat_inbox("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
            "message_id TEXT NOT NULL, payload TEXT NOT NULL, at REAL NOT NULL, "
            "UNIQUE(session_id,message_id));"
            "CREATE INDEX IF NOT EXISTS chat_inbox_session ON chat_inbox(session_id,id);")
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
