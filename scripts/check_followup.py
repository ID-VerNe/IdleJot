"""钉住追问窗口：回复之后还能听见群里说什么，没人接话就收回。

`mention_only` 让闲聊进不了 `wg_events.jsonl`，而追问要听的恰恰是闲聊——
所以窗口只能读 `chat_inbox`。这里用一份临时库固定：开窗参数、只看自己会话、
自己的话和系统提示推进游标但不唤醒、攒满预算 / 静默超时 / 换会话三种收窗理由，
以及 reply.py 回闲聊时要从库里拿群号和消息编号的兜底。

issue #29 的两条回归也在这里：回复发出前的积压（id 在锚点之后、at 在发送开始
之前）不能算成接话、不能清零预算；窗口之后超过 50 条时不能被 LIMIT 静默跳过。

issue #70：`#` 指令（slash_commands.prefix 开头）不算接话——不扣额度、不唤醒、
不续期，但游标必须照常推进；前缀是配置项，不是写死的 `#`。

issue #98：额度扣到 0 的那一批仍然是有效唤醒（消息要送到），但窗口必须**当场**
收掉，报告改口成「本批 N 条，窗口到此为止」，不再出现「额度还剩 0 条」。

    .venv/Scripts/python.exe scripts/check_followup.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="wg_followup_")
DB = Path(TMP) / "fixture.sqlite3"
SID = "session-fixture"
WATCH = ROOT / "water-group" / "daemon" / "watch.py"

# 两个旧名都要钉：state_dir()/DB 先读 IDLEJOT_*，只设 TULPA_* 时外面若已有
# IDLEJOT_* 就会落回实机状态——本文件会 close() 窗口，等于删掉真实窗口文件。
os.environ["TULPA_WG_DB"] = str(DB)
os.environ["IDLEJOT_WG_DB"] = str(DB)
os.environ["TULPA_WG_STATE"] = TMP
os.environ["IDLEJOT_WG_STATE"] = TMP

sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

import followup  # noqa: E402
import up  # noqa: E402  (`#` 指令闸门与 wake_reason 同源，issue #70)

# 5 是被回复的那条；6/9 是群友接话；7 是机器人自己；8 是系统提示；
# 10 属于另一个会话；11 是稍后机器人自己补的一句。第三个字段是入库时刻 at。
MESSAGES = [
    (5, {"sender": "甲", "content": "@机器人 在吗", "timestamp": 1792500000000}, 1792500000.0),
    (6, {"sender": "乙", "content": "在的在的", "timestamp": 1792500001000}, 1792500001.0),
    (7, {"sender": "小鲸鱼", "content": "在呢", "timestamp": 1792500002000,
         "is_self": True}, 1792500002.0),
    (8, {"sender": "系统", "content": "提示", "timestamp": 1792500003000,
         "synthetic": True}, 1792500003.0),
    (9, {"sender": "丙", "content": "问作业吗", "timestamp": 1792500004000}, 1792500004.0),
    (11, {"sender": "小鲸鱼", "content": "刚那句当我没说", "timestamp": 1792500006000,
          "is_self": True}, 1792500006.0),
]
FOREIGN = (10, {"sender": "外人", "content": "别的会话", "timestamp": 1792500005000},
           1792500005.0)
GROUP = "123"
ONEBOT = {5: "775", 6: "776", 7: "777", 8: "778", 9: "779", 10: "7710", 11: "7711"}


def append(rows):
    """往临时库里追加消息：(id, payload, at)，at 是入库时刻。"""
    db = sqlite3.connect(DB)
    for event_id, payload, at in rows:
        data = dict(payload)
        data["conversation_id"] = f"111:group:{GROUP}"
        data["onebot_message_id"] = ONEBOT.get(event_id, str(7000 + event_id))
        db.execute("INSERT INTO chat_inbox VALUES (?, ?, ?, ?, '[]')",
                   (event_id, SID, json.dumps(data, ensure_ascii=False), at))
    db.commit()
    db.close()


def build():
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    db.execute("CREATE TABLE chat_inbox "
               "(id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT, at REAL NOT NULL,"
               " media TEXT NOT NULL DEFAULT '[]')")
    db.execute("INSERT INTO chat_sessions VALUES (?, ?, 1)", (SID, f"111:group:{GROUP}"))
    db.execute("INSERT INTO chat_sessions VALUES ('dead-session', '100:group:999', 0)")
    foreign = dict(FOREIGN[1])
    foreign["conversation_id"] = "100:group:999"
    foreign["onebot_message_id"] = ONEBOT[FOREIGN[0]]
    db.execute("INSERT INTO chat_inbox VALUES (?, ?, ?, ?, '[]')",
               (FOREIGN[0], "dead-session", json.dumps(foreign, ensure_ascii=False), FOREIGN[2]))
    db.commit()
    db.close()
    append(MESSAGES)


def check_open():
    state = followup.open(5, max_messages=4, idle_seconds=60)
    assert state["after_id"] == 5, state
    assert state["session"] == SID, state
    assert state["budget"] == 4, state
    assert 59 <= state["until"] - state["opened_at"] <= 61, state
    assert followup.load() == state, "状态没落盘"
    # 不传参数时跟 config 的 followup 段走
    defaults = followup.open(5)
    assert defaults["budget"] == followup.settings()["max_messages"], defaults
    assert defaults["idle_seconds"] == followup.settings()["idle_seconds"], defaults
    print("  ok  开窗参数与默认值（config followup 段）")


def check_since():
    followup.open(5, max_messages=4, idle_seconds=60)
    rows = followup.since(5, SID)
    assert [r["id"] for r in rows] == [6, 7, 8, 9, 11], [r["id"] for r in rows]
    assert all(r["sender"] and r["text"] for r in rows), "sender/text 没取出来"
    # 别的会话的消息（id=10）不能混进来
    assert all(r["id"] != 10 for r in rows), "混进了别的会话"
    print("  ok  窗口之后的消息按 id 涨序、不跨会话")


def check_consume():
    state = followup.open(5, max_messages=4, idle_seconds=60)
    woke = followup.consume(state, followup.since(5, SID))
    assert [r["id"] for r in woke] == [6, 9], "自己说的和系统提示不该唤醒"
    assert state["after_id"] == 11, "游标要推过所有行，包括自己的话"
    assert state["budget"] == 2, state
    assert state["wakes"] == 1, state
    assert state["until"] > state["opened_at"] + 60, "有人接话要续期"
    assert followup.consume(state, followup.since(11, SID)) == [], "没有新消息时不该再唤醒"
    assert state["budget"] == 2, "空批次不该动额度"
    print("  ok  接话才唤醒、才续期；自己的话只推游标")


def check_budget():
    state = followup.open(5, max_messages=2, idle_seconds=600)
    followup.consume(state, followup.since(5, SID))
    assert state["budget"] == 0, state
    assert followup.stale(state) == "budget", followup.stale(state)
    # issue #98：额度用完必须当场收窗，不能留到下一轮 stale() 才生效
    assert followup.load() is None, "额度扣到 0 之后窗口文件还在（issue #98）"
    # 一批远超额度时钳在 0，不透支成负数
    tight = followup.open(5, max_messages=1, idle_seconds=600)
    followup.consume(tight, followup.since(5, SID))
    assert tight["budget"] == 0, tight
    assert followup.load() is None, "超额那批也要当场收窗"
    print("  ok  接满预算就收窗（含超额钳零），当场生效（issue #98）")


def check_budget_report():
    """issue #98：用完的那批照旧唤醒，但报告不能说「还剩 0 条」。"""
    anchor = 500
    append([
        (501, {"sender": "甲", "content": "接着说", "timestamp": 1792500300000}, 1792500300.0),
        (502, {"sender": "乙", "content": "嗯", "timestamp": 1792500301000}, 1792500301.0),
    ])
    # 没扣完：照旧报剩余额度，窗口留着等下一句
    state = followup.open(anchor, max_messages=5, idle_seconds=600)
    woke = followup.consume(state, followup.since(anchor, SID))
    assert len(woke) == 2, woke
    assert state["budget"] == 3, state
    assert followup.load() is not None, "还有额度，窗口不该收"
    line = followup.wake_report(state, len(woke))
    assert "额度还剩 3 条" in line and "静默 600s 后收回" in line, line

    # 正好扣完：窗口当场消失，报告改口
    state = followup.open(anchor, max_messages=2, idle_seconds=600)
    woke = followup.consume(state, followup.since(anchor, SID))
    assert len(woke) == 2, "用完的那批消息必须照常送出，不能吞"
    assert state["budget"] == 0, state
    assert followup.load() is None, "额度用完的那一批必须当场收窗（issue #98）"
    line = followup.wake_report(state, len(woke))
    assert "还剩 0 条" not in line, f"额度已是 0 还在报剩余额度：{line}"
    assert "本批 2 条" in line and "到此为止" in line, line
    assert followup.stale(state) == "budget", "兜底分支要留着（旧状态文件/手改状态）"
    print("  ok  用完的那批照旧唤醒，报告改成「本批 N 条，到此为止」（issue #98）")


