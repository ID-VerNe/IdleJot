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

issue #4 PD2a：追问窗口插件化之后的新契约——``reply_sent`` 三态门控（停用不开
窗、群白名单按群放行、群级关闭优先）、``wake_scan`` 停用即不唤醒（本节点唯一的
行为变化：改前 ``enabled:false`` 只挡开窗）、``event_lookup`` / ``active_group``
两个 DB 兜底事件**不受门控**（关了窗口也要能回 ``--context`` 里看到的闲聊）、
``window_status`` 过门控（插件行与追问行口径一致）、顶层 ``followup`` 旧段经
``legacy_section`` 逐字段等效、``plugins.emit`` 与 commands 注册表派发到同一个
PLUGIN 对象。

issue #138：**只 @ 了别人、正文空白**的消息不算接话——不唤醒、不扣额度、不续期，
但游标照常推进；``@我 + 空白`` 照旧唤醒，纯图片照旧唤醒（两组对照）。判据取入库
的 ``segments``（``followup.mention_info``），没有段清单的老行退化成从 ``content``
认 ``@<qq>``。同一 issue 的第二半在渲染侧：追问行不许把 ``@<qq>`` 抹成裸 ``@``。

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
import wg_text  # noqa: E402  (追问行正文渲染，issue #138)
import wg_up_wake  # noqa: E402  (issue #37 PC: config 的真实调用点；up.config 只是门面引用)

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
    """issue #29/#117：回复发出前的积压照常返回、照常唤醒，但不占预算。

    #29 的核心不变量——积压不清零预算、不开窗即收窗——保留。翻面的是「可见
    性」：以前 since() 在 SQL 层用 `at>=?` 把 pre-reply 行整个滤掉（issue #117
    的根因——它们既不唤醒也不推进游标，下一轮仍在 SQL 层挡掉，永久丢）；现在
    照常返回、照常唤醒，预算只扣真正的接话。
    """
    anchor = 11
    sent_at = time.time()
    # 积压的 id 都大于被回复的那条，但 at 早于开始发送的时刻。
    append([(i, {"sender": "群友", "content": f"积压 {i}",
                 "timestamp": int((sent_at - 30) * 1000)}, sent_at - 30 + i * 0.1)
            for i in range(20, 36)])
    state = followup.open(anchor, max_messages=10, idle_seconds=600, not_before=sent_at)
    # pre-reply 积压必须照常返回——不能再在 SQL 层过滤（issue #117）
    rows = followup.since(anchor, SID, state["not_before"])
    assert [r["id"] for r in rows] == list(range(20, 36)), \
        f"pre-reply 积压被过滤掉了（issue #117 复发）：{[r['id'] for r in rows]}"
    woke = followup.consume(state, rows)
    assert [r["id"] for r in woke] == list(range(20, 36)), "积压该唤醒，不能吞"
    assert state["budget"] == 10, "pre-reply 不占预算（issue #29 不变量）"
    assert state["after_id"] == 35, "游标要越过积压，不能卡在锚点"
    assert state["wakes"] == 1, state
    assert followup.stale(state) is None, followup.stale(state)
    # 开窗之后真有人接话，才该扣额度
    append([(40, {"sender": "群友", "content": "回复之后说的",
                  "timestamp": int(sent_at * 1000)}, sent_at + 5)])
    woke = followup.consume(state, followup.since(state["after_id"], SID,
                                                  state["not_before"]))
    assert [r["id"] for r in woke] == [40], woke
    assert state["budget"] == 9, "post-reply 才占预算"
    print("  ok  回复前的积压照常唤醒但不占预算（#29 不变量 + #117 可见性）")


