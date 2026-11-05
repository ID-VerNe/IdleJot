"""issue #390 gate: group_ban notice 要记下操作者，且要能被 --list 看见。

issue #390 的现场：机器人被管理员禁言 / 解禁时 NapCat 推送 ``group_ban`` notice，
但整条链路只处理消息系事件——``MCPChat.receive`` 把 notice 落进 ``post_type`` 早退
（只给 recall 开了口子），感知禁言全靠发消息失败后探 ``shut_up_timestamp``：既
不知道是谁禁的，更不知道是谁提前解的。

本门禁钉住四层（数据根 / 状态目录 / 库 / 配置全部钉进临时目录，import 任何被测
模块之前完成，同 check_unmute_probe 的纪律）：

1. 解析相关性（``chatlocal.mcp_chat_ban.parse``）：本机被禁（``user_id == self_id``）
   与全员禁言（``user_id == 0``）才记；别的群友被禁不记；sub_type 不认 / operator
   缺失 / 非对象一律 None，不记也不崩——「不知道是谁」的事实不许落盘；
2. 落盘（``record_group_ban``）：ban 与 lift 各留最新一条、同一群两条并存，两个群
   的事实互不冲掉，文件是合法 JSON；
3. 路由（``MCPChat.receive``）：group_ban notice 要走到落盘；无关 notice
   （group_upload）与消息系事件不走这条——用裸实例 + 桩 apps 测，不建库；
4. 展示（``water-group/daemon/wg_group_ban`` + ``reply.list_wakes``）：事实行带
   operator / 时长 / **完整日期**的到期时刻（#392 的教训：只报时分秒会让人把
   「明天 18:25」读成「今天 18:25」）；解禁行带「此前被谁禁言多久」；钉死群时
   别的群的事实不串行；没有事实时一行都不多打；``reply.py --list`` 端到端出这行。

明确边界：事件到达即清账本 / 立刻 WAKE 属 issue #391，本门禁不测——这些用例里
``wg_send_failure.json`` 根本不出现。

    .venv/Scripts/python.exe scripts/check_group_ban.py
"""
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: E402,F401  (先钉数据根，见 issue #79/#89)
from check_fixture_support import pin_state_env, scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False)

# 数据根 / 状态目录 / 库 / 配置全部钉进临时目录，import 任何 chatlocal / daemon
# 模块之前完成：`chatlocal.config` 的 STATE 与 `wg_paths.state_dir()` 都是 import 期
# 求值的，晚一步就会指向实机（check_unmute_probe 同一条纪律）。state 两腿钉合走
# `pin_state_env`（#281），app 侧与 daemon 侧才落在同一个 state 目录里。
_SCRATCH = scratch_dir(prefix="wg_group_ban_")
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
pin_state_env(_SCRATCH / "state")
os.environ["IDLEJOT_WG_DB"] = str(_SCRATCH / "chats.sqlite3")
os.environ["IDLEJOT_WG_CONFIG"] = str(_SCRATCH / "config.json")
for path in ("home", "state"):
    (_SCRATCH / path).mkdir(parents=True, exist_ok=True)

import chatlocal.mcp_chat  # noqa: E402  (import 期钉根之后才轮到它)
import chatlocal.mcp_chat_ban as mcp_chat_ban  # noqa: E402
import reply  # noqa: E402
import wg_group_ban  # noqa: E402

GROUP = "777000111"
OTHER_GROUP = "777000222"
SELF = "99999"
OPERATOR = "10001"
OTHER_OPERATOR = "10002"
STATE = _SCRATCH / "state"
FACTS = STATE / "wg_group_ban.json"

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def _reset_facts():
    try:
        FACTS.unlink()
    except OSError:
        pass