def check_idle():
    state = followup.open(5, idle_seconds=1)
    assert followup.stale(state) is None, followup.stale(state)
    time.sleep(1.1)
    assert followup.stale(state) == "idle", followup.stale(state)
    print("  ok  静默超时就收窗")


def check_session():
    state = followup.open(5, idle_seconds=600)
    state["session"] = "dead-session"
    followup.save(state)
    assert followup.stale(state) == "session", followup.stale(state)
    print("  ok  换会话即收窗（防 after_id 对错号）")


def check_lookup():
    followup.close()
    assert followup.load() is None, "close 之后不该还有窗口"
    assert followup.lookup(6) == (GROUP, "776"), followup.lookup(6)
    assert followup.lookup(11) == (GROUP, "7711"), followup.lookup(11)
    assert followup.lookup(9999) is None, "查无此事"
    assert followup.latest_group_id() == GROUP, followup.latest_group_id()
    print("  ok  close/开窗文件，lookup 与群号兜底")


def check_backlog():
    """issue #29：回复发出前的积压不算接话，也不能把预算清零、开窗即收窗。"""
    anchor = 11
    sent_at = time.time()
    # 积压的 id 都大于被回复的那条，但 at 早于开始发送的时刻。
    append([(i, {"sender": "群友", "content": f"积压 {i}",
                 "timestamp": int((sent_at - 30) * 1000)}, sent_at - 30 + i * 0.1)
            for i in range(20, 36)])
    state = followup.open(anchor, max_messages=10, idle_seconds=600, not_before=sent_at)
    rows = followup.since(anchor, SID, state["not_before"])
    assert rows == [], f"发送前的积压混进了窗口：{[r['id'] for r in rows]}"
    assert followup.consume(state, rows) == [], "积压不该唤醒"
    assert state["budget"] == 10, state
    assert state["after_id"] == anchor, state
    assert followup.stale(state) is None, followup.stale(state)
    # 开窗之后真有人接话，才该唤醒、才该扣额度
    append([(40, {"sender": "群友", "content": "回复之后说的",
                  "timestamp": int(sent_at * 1000)}, sent_at + 5)])
    woke = followup.consume(state, followup.since(state["after_id"], SID,
                                                  state["not_before"]))
    assert [r["id"] for r in woke] == [40], woke
    assert state["budget"] == 9, state
    print("  ok  回复前的积压不算接话、不清零预算（issue #29）")