def check_pre_reply_visible():
    """issue #117 专项：Agent 处理回复期间群友发的消息，追问窗口必须看得见。

    复现 issue 的时间线：开窗的 not_before 落在「发送开始」，但更早的若干条已经
    在库里（at < not_before）——正是 Agent 从拿到唤醒到把回复发出去那段积压。
    旧 since() 在 SQL 层用 `at>=?` 过滤，它们从查询结果里消失，consume() 的游标
    也不推进——永久丢。新语义：照常返回、照常唤醒、不占预算（pre-reply 是处理
    期间漏掉的，不该再算一次额度）。
    """
    anchor = 800
    sent_at = time.time()
    # 处理期间群友连发 3 条：id 在锚点之后、at 在发送开始之前
    append([(i, {"sender": "群友", "content": f"处理期间 {i}",
                 "timestamp": int((sent_at - 10) * 1000)}, sent_at - 10 + (i - 801) * 0.1)
            for i in range(801, 804)])
    state = followup.open(anchor, max_messages=5, idle_seconds=600, not_before=sent_at)
    # since() 必须把 pre-reply 行返回——不再在 SQL 层过滤
    rows = followup.since(anchor, SID, state["not_before"])
    assert [r["id"] for r in rows] == [801, 802, 803], \
        f"pre-reply 行被过滤掉了（issue #117 复发）：{[r['id'] for r in rows]}"
    # consume() 必须把它们列入 woke——Agent 才看得见处理期间漏掉的消息（含图片）
    woke = followup.consume(state, rows)
    assert [r["id"] for r in woke] == [801, 802, 803], "pre-reply 该唤醒，不能吞"
    assert state["after_id"] == 803, "游标要越过 pre-reply"
    assert state["wakes"] == 1, state
    # 但 pre-reply 不占预算——它们是处理期间漏掉的，不是真正的接话
    assert state["budget"] == 5, "pre-reply 不该扣额度（issue #117）"
    assert followup.stale(state) is None, followup.stale(state)

    # 发送开始之后的接话才占预算
    append([(810, {"sender": "群友", "content": "回复之后说的",
                   "timestamp": int(sent_at * 1000)}, sent_at + 5)])
    woke = followup.consume(state, followup.since(state["after_id"], SID,
                                                  state["not_before"]))
    assert [r["id"] for r in woke] == [810], woke
    assert state["budget"] == 4, "post-reply 才扣额度"
    followup.close()
    print("  ok  处理期间的消息照常返回、照常唤醒、不占预算（issue #117）")


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
    real = wg_up_wake.config
    wg_up_wake.config = lambda: {"slash_commands": {"prefix": "#"},
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
        wg_up_wake.config = lambda: {"slash_commands": {"prefix": "!"},
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
        wg_up_wake.config = real
    print("  ok  `#` 指令不占额度、不续期，但推进游标；前缀读配置（issue #70）")


def run_watch():
    """跑一次 `watch.py --timeout 5`，返回 (returncode, 合并后的输出)。

    #51 之后 watch.py 启动就要 self_account；config.json 被 gitignore，CI 干净
    checkout 没有它——写份最小配置用 IDLEJOT_WG_CONFIG 指过去（只进子进程 env，
    不动本进程 environ：本文件其他用例对 config 有自己的桩）。
    """
    fixture = Path(TMP) / "watch-fixture-config.json"
    fixture.write_text(json.dumps({"self_account": "10001",
                                   "onebot_token": "followup-fixture"},
                                  ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    for name in ("WATER_GROUP_HOME", "IDLEJOT_WG_STATE", "TULPA_WG_STATE",
                 "IDLEJOT_WG_DB", "TULPA_WG_DB"):
        env.pop(name, None)
    env["IDLEJOT_WG_STATE"] = TMP
    env["TULPA_WG_STATE"] = TMP
    env["IDLEJOT_WG_DB"] = str(DB)
    env["TULPA_WG_DB"] = str(DB)
    env["IDLEJOT_WG_CONFIG"] = str(fixture)
    result = subprocess.run([sys.executable, str(WATCH), "--timeout", "5"],
                            cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", timeout=120)
    return result.returncode, result.stdout or ""


def check_watch_budget_wake():
    """issue #98 的症状级回归：真跑 watch.py，用完那轮不许打「还剩 0 条」。

    followup 单测只能证明状态对了，症状在 watch.py 的打印分支上：旧代码只看
    `woke` 非空就 WAKE，报告行直接读 budget，于是额度 0 也照样喊一次。
    """
    anchor = 600
    append([(601, {"sender": "丙", "content": "最后一句",
                   "timestamp": 1792500400000}, 1792500400.0)])
    followup.open(anchor, max_messages=1, idle_seconds=600)
    code, out = run_watch()
    assert code == 0, f"watch.py 退出码 {code}：\n{out}"
    assert "WAKE" in out, f"用完的那批没唤醒，消息被吞了：\n{out}"
    assert "还剩 0 条" not in out, f"额度用尽仍在报剩余额度（issue #98 复发）：\n{out}"
    assert "本批 1 条" in out and "到此为止" in out, out
    assert followup.load() is None, "watch 跑完窗口文件还在：用完没当场收窗"
    print("  ok  watch.py 用完那轮不再报「还剩 0 条」（issue #98）")


# ---------- issue #4 PD2a：插件化之后的新契约 ----------
# 以下用例都追加 id 700+ 的新消息：既不被前面任何 `since(小 anchor)` 用例捞到，
# 也不给 watch 子进程测试的「本批 1 条」添行。

def check_plugin_gating():
    """reply_sent 过三态门控：停用不开窗；群白名单按群放行；群级关闭优先。"""
    import commands
    import plugins

    def reply_sent(settings, group_id):
        return plugins.emit("reply_sent", {
            "settings": settings,
            "group_id": group_id,
            "event_id": 5,
            "not_before": None,
        })

    # 默认（什么都没配）：开，参数走机件默认值
    followup.close()
    state = reply_sent(commands.settings_from({}), "123")
    assert state and state["budget"] == 10 and state["idle_seconds"] == 180, state
    assert followup.load() == state, "开窗没落盘"

    # 总开关关：不开
    off = commands.settings_from({"plugins": {"followup": {"enabled": False}}})
    followup.close()
    assert reply_sent(off, "123") is None, "停用不该开窗"
    assert followup.load() is None

    # 群白名单：只在这些群开
    scoped = commands.settings_from({"plugins": {"followup": {
        "enabled": True, "groups": ["123"]}}})
    followup.close()
    assert reply_sent(scoped, "123"), "白名单内的群该开窗"
    followup.close()
    assert reply_sent(scoped, "999") is None, "白名单外的群不该开窗"
    assert followup.load() is None

    # 群级关闭盖过白名单
    group_off = commands.settings_from({
        "plugins": {"followup": {"enabled": True, "groups": ["123"]}},
        "groups": {"123": {"plugins": {"followup": False}}}})
    followup.close()
    assert reply_sent(group_off, "123") is None, "群级关闭优先于白名单"
    assert followup.load() is None

    # options 显式传进窗口机件
    tuned = commands.settings_from({"plugins": {"followup": {
        "enabled": True, "options": {"max_messages": 3, "idle_seconds": 60}}}})
    followup.close()
    state = reply_sent(tuned, "123")
    assert state["budget"] == 3 and state["idle_seconds"] == 60, state
    followup.close()
    print("  ok  reply_sent 三态门控 + options 透传（issue #4 PD2a）")


def check_disable_no_wake():
    """停用即降级：窗口开着也不唤醒、不关窗（本节点唯一的行为变化）。"""
    import commands
    import plugins

    followup.close()
    state = followup.open(700, max_messages=5, idle_seconds=600,
                          not_before=time.time() - 1)
    append([(701, {"sender": "群友", "content": "停用期间说的话",
                   "timestamp": 1792500500000}, time.time())])

    off = commands.settings_from({"plugins": {"followup": {"enabled": False}}})
    scan = plugins.emit("wake_scan", {"settings": off})
    assert scan is None, f"停用不该唤醒：{scan}"
    assert followup.load() == state, "停用只该不唤醒，不该顺手关窗"

    # 启用且真有新消息：唤醒，报告由插件给
    on = commands.settings_from({})
    scan = plugins.emit("wake_scan", {"settings": on})
    assert scan and scan.get("woke"), scan
    assert [r["id"] for r in scan["woke"]] == [701], scan["woke"]
    assert "群里还在聊" in scan["report"], scan["report"]

    # 过期窗口：启用态下顺手关掉并报告
    followup.close()
    stale = followup.open(702, max_messages=5, idle_seconds=600,
                          not_before=time.time() - 1)
    stale["until"] = time.time() - 1
    followup.save(stale)
    scan = plugins.emit("wake_scan", {"settings": on})
    assert scan and scan.get("closed"), scan
    assert followup.load() is None, "过期窗口要当场收掉"
    followup.close()
    print("  ok  停用即不唤醒（窗口留着），启用才唤醒；过期顺手收窗（issue #4 PD2a）")


def check_db_fallback_not_gated():
    """DB 兜底不限门控：关了窗口也要能回 --context 里看到的闲聊。"""
    import commands
    import plugins

    off = commands.settings_from({"plugins": {"followup": {"enabled": False}}})
    # event_lookup / active_group 连 settings 都不看：停用态照旧回答
    assert plugins.emit("event_lookup", {"event_id": 6, "settings": off}) == (GROUP, "776")
    assert plugins.emit("event_lookup", {"event_id": 9999, "settings": off}) is None
    assert plugins.emit("active_group", {"settings": off}) == GROUP
    # commands 注册表同一条路，答案一样
    assert commands.PLUGINS.emit("event_lookup", {"event_id": 6}) == (GROUP, "776")
    assert commands.PLUGINS.emit("active_group") == GROUP
    print("  ok  event_lookup / active_group 不受门控（回复链路 DB 兜底）")


def check_window_status():
    """window_status 过门控：插件行说停用而追问行说进行中是自相矛盾。"""
    import commands
    import plugins

    on = commands.settings_from({})
    off = commands.settings_from({"plugins": {"followup": {"enabled": False}}})
    followup.close()
    assert followup.status_text() is None, "没开窗时没有文案"
    assert plugins.emit("window_status", {"settings": on}) is None

    followup.open(5, max_messages=4, idle_seconds=600)
    text = plugins.emit("window_status", {"settings": on})
    assert text and "进行中" in text and "还可跟 4 条" in text, text
    assert plugins.emit("window_status", {"settings": off}) is None, "停用不该报进行中"
    assert commands.PLUGINS.emit("window_status", {"settings": off}) is None
    followup.close()
    print("  ok  window_status 过三态门控，文案与 up.py 追问行同源")


def check_legacy_section():
    """旧版顶层 followup 段逐字段等效（Plugin.legacy_section，PD2a）。"""
    import commands

    legacy = commands.settings_from({"followup": {
        "enabled": True, "max_messages": 5, "idle_seconds": 60}})
    new = commands.settings_from({"plugins": {"followup": {
        "enabled": True, "options": {"max_messages": 5, "idle_seconds": 60}}}})
    assert legacy["plugins"]["followup"] == new["plugins"]["followup"], (
        f"{legacy['plugins']['followup']} != {new['plugins']['followup']}")
    # 顶层段不算 plugins_legacy：那个列表的 --status 提示写死了 slash_commands.<名字>
    assert legacy["plugins_legacy"] == [], legacy["plugins_legacy"]
    # 停用也认；没配的键用机件默认
    off = commands.settings_from({"followup": {"enabled": False}})
    assert off["plugins"]["followup"]["enabled"] is False, off["plugins"]["followup"]
    assert off["plugins"]["followup"]["options"] == {}, off["plugins"]["followup"]
    print("  ok  顶层 followup 旧段逐字段等效，不计入 plugins_legacy（issue #4 PD2a）")


def check_two_dispatch_paths():
    """CLI 的 plugins.emit 与 commands 注册表的 emit 是同一个 PLUGIN 对象。"""
    import commands
    import plugins

    follow = commands.PLUGINS.get("followup")
    assert follow is not None, "followup 没注册进 commands 注册表"
    assert follow.hooks.on_event is not None, "followup 必须挂 on_event"
    assert plugins._load_followup() is follow, "两条派发路必须是同一个对象"
    for name, loader in plugins.EVENT_LOADERS:
        assert (name, loader) in plugins.LOADERS, f"{name} 只在 EVENT_LOADERS，没静态注册"
    # 注册表路的 reply_sent 同样过门控（不只是 CLI 路）
    off = commands.settings_from({"plugins": {"followup": {"enabled": False}}})
    followup.close()
    assert commands.PLUGINS.emit("reply_sent", {
        "settings": off, "group_id": "123", "event_id": 5}) is None
    assert followup.load() is None, "注册表路停用也不该开窗"
    followup.close()
    print("  ok  plugins.emit 与注册表 emit 同源，EVENT_LOADERS ⊆ LOADERS")


def check_bare_at_others():
    """issue #138：只 @ 别人、正文空白的消息不是「在聊」，不许唤醒。

    线上现场：追问窗口里收到 `[{"type":"at","qq":"<别人>"},{"type":"text","text":" "}]`，
    被当成接话唤醒，追问行又渲染成裸 `@ `，于是 Agent 答了一句「你 @ 我……」。
    两组对照不能少：`@我 + 空白` 是真空唤醒（issue 明说别一起吞），纯图片是
    #35 要保的形态——把「没有正文」判成「没有内容」就会误杀它们。
    """
    real = wg_up_wake.config
    wg_up_wake.config = lambda: {"self_account": "10001", "self_names": ["小鲸鱼"],
                                 "slash_commands": {"prefix": "#"}}
    try:
        append([
            # 1) @别人 + 空白（at 段带显示名）
            (901, {"sender": "甲", "content": "@乙 ",
                   "segments": [{"type": "at", "qq": "20002", "name": "乙"},
                                {"type": "text", "text": " "}],
                   "timestamp": 1792500600000}, 1792500600.0),
            # 2) @别人 + 空白（at 段无名字，content 就是 @<qq>）
            (902, {"sender": "甲", "content": "@20003 ",
                   "segments": [{"type": "at", "qq": "20003"},
                                {"type": "text", "text": " "}],
                   "timestamp": 1792500601000}, 1792500601.0),
            # 3) @我 + 空白：真空唤醒，照旧算接话
            (903, {"sender": "丙", "content": "@小鲸鱼 ",
                   "segments": [{"type": "at", "qq": "10001", "name": "小鲸鱼"},
                                {"type": "text", "text": " "}],
                   "timestamp": 1792500602000}, 1792500602.0),
            # 4) 对照组：@别人 + 正文，照旧算接话
            (904, {"sender": "丁", "content": "@乙 这题你会吗",
                   "segments": [{"type": "at", "qq": "20002", "name": "乙"},
                                {"type": "text", "text": " 这题你会吗"}],
                   "timestamp": 1792500603000}, 1792500603.0),
            # 5) 对照组：纯图片，没有正文也没有 @（issue #35）
            (905, {"sender": "戊", "content": "[图片]",
                   "segments": [{"type": "image", "image_index": 0}],
                   "timestamp": 1792500604000}, 1792500604.0),
        ])
        state = followup.open(900, max_messages=10, idle_seconds=600)
        until = state["until"]
        rows = followup.since(900, SID)
        assert [r["id"] for r in rows] == [901, 902, 903, 904, 905], \
            [r["id"] for r in rows]
        woke = followup.consume(state, rows)
        assert [r["id"] for r in woke] == [903, 904, 905], (
            "只 @ 别人、没有正文的消息被当成接话（issue #138 复发）："
            f"{[r['id'] for r in woke]}")
        assert state["after_id"] == 905, "跳过的消息也必须推游标（跳过 ≠ 卡住）"
        assert state["budget"] == 7, f"被跳过的两条不该扣额度：{state['budget']}"
        assert state["wakes"] == 1, state
        assert state["until"] > until, "有真接话，窗口该续期"
        assert followup.load()["after_id"] == 905, "游标没落盘，下一轮会重捞"

        # 整批都是「只 @ 别人」：不唤醒、不扣额度、不续期，但游标照常推进
        append([(906, {"sender": "己", "content": "@乙 ",
                       "segments": [{"type": "at", "qq": "20002", "name": "乙"},
                                    {"type": "text", "text": " "}],
                       "timestamp": 1792500605000}, 1792500605.0)])
        frozen = (state["budget"], state["wakes"], state["until"])
        assert followup.consume(state, followup.since(state["after_id"], SID)) == [], \
            "整批都是「只 @ 别人」，不该唤醒"
        assert (state["budget"], state["wakes"], state["until"]) == frozen, state
        assert state["after_id"] == 906, "跳过的消息也必须推游标（跳过 ≠ 卡住）"
        assert followup.load()["after_id"] == 906, "游标没落盘，下一轮会重捞"

        # 判据本身：@ 指向、可见正文、有没有内容，逐项对得上
        assert rows[0].get("segments"), "since() 没把 segments 带出来，判不了 @ 指向"
        assert followup.mention_info("@乙 ", rows[0]["segments"], "10001") == \
            ("", False, ["乙"], False), "带名字的 @别人 + 空白 没判出来"
        assert followup.mention_info("@20003 ", rows[1]["segments"], "10001") == \
            ("", False, ["20003"], False), "无名字的 @别人 + 空白 没判出来"
        assert followup.mention_info("@小鲸鱼 ", rows[2]["segments"], "10001")[1] is True, \
            "@我 没认出来"
        assert followup.mention_info("@乙 这题你会吗", rows[3]["segments"], "10001") == \
            ("这题你会吗", False, ["乙"], True), "带正文的 @ 不该算空"
        assert followup.mention_info("[图片]", rows[4]["segments"], "10001") == \
            ("", False, [], True), "纯图片被当成空消息"
        # 没有 segments 的老行/超限行：退化成从 content 认 @<qq>
        assert followup.mention_info("@20002 ", None, "10001") == ("", False, ["20002"], False)
        assert followup.mention_info("@20002 在吗", None, "10001") == ("在吗", False, ["20002"], True)

        # issue 的复现命令就是手搓行（没有 segments）：照样不许唤醒
        manual = [{"id": 910, "sender": "甲", "raw": "@20002 ", "text": "@20002 ",
                   "at": time.time(), "self": False, "synthetic": False}]
        assert followup.consume(state, manual) == [], \
            "手搓行（issue 复现命令的形态）仍然被唤醒（issue #138 复发）"
        assert state["after_id"] == 910, state

        # P1-b：追问行不许把 @ 目标抹成裸 `@`
        line, _, _ = wg_text.render_body("@20002 ", wg_text.WAKE_BODY_CHARS, 902)
        assert "20002" in line and line.strip() != "@", \
            f"@ 指向被渲染成裸 `@`（issue #138 复发）：{line!r}"
        named, _, _ = wg_text.render_body("@乙 ", wg_text.WAKE_BODY_CHARS, 901)
        assert named.strip() == "@乙", named
    finally:
        wg_up_wake.config = real
    print("  ok  只 @ 别人、正文空白不唤醒（游标照推）；@我 / 带正文 / 纯图片照旧（issue #138）")


def check_bare_at_others_watch():
    """症状级：issue #138 的现场是 watch.py 打出来的那一行。

    窗口里只放两条：@别人 + 空白、@我 + 空白。前者不许出现在输出里，后者照旧
    WAKE——只改状态不改打印，Agent 看到的仍然是那一行。
    """
    anchor = 950
    now = time.time()
    append([
        (951, {"sender": "甲", "content": "@乙 ",
               "segments": [{"type": "at", "qq": "20002", "name": "乙"},
                            {"type": "text", "text": " "}],
               "timestamp": 1792500700000}, now),
        (952, {"sender": "丙", "content": "@小鲸鱼 ",
               "segments": [{"type": "at", "qq": "10001", "name": "小鲸鱼"},
                            {"type": "text", "text": " "}],
               "timestamp": 1792500701000}, now),
    ])
    followup.open(anchor, max_messages=5, idle_seconds=600)
    code, out = run_watch()
    assert code == 0, f"watch.py 退出码 {code}：\n{out}"
    assert "WAKE" in out, f"@我 的空白消息该唤醒，不能连坐：\n{out}"
    assert "id=951" not in out, \
        f"只 @ 别人的空白消息进了追问行（issue #138 复发）：\n{out}"
    assert "id=952" in out, f"@我 的那条没进追问行：\n{out}"
    followup.close()
    print("  ok  watch.py 追问行只放真接话：@别人+空白不出现，@我+空白照旧（issue #138）")


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
    # PD2a 用例追加 id 700+ 的新消息，排在 watch 子进程测试之后
    check_plugin_gating()
    check_disable_no_wake()
    # issue #117 专项：追加 id 800+ 的新消息，必须排在所有 `since(小 anchor)`
    # 用例（含 check_command_gate 的 since(199..207)、check_disable_no_wake 的
    # since(700)）之后——否则 800+ 会被它们当成接话捞进去，断言翻车。
    check_pre_reply_visible()
    check_db_fallback_not_gated()
    check_window_status()
    check_legacy_section()
    check_two_dispatch_paths()
    # issue #138 专项：追加 id 900+ / 950+ 的新消息，必须排在所有 `since(小 anchor)`
    # 用例之后——否则会被它们当成接话捞进去。
    check_bare_at_others()
    check_bare_at_others_watch()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
