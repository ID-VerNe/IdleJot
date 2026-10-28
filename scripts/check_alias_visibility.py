"""issue #30 gate: 正文里的外号必须能指到人，认不出也不许编。

写侧落得进去（档案 aliases 一直有数据），读侧不接：唤醒行只印发送者的 QQ 群
名片，正文里出现的外号没有任何指向信息，Agent 判不出「这是谁」，而唯一的反查
路径（先知道外号属于谁）构成循环依赖。

本门禁钉住读侧渲染：

  1. 正文含已知外号 → 必须给出指向（`↪ 外号「X」= 主名(QQ)`）；
  2. 未知称呼 / 通用词（「领导」「群友」）→ 不长出指向，不编；
  3. 发送者对得上档案 → 附别名锚；对不上 → 原样；
  4. 群覆盖行里的外号也要能指（issue #5 并集视图）；
  5. 记忆引擎故障 → 静默降级，渲染链路不炸；
  6. `reply.py --context` 端到端输出含指向。

    .venv/Scripts/python.exe scripts/check_alias_visibility.py
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

import check_fixture_support as fs  # noqa: E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import scratch_dir  # noqa: E402
ROOT = fs.bootstrap_paths()

# 记忆库必须在 import 任何 daemon 模块之前钉走：memory_store 的路径是 import
# 期求值的（与 check_sixty 同一理由）。
_SCRATCH = scratch_dir(prefix="wg_alias_")
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
os.environ["IDLEJOT_WG_MEMORY_DB"] = str(_SCRATCH / "mem.sqlite3")
(_SCRATCH / "home").mkdir(parents=True, exist_ok=True)

DB = _SCRATCH / "mem.sqlite3"
STATE = _SCRATCH / "state"
STATE.mkdir(parents=True, exist_ok=True)
os.environ["TULPA_WG_STATE"] = str(STATE)
os.environ["TULPA_WG_DB"] = str(_SCRATCH / "chats.sqlite3")

import reply  # noqa: E402
import wg_aliases  # noqa: E402
from water_group.memory_engine import get_memory_engine  # noqa: E402

GROUP = "777000111"
CID = f"666000222:group:{GROUP}"
# 甲：主档有主名+历史外号；乙：主名只在覆盖行里有别群外号；丙：只在覆盖行
# 戊/己/庚：单字主名、单字外号、泛称别名各一，钉精确视图不被扫描规则挡掉（#54）
PROFILES = [
    ("10001", "群友甲", ["甲哥", "老甲"]),
    ("10002", "群友乙", []),
    ("10003", "丙老板", []),
    ("10004", "戊", ["老戊"]),
    ("10005", "群友己", ["己"]),
    ("10006", "群友庚", ["领导"]),
]
OVERLAY = [("10002", "乙总"), ("10003", "老丙")]


def build():
    get_memory_engine()  # 建表
    conn = sqlite3.connect(DB)
    for uid, name, aliases in PROFILES:
        conn.execute(
            "INSERT INTO member_profiles (user_id, primary_name, aliases, tone_style,"
            " interests, traits, key_memories, created_at, updated_at)"
            " VALUES (?,?,?,'','[]','{}','[]',0,0)",
            (uid, name, json.dumps(aliases, ensure_ascii=False)))
    for uid, alias in OVERLAY:
        conn.execute(
            "INSERT INTO member_group_memories (user_id, group_id, key_memories, tone_style,"
            " interests, aliases, created_at, updated_at) VALUES (?,?,'[]','','[]',?,0,0)",
            (uid, GROUP, json.dumps([alias], ensure_ascii=False)))
    conn.commit()
    conn.close()
    # chat_inbox 夹具（--context 端到端）
    chats = sqlite3.connect(_SCRATCH / "chats.sqlite3")
    chats.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    chats.execute("CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id TEXT, message_id TEXT,"
                  " payload TEXT, media TEXT NOT NULL DEFAULT '[]', details TEXT NOT NULL DEFAULT '[]',"
                  " delivered INTEGER NOT NULL DEFAULT 0)")
    chats.execute("INSERT INTO chat_sessions VALUES ('s1', ?, 1)", (CID,))
    payload = {"sender": "群友甲", "content": "老甲今天带 「乙总」 过来玩",
               "timestamp": 1792815041000, "is_self": False, "conversation_id": CID}
    chats.execute("INSERT INTO chat_inbox (id,session_id,message_id,payload) VALUES (1,'s1','m1',?)",
                  (json.dumps(payload, ensure_ascii=False),))
    # 单字显示名：行内必须带档案锚，正文里的单字不得长出指向（issue #54）
    payload = {"sender": "己", "content": "己来了",
               "timestamp": 1792815042000, "is_self": False, "conversation_id": CID}
    chats.execute("INSERT INTO chat_inbox (id,session_id,message_id,payload) VALUES (2,'s1','m2',?)",
                  (json.dumps(payload, ensure_ascii=False),))
    chats.commit()
    chats.close()


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def 正文含已知外号_给出指向():
    hits = wg_aliases.body_hits("老甲 这次靠谱", GROUP)
    return hits == ["↪ 外号「老甲」= 群友甲(10001)"]


@case
def 正文含未知称呼_不长出指向():
    return wg_aliases.body_hits("那个谁来着", GROUP) == []


@case
def 通用词不当人名():
    """「领导」「群友」是 GENERIC_ALIASES：对谁都不指。"""
    return wg_aliases.body_hits("领导说话了，群友都在", GROUP) == []


@case
def 主名也算指向():
    hits = wg_aliases.body_hits("丙老板 结账", GROUP)
    return hits == ["↪ 外号「丙老板」= 丙老板(10003)"]


@case
def 发送者对得上_附档案锚():
    # 发送者就是主名时不必重复主名，只附外号锚
    return wg_aliases.sender_tag("群友甲", GROUP) == "（外号：甲哥/老甲）"


@case
def 发送者按外号也对得上():
    """唤醒行印的可能是外号本身——照样要能附上档案锚。"""
    return "档案：群友甲" in wg_aliases.sender_tag("老甲", GROUP)


@case
def 发送者对不上_原样():
    return wg_aliases.sender_tag("查无此人", GROUP) == ""


@case
def 单字主名_发送者带外号锚():
    """主名只有一个字，精确查找也必须认出档案（issue #54）。"""
    return wg_aliases.sender_tag("戊", GROUP) == "（外号：老戊）"