def check_pagination():
    """issue #29：窗口之后超过 LIMIT 的消息不能被静默跳过。"""
    anchor = 100
    sent_at = time.time()
    ids = list(range(110, 170))  # 60 条，超过 since() 的默认每页 50 条
    append([(i, {"sender": "群友", "content": f"接话 {i}",
                 "timestamp": int(sent_at * 1000)}, sent_at + i * 0.001) for i in ids])
    rows = followup.since(anchor, SID, sent_at)
    assert [r["id"] for r in rows] == ids, f"分页漏了：{[r['id'] for r in rows]}"
    state = followup.open(anchor, max_messages=100, idle_seconds=600, not_before=sent_at)
    woke = followup.consume(state, followup.since(anchor, SID, state["not_before"]))
    assert [r["id"] for r in woke] == ids, "consume 吞掉了消息"
    assert state["after_id"] == ids[-1], state
    assert state["budget"] == 40, state
    assert followup.stale(state) is None, followup.stale(state)
    print("  ok  超过 50 条也一条不落（分页读完）")


def check_command_gate():
    """issue #70：`#` 指令不算接话，但游标要越过它；前缀读配置。"""
    real = up.config
    up.config = lambda: {"slash_commands": {"prefix": "#"},
                         "self_account": "999", "self_names": ["小鲸鱼"]}
    try:
        # 一批里混排：两条指令 + 一条真接话
        append([
            (200, {"sender": "甲", "content": "#ping",
                   "timestamp": 1792500100000}, 1792500100.0),
            (201, {"sender": "乙", "content": "#帮助 小鲸鱼",
                   "timestamp": 1792500101000}, 1792500101.0),
            (202, {"sender": "丙", "content": "正常接话",
                   "timestamp": 1792500102000}, 1792500102.0),
        ])
        state = followup.open(199, max_messages=10, idle_seconds=600)
        woke = followup.consume(state, followup.since(199, SID))
        assert [r["id"] for r in woke] == [202], [r["id"] for r in woke]
        assert state["budget"] == 9, state
        assert state["wakes"] == 1, state
        assert state["after_id"] == 202, state
        until = state["until"]

        # 整批都是指令：不唤醒、不扣额度、不续期，但游标推进且落盘
        append([
            (203, {"sender": "甲", "content": "#天气",
                   "timestamp": 1792500103000}, 1792500103.0),
            (204, {"sender": "乙", "content": "#简报",
                   "timestamp": 1792500104000}, 1792500104.0),
        ])
        woke = followup.consume(state, followup.since(state["after_id"], SID))
        assert woke == [], woke
        assert state["budget"] == 9 and state["wakes"] == 1, state
        assert state["until"] == until, "指令不该给窗口续期"
        assert state["after_id"] == 204, state
        saved = followup.load()
        assert saved and saved["after_id"] == 204, "游标没落盘，下一轮会重捞"
        assert followup.since(204, SID) == [], "跳过的指令没被游标越过"
        assert followup.stale(state) is None, followup.stale(state)

        # 行首空白：text 被洗成 "#ping"，raw 不会；守护也不认它，所以算接话
        append([(205, {"sender": "丙", "content": " #ping",
                       "timestamp": 1792500105000}, 1792500105.0)])
        woke = followup.consume(state, followup.since(204, SID))
        assert [r["id"] for r in woke] == [205], woke

        # wake_reason：指令闸门在关键词之前；@ 仍然优先
        assert up.wake_reason({"content": "#帮助 小鲸鱼"}) == ""
        assert up.wake_reason({"content": "小鲸鱼 在吗"}) == "keyword:小鲸鱼"
        assert up.wake_reason({"content": "#ping",
                               "segments": [{"type": "at", "qq": "999"}]}) == "at"

        # 前缀是配置项：换成 ! 之后 #ping 是闲聊、!ping 才是指令
        up.config = lambda: {"slash_commands": {"prefix": "!"},
                             "self_account": "999", "self_names": ["小鲸鱼"]}
        append([
            (206, {"sender": "甲", "content": "#ping",
                   "timestamp": 1792500106000}, 1792500106.0),
            (207, {"sender": "乙", "content": "!ping",
                   "timestamp": 1792500107000}, 1792500107.0),
        ])
        woke = followup.consume(state, followup.since(205, SID))
        assert [r["id"] for r in woke] == [206], woke
        assert state["budget"] == 7, state
        assert state["after_id"] == 207, state
        assert followup.since(207, SID) == []
    finally:
        up.config = real
    print("  ok  `#` 指令不占额度、不续期，但推进游标；前缀读配置（issue #70）")


