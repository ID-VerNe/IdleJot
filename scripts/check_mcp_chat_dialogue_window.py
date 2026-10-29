"""issue #217 gate: mention_only 的对话窗口必须跨调用、跨进程存活。

issue #217 的症状不在单次 `wait_chat_messages` 里，而在**两次调用之间**：

  * 窗口（`active_until` / `remaining_followups`）只活在 `MCPChat` 的内存字典里，
    服务重启（collector 重连、`reload.ps1`、整栈重启）就没了；
  * 窗口的起算点挂在「被 @ 的那一刻」，而 Agent 思考加组装回复常常比
    `followup_seconds` 久——等它回完，窗口早过期，于是「回复之后那几句」必落进
    mention_only 的跳批分支；
  * 跳批分支还把整批置 `delivered=1`：一条没交给过任何人的消息被记成已投递，
    `--context` 看得见、追问窗口看不见、按 `delivered` 判已处理的下游全漏。

所以本门禁钉四层可观察契约，全部只碰临时库、不发任何 QQ 请求：

  1. 跨 wait 调用：同一会话两次 wait，第一次以 @ 收尾，第二次只来闲聊 → 仍投递；
  2. 跨进程：换一个 `MCPChat` 实例（模拟服务重启）后同样要投递——内存态到这里
     已经全丢，能过只可能是因为窗口落在 `chat_sessions` 里；
  3. 跳批只推游标、不置 `delivered`：窗口关掉之后那一批必须「没消息、游标前进、
     `delivered` 仍是 0」，否则追问窗口与 `--context` 又会和 MCP 侧分叉；
  4. 本机发出的回复要重开窗口：`reply.py` 直发的回执没有
     `send_request.operation_id`，`consume_sent_echoes` 不认，窗口只能靠 is_self
     行重新起算——不钉这条，第 2 层绿了也接不上「回复之后」的真实场景。

未打补丁的 base 上本门禁必须红，且红在第 2 层（换实例后不投递）与第 3 层
（跳批把行记成 delivered=1），不是 import 裸奔。

    /usr/bin/env -u PYTHONPATH .venv/Scripts/python.exe scripts/check_mcp_chat_dialogue_window.py
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import check_fixture_support as fs  # noqa: F401,E402  (issue #169 PE: path 注入收口)
from check_fixture_support import scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False)

SCRATCH = scratch_dir(prefix="tulpa-dialogue-window-")
os.environ["WATER_GROUP_HOME"] = str(SCRATCH / "home")
os.environ["IDLEJOT_WG_STATE"] = str(SCRATCH / "state")
os.environ["IDLEJOT_WG_DB"] = str(SCRATCH / "mcp-access.sqlite3")
(SCRATCH / "home").mkdir(parents=True, exist_ok=True)
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)

from chatlocal.mcp_access import MCPAccess  # noqa: E402
from chatlocal.mcp_actions import MCPActions  # noqa: E402
from chatlocal.mcp_chat import MCPChat  # noqa: E402
from chatlocal.store import Store  # noqa: E402

CID = "111:group:223"
GROUP = "223"
ACCOUNT = "111"
PEER = "444"
SESSION = "dialogue-window-fixture"
FAILURES = []


def build(name):
    """一个只碰临时库的 MCPChat：假 grant + 假会话，接收器不会连到任何地方。

    scratch_dir 跨运行复用，所以每个用例先把自己那份库删干净——不然上一次
    跑剩下的行会让 AUTOINCREMENT 继续往上走，断言按 id 对齐就失真了。
    """
    path = SCRATCH / f"chats-{name}.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        leftover = Path(str(path) + suffix)
        if leftover.exists():
            leftover.unlink()
    store = Store(path)
    access = MCPAccess(store)
    access.configure(True, 18777)
    scope = dict(platforms=["qq"], conversations=[json.dumps(["qq", CID])],
                 chat=True, send=True, chat_account=ACCOUNT, start="", end="")
    with access.connect() as db:
        db.execute("DELETE FROM grants")
        db.execute("INSERT INTO grants(id,name,token_hash,scope,epoch,accounts,created_at)"
                   " VALUES(?,?,?,?,?,?,?)",
                   ("g1", "fixture", "fixture-hash", json.dumps(scope), "fixture-epoch",
                    json.dumps({"qq": ACCOUNT}), time.time()))
    chat = MCPChat(access, MCPActions(access))
    with access.connect() as db:
        db.execute("DELETE FROM chat_sessions WHERE id=?", (SESSION,))
        db.execute("DELETE FROM chat_inbox WHERE session_id=?", (SESSION,))
        db.execute("INSERT INTO chat_sessions(id,grant_id,conversation_id,name,persona,"
                   "participation,request_key,signature,active,created,updated,cursor,"
                   "offered,note,last_contact,stop_reason,source)"
                   " VALUES(?,?,?,?,?,?,?,?,1,?,?,0,0,'',0,'','onebot')",
                   (SESSION, "g1", CID, "fixture", "fixture", "mention_only", "k", "sig",
                    time.time() - 60, time.time()))
    return chat, access


def grant_of(access):
    return dict(id="g1", scope=access.by_id("g1", source=False)["scope"])


def inject(chat, text, message_id, at_me=False, self_sent=False):
    """走 receive() 的真实入库路径，模拟 OneBot 推来的一条群消息，返回它的事件 id。"""
    uid = ACCOUNT if self_sent else PEER
    segs = [dict(type="at", data=dict(qq=ACCOUNT))] if at_me else []
    segs.append(dict(type="text", data=dict(text=text)))
    chat.receive(dict(post_type="message", message_type="group", self_id=int(ACCOUNT),
                      group_id=int(GROUP), message_id=message_id, user_id=int(uid),
                      sender=dict(user_id=int(uid)), time=time.time(), message=segs))
    with chat.access.connect() as db:
        row = db.execute("SELECT id FROM chat_inbox WHERE session_id=? AND message_id=?",
                         (SESSION, str(message_id))).fetchone()
    assert row is not None, f"receive() 没有把 message_id={message_id} 写进 chat_inbox"
    return row["id"]


def wait(chat, grant, **extra):
    args = dict(session_id=SESSION, timeout_seconds=1, quiet_seconds=0, mention_only=True)
    args.update(extra)
    return chat.wait(grant, args, threading.Event())


def ids(result):
    return [m["id"] for m in result.get("messages") or []]


def inbox(access):
    with access.connect() as db:
        return [(r["id"], r["delivered"]) for r in
                db.execute("SELECT id,delivered FROM chat_inbox WHERE session_id=? ORDER BY id",
                           (SESSION,))]


def case_window_survives_between_calls():
    chat, access = build("calls")
    grant = grant_of(access)
    try:
        at_id = inject(chat, "有人吗", 1, at_me=True)
        first = wait(chat, grant)
        assert ids(first) == [at_id], (ids(first), first.get("event"))
        chat_id = inject(chat, "哈哈哈哈", 2)
        second = wait(chat, grant, acknowledge_through_id=first["read_through_id"])
        assert ids(second) == [chat_id], ("同进程两次 wait 之间窗口丢了", ids(second), second.get("event"))
    finally:
        chat.receiver.stop()


def case_window_survives_restart():
    """换一个 MCPChat 实例：内存态全丢，还能投递就只可能是从库里读回来的。"""
    chat, access = build("restart")
    grant = grant_of(access)
    try:
        at_id = inject(chat, "有人吗", 1, at_me=True)
        first = wait(chat, grant)
        assert ids(first) == [at_id], (ids(first), first.get("event"))
        with access.connect() as db:
            row = db.execute("SELECT dialogue_until,dialogue_turns FROM chat_sessions WHERE id=?",
                             (SESSION,)).fetchone()
        assert float(row["dialogue_until"]) > time.time() and int(row["dialogue_turns"]) > 0, (
            "issue #217：被 @ 之后窗口没有落库", dict(row))
        chat.receiver.stop()
        chat = MCPChat(access, MCPActions(access))  # 服务重启
        chat_id = inject(chat, "哈哈哈哈", 2)
        more_id = inject(chat, "确实", 3)
        second = wait(chat, grant, acknowledge_through_id=first["read_through_id"])
        assert ids(second) == [chat_id, more_id], (
            "issue #217：服务重启后，回复之后的续接闲聊没有投递", ids(second), second.get("event"))
    finally:
        chat.receiver.stop()


def case_skip_keeps_delivered_zero():
    """窗口关掉之后的那批：没消息、游标前进、delivered 保持 0。"""
    chat, access = build("skip")
    grant = grant_of(access)
    try:
        at_id = inject(chat, "有人吗", 1, at_me=True)
        first = wait(chat, grant)
        assert ids(first) == [at_id], (ids(first), first.get("event"))
        with access.connect() as db:
            db.execute("UPDATE chat_sessions SET dialogue_until=0,dialogue_turns=0 WHERE id=?", (SESSION,))
        skip_id = inject(chat, "后来的闲聊", 2)
        idle = wait(chat, grant, acknowledge_through_id=first["read_through_id"])
        assert not idle.get("messages"), ("窗口关了还投递闲聊", ids(idle))
        assert chat.row(SESSION)["cursor"] >= skip_id, "跳批没有推进游标，下一轮会重复看见"
        assert inbox(access)[-1] == (skip_id, 0), "issue #217：跳批把没交付的行记成了 delivered=1"
    finally:
        chat.receiver.stop()


def case_own_reply_reanchors():
    """回复（is_self 回执）重新起算窗口，回复之后的闲聊才接得上。"""
    chat, access = build("reply")
    grant = grant_of(access)
    try:
        at_id = inject(chat, "有人吗", 1, at_me=True)
        first = wait(chat, grant)
        assert ids(first) == [at_id], (ids(first), first.get("event"))
        # 窗口过期（Agent 想得比 followup_seconds 久），此时才发出回复
        with access.connect() as db:
            db.execute("UPDATE chat_sessions SET dialogue_until=0,dialogue_turns=0 WHERE id=?", (SESSION,))
        echo_id = inject(chat, "我在", 2, self_sent=True)
        echo = wait(chat, grant, acknowledge_through_id=first["read_through_id"])
        assert ids(echo) == [echo_id], ("本机回执没有被当成对话窗口的起点", ids(echo))
        assert chat.row(SESSION)["dialogue_until"] > time.time(), (
            "issue #217：本机发出回复之后窗口没有重新起算")
        chat_id = inject(chat, "哈哈哈哈", 3)
        after = wait(chat, grant, acknowledge_through_id=echo["read_through_id"])
        assert ids(after) == [chat_id], ("回复之后的闲聊没有投递", ids(after))
    finally:
        chat.receiver.stop()


def case_turns_budget_closes_window():
    """轮次预算扣完就收窗：下一批闲聊不再投递（窗口不是永久放行）。"""
    chat, access = build("turns")
    grant = grant_of(access)
    budget = dict(followup_turns=1, followup_seconds=180)
    try:
        at_id = inject(chat, "有人吗", 1, at_me=True)
        first = wait(chat, grant, **budget)
        assert ids(first) == [at_id], (ids(first), first.get("event"))
        chat_id = inject(chat, "哈哈哈哈", 2)
        second = wait(chat, grant, acknowledge_through_id=first["read_through_id"], **budget)
        assert ids(second) == [chat_id], ("轮次预算内的接话没投递", ids(second))
        assert int(chat.row(SESSION)["dialogue_turns"]) == 0, "轮次预算没有扣"
        late_id = inject(chat, "后来的闲聊", 3)
        third = wait(chat, grant, acknowledge_through_id=second["read_through_id"], **budget)
        assert not third.get("messages"), ("轮次扣完了窗口还开着", ids(third))
        assert inbox(access)[-1] == (late_id, 0), "轮次扣完后的跳批又把行记成已投递"
    finally:
        chat.receiver.stop()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 72)
    print("        [测试] issue #217 mention_only 对话窗口：跨调用 / 跨进程 / 跳批口径")
    print("=" * 72)
    cases = [
        ("同进程两次 wait 之间窗口存活", case_window_survives_between_calls),
        ("服务重启（新实例）后窗口仍存活", case_window_survives_restart),
        ("跳批只推游标、不置 delivered", case_skip_keeps_delivered_zero),
        ("本机回复重新起算窗口", case_own_reply_reanchors),
        ("轮次预算扣完即收窗", case_turns_budget_closes_window),
    ]
    for label, fn in cases:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001  收集全部红签名，不停在第一个
            print(f"✗ {label}：{type(exc).__name__}: {exc}")
            FAILURES.append(label)
        else:
            print(f"✓ {label}")
    print()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} 项未通过 —— {', '.join(FAILURES)}")
        return 1
    print(f"PASS: mention_only 对话窗口（{len(cases)} 项全过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