@case
def 单字外号_发送者带档案锚():
    return wg_aliases.sender_tag("己", GROUP) == "（档案：群友己）"


@case
def 单字名不参与正文扫描():
    """子串扫描继续挡单字：正文里的「戊」「己」不产生任何指向。"""
    return (wg_aliases.body_hits("戊说她要睡了", GROUP) == []
            and wg_aliases.body_hits("己来了", GROUP) == [])


@case
def 泛称精确命中_正文仍不指():
    """显示名就叫「领导」是确定的同一字符串；正文里的泛称仍不指（issue #54）。"""
    return (wg_aliases.sender_tag("领导", GROUP) == "（档案：群友庚）"
            and wg_aliases.body_hits("领导说话了，群友都在", GROUP) == [])


@case
def 群覆盖行外号也指向():
    """乙总只在覆盖行里（issue #5 并集视图），按群号必须指得到。"""
    return wg_aliases.body_hits("乙总 来了", GROUP) == ["↪ 外号「乙总」= 群友乙(10002)"]


@case
def 无群号时只用主档():
    """不给群号 = 跨群主档视图：主档外号指到，纯覆盖行外号不指。"""
    wg_aliases._CACHE.clear()
    try:
        return (wg_aliases.body_hits("老甲 乙总", None) == ["↪ 外号「老甲」= 群友甲(10001)"])
    finally:
        wg_aliases._CACHE.clear()


@case
def 同一个人只指一次_长的优先():
    """老甲/甲哥 同属甲：出现两个也只指一次，且长的先匹配。"""
    hits = wg_aliases.body_hits("老甲 和 甲哥 都在", GROUP)
    return len(hits) == 1 and hits[0].endswith("群友甲(10001)")


@case
def 引擎故障_静默降级():
    engine = get_memory_engine()
    real = engine.alias_index
    real_exact = engine.alias_index_exact
    boom = lambda **k: (_ for _ in ()).throw(RuntimeError("boom"))
    engine.alias_index = boom
    engine.alias_index_exact = boom
    wg_aliases._CACHE.clear()
    try:
        return (wg_aliases.sender_tag("群友甲", GROUP) == ""
                and wg_aliases.body_hits("老甲 今天", GROUP) == [])
    finally:
        engine.alias_index = real
        engine.alias_index_exact = real_exact
        wg_aliases._CACHE.clear()


@case
def context_端到端带指向():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = reply.show_context(10)
    text = buf.getvalue()
    return (code == 0 and "↪ 外号「乙总」= 群友乙(10002)" in text
            and "群友甲（外号：甲哥/老甲）" in text)


@case
def context_端到端单字锚():
    """单字显示名的上下文行带档案锚，正文里的单字不长出指向（issue #54）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = reply.show_context(10)
    text = buf.getvalue()
    return (code == 0 and "己（档案：群友己）: 己来了" in text
            and "外号「己」" not in text)


def main():
    return fs.run_cases(CASES, setup=build, prefix=False)


if __name__ == "__main__":
    sys.exit(main())