def check_watch_budget_wake():
    """issue #98 的症状级回归：真跑 watch.py，用完那轮不许打「还剩 0 条」。

    followup 单测只能证明状态对了，症状在 watch.py 的打印分支上：旧代码只看
    `woke` 非空就 WAKE，报告行直接读 budget，于是额度 0 也照样喊一次。
    """
    anchor = 600
    append([(601, {"sender": "丙", "content": "最后一句",
                   "timestamp": 1792500400000}, 1792500400.0)])
    followup.open(anchor, max_messages=1, idle_seconds=600)
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    for name in ("WATER_GROUP_HOME", "IDLEJOT_WG_STATE", "TULPA_WG_STATE",
                 "IDLEJOT_WG_DB", "TULPA_WG_DB"):
        env.pop(name, None)
    env["IDLEJOT_WG_STATE"] = TMP
    env["TULPA_WG_STATE"] = TMP
    env["IDLEJOT_WG_DB"] = str(DB)
    env["TULPA_WG_DB"] = str(DB)
    result = subprocess.run([sys.executable, str(WATCH), "--timeout", "5"],
                            cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", timeout=120)
    out = result.stdout or ""
    assert result.returncode == 0, f"watch.py 退出码 {result.returncode}：\n{out}"
    assert "WAKE" in out, f"用完的那批没唤醒，消息被吞了：\n{out}"
    assert "还剩 0 条" not in out, f"额度用尽仍在报剩余额度（issue #98 复发）：\n{out}"
    assert "本批 1 条" in out and "到此为止" in out, out
    assert followup.load() is None, "watch 跑完窗口文件还在：用完没当场收窗"
    print("  ok  watch.py 用完那轮不再报「还剩 0 条」（issue #98）")


def main():
    build()
    print("追问窗口回归：")
    check_open()
    check_since()
    check_consume()
    check_budget()
    check_idle()
    check_session()
    check_lookup()
    check_backlog()
    check_pagination()
    check_command_gate()
    # 这两条自带新消息（id 500+），必须排在所有 `since(小 anchor)` 用例之后，
    # 否则会被别的用例当成接话捞进去。
    check_budget_report()
    check_watch_budget_wake()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