def _read_facts():
    try:
        return json.loads(FACTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ban(group=GROUP, operator=OPERATOR, user=SELF, duration=600, sub="ban", at=None):
    event = {"post_type": "notice", "notice_type": "group_ban", "sub_type": sub,
             "group_id": int(group), "operator_id": int(operator),
             "user_id": int(user) if user is not None else None,
             "duration": duration, "self_id": int(SELF)}
    if at is not None:
        event["time"] = at
    return event


@case
def 本机被禁记下操作者与时长():
    """user_id == self_id 的 ban：operator_id / duration / 时刻都要进事实文件。"""
    _reset_facts()
    ok = mcp_chat_ban.record_group_ban(_ban(duration=600), now=1700000000)
    data = _read_facts() or {}
    fact = ((data.get("groups") or {}).get(GROUP) or {}).get("ban") or {}
    return (ok is True
            and fact.get("operator_id") == OPERATOR
            and fact.get("duration") == 600
            and fact.get("at") == 1700000000
            and fact.get("action") == "ban")


@case
def 全员禁言也记():
    """user_id == 0（全员禁言）也是本机的事实：NapCat 全员禁言常常不给成员写
    shut_up_timestamp，只有事件能感知。"""
    _reset_facts()
    ok = mcp_chat_ban.record_group_ban(_ban(user="0", duration=1800), now=1700000000)
    fact = ((mcp_chat_ban._read().get("groups") or {}).get(GROUP) or {}).get("ban") or {}
    return ok is True and fact.get("user_id") == "0" and fact.get("duration") == 1800


@case
def 别人的禁言不记():
    """user_id 既不是本机也不是 0：别人的禁言不是本机的事实，一个字都不落盘。"""
    _reset_facts()
    parsed = mcp_chat_ban.parse(_ban(user="88888"))
    ok = mcp_chat_ban.record_group_ban(_ban(user="88888"))
    return parsed is None and ok is False and _read_facts() is None


@case
def 解禁记下操作者并与禁言并存():
    """lift_ban 落在同一个群条目里：ban 与 lift 各留最新一条，谁禁的与谁解的同时可见。"""
    _reset_facts()
    mcp_chat_ban.record_group_ban(_ban(operator=OPERATOR), now=1700000000)
    mcp_chat_ban.record_group_ban(_ban(operator=OTHER_OPERATOR, sub="lift_ban"),
                                  now=1700000300)
    entry = (mcp_chat_ban._read().get("groups") or {}).get(GROUP) or {}
    ban, lift = entry.get("ban") or {}, entry.get("lift_ban") or {}
    return (ban.get("operator_id") == OPERATOR
            and lift.get("operator_id") == OTHER_OPERATOR
            and lift.get("action") == "lift_ban")


@case
def 字段不全不记也不崩():
    """operator 缺失 / sub_type 不认 / post_type 是消息 / 事件不是对象：一律 None，
    不抛异常——「不知道是谁」的事实不许落盘。"""
    _reset_facts()
    no_operator = _ban()
    del no_operator["operator_id"]
    bad = [no_operator, _ban(sub="poke"),
           {"post_type": "message", "notice_type": "group_ban", "sub_type": "ban"},
           None, ["not", "a", "dict"]]
    return all(mcp_chat_ban.parse(e) is None for e in bad) and _read_facts() is None


@case
def 两个群的事实互不冲掉():
    """A 群的 ban 不会冲掉 B 群的 lift：文件按群号分组，不是单条全局记录。"""
    _reset_facts()
    mcp_chat_ban.record_group_ban(_ban(group=GROUP), now=1700000000)
    mcp_chat_ban.record_group_ban(_ban(group=OTHER_GROUP, sub="lift_ban"), now=1700000100)
    groups = (mcp_chat_ban._read().get("groups") or {})
    return ("ban" in (groups.get(GROUP) or {})
            and "lift_ban" in (groups.get(OTHER_GROUP) or {}))


@case
def receive把group_ban路由到落盘():
    """`MCPChat.receive` 对 group_ban notice 要走到 record；无关 notice 与消息系
    事件不走这条。裸实例 + 桩 apps（receive 的前两道门都不碰库）。"""
    _reset_facts()
    host = object.__new__(chatlocal.mcp_chat.MCPChat)
    host.available = True
    host.apps = SimpleNamespace(receive=lambda event: False)
    with patch("chatlocal.mcp_chat_ban.record_group_ban") as record:
        host.receive(_ban())
        host.receive(_ban(sub="lift_ban"))
        unrelated = host.receive({"post_type": "notice", "notice_type": "group_upload",
                                  "self_id": int(SELF)})
        message = host.receive({"post_type": "message", "message_type": "",
                                "self_id": int(SELF)})
    calls = [json.dumps(c.args[0], sort_keys=True) for c in record.call_args_list]
    return (len(calls) == 2
            and json.dumps(_ban(), sort_keys=True) in calls
            and json.dumps(_ban(sub="lift_ban"), sort_keys=True) in calls
            and unrelated is None and message is None)


@case
def 事实行带操作者时长与完整日期():
    """ban 行：operator / 时长 / 到期时刻俱全，且到期时刻是**完整日期**（#392 的
    教训）；未到期报剩余秒数，到期改口「已到点」，不冒充已解禁。"""
    _reset_facts()
    mcp_chat_ban.record_group_ban(_ban(duration=600), now=1700000000)
    live = wg_group_ban.line(GROUP, now=1700000060)
    expired = wg_group_ban.line(GROUP, now=1700000700)
    import wg_clock
    until = wg_clock.stamp(1700000600)
    return (f"被 QQ {OPERATOR} 禁言 600 秒" in live
            and f"至 {until}，还剩 540 秒" in live
            and f"至 {until}，已到点" in expired
            and "还剩" not in expired)


@case
def 全员禁言行说清是全员():
    """user_id == 0 的 ban 行要说「设全员禁言」，与单人禁言的措辞分开。"""
    _reset_facts()
    mcp_chat_ban.record_group_ban(_ban(user="0", duration=60), now=1700000000)
    live = wg_group_ban.line(GROUP, now=1700000010)
    return f"被 QQ {OPERATOR} 设全员禁言 60 秒" in live


@case
def 解禁行带此前禁言():
    """lift 行：谁解的 + 此前被谁禁言多久（同一条街读完整个事件），日期全格式。"""
    _reset_facts()
    mcp_chat_ban.record_group_ban(_ban(operator=OPERATOR, duration=600), now=1700000000)
    mcp_chat_ban.record_group_ban(_ban(operator=OTHER_OPERATOR, sub="lift_ban"),
                                  now=1700000300)
    text = wg_group_ban.line(GROUP, now=1700000400)
    import wg_clock
    return (f"被 QQ {OTHER_OPERATOR} 解禁" in text
            and f"此前 {wg_clock.stamp(1700000000)} 被 QQ {OPERATOR} 禁言 600 秒" in text)


@case
def 钉死群只看本群事实():
    """本会话钉死群 B 时：B 没有事实就不出声（不把 A 群的事实串进来）；未钉死取
    最新并带 group= 标注——与 wg_send_failure.print_last 的全局口径一致。"""
    _reset_facts()
    mcp_chat_ban.record_group_ban(_ban(group=GROUP), now=1700000000)
    pinned = wg_group_ban.line(OTHER_GROUP, now=1700000100)
    mine = wg_group_ban.line(GROUP, now=1700000100)
    unpinned = wg_group_ban.line("", now=1700000100)
    return (pinned == ""
            and f"group={GROUP}" in mine and OPERATOR in mine
            and f"group={GROUP}" in unpinned and OPERATOR in unpinned)


@case
def 无事实一行不出声():
    """文件不存在 / 本群没有事实：line 返回空串、print_line 一个字不打——不造假事实。"""
    _reset_facts()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        wg_group_ban.print_line(GROUP)
    return wg_group_ban.line(GROUP) == "" and buf.getvalue() == ""


@case
def list端到端出事实行():
    """`reply.py --list` 抬头（wg_wakes.list_wakes）要打出这条事实行：钉死群时带
    本群的禁言事实，operator 到达 Agent 眼前——issue #390 的验收形态。"""
    _reset_facts()
    os.environ["IDLEJOT_WG_CONVERSATION"] = f"{SELF}:group:{GROUP}"
    try:
        # 会话库最小骨架（wg_gap / wg_dossiers 会顺手读库；库文件缺表不该红在别处）。
        import sqlite3
        db = sqlite3.connect(os.environ["IDLEJOT_WG_DB"])
        db.execute("CREATE TABLE IF NOT EXISTS chat_inbox "
                   "(id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT,"
                   " at REAL NOT NULL, media TEXT NOT NULL DEFAULT '[]')")
        db.execute("CREATE TABLE IF NOT EXISTS chat_sessions "
                   "(id TEXT, conversation_id TEXT, active INTEGER)")
        db.commit()
        db.close()
        mcp_chat_ban.record_group_ban(_ban(duration=600), now=1700000000)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            reply.list_wakes()
        out = buf.getvalue()
    finally:
        del os.environ["IDLEJOT_WG_CONVERSATION"]
    return (f"# 禁言事件：" in out and f"被 QQ {OPERATOR} 禁言 600 秒" in out
            and f"group={GROUP}" in out)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return fs.run_cases(CASES)


if __name__ == "__main__":
    sys.exit(main())
