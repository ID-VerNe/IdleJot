"""issue #52 gate: at 段必须带显示名，正文命中不许自指 QQ 号。

实测症状（2026-10-24 晚）：唤醒行 `id=6263 [at] <群友甲>…: @ 快变神里绫华`
——`@` 后面本该是被 @ 人的名字，永远是空的；行尾还多一段
`↪ 外号「<self_account>」= <机器人名>(<self_account>)`，把自己的 QQ 号当
外号解释了一遍。两个问题互相放大：@ 段不存名字 → 渲染只能吐 `@<qq>` →
正文里出现纯数字 → 建档时写进 aliases 的 QQ 号被 `body_hits` 当外号命中。

本门禁钉四层，缺一层回归就能 silent 复燃：

  1. `onebot_segments.at()` 契约：name 非空（strip 后）才进段
     （`NAME_LIMIT=100` 截断），无 name 的请求与旧事件回落
     `{'type':'at','qq':…}` 不变；
  2. 转发体两侧透传：`sanitize_nodes` 收 OneBot 原始段的 `data.name`，
     `ChatDetails.render` 优先印 name、段上保留 name；旧段（无 name）
     回落 `@<qq>`；
  3. 入库侧 AST 钉映射（同 `check_sender_name.py` 范式）：`mcp_chat.receive`
     必须把原始 name 过 `at_name` 归一化、`at_segment` 带第二参、content 拼
     `@<显示名>`——只加函数没人调 = 死协议；
  4. `wg_aliases.body_hits` 防自指：扫描前剥掉 `@<数字>`（指向信息不是正文，
     用本模块自带的 `AT_MENTION_RE`——`wg_text` 自 issue #138 起显示时保留
     `@<qq>` 原样，两处口径已分开）；纯数字别名（QQ 号）不参与正文命中；
     `@<显示名>` 命中别名视为真实提及，保留。

未打补丁的 base 上本门禁必须红，且红签名就是 issue 症状（at() 不收 name、
body_hits 对 `@<qq>` 自指）——不是 import 裸奔。

    /usr/bin/env -u PYTHONPATH .venv/Scripts/python.exe scripts/check_at_segment_name.py
"""
import ast
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: F401,E402  先钉数据根（#79/#89 同类）
fs.bootstrap_paths(scripts=False)

# 记忆库路径 import 期求值，必须在 import daemon/chatlocal 模块之前钉走。
_SCRATCH = Path(tempfile.mkdtemp(prefix="wg_at_name_"))
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
os.environ["IDLEJOT_WG_MEMORY_DB"] = str(_SCRATCH / "mem.sqlite3")
(_SCRATCH / "home").mkdir(parents=True, exist_ok=True)

from chatlocal import onebot_segments  # noqa: E402
from chatlocal.mcp_chat_details import ChatDetails, sanitize_nodes  # noqa: E402
from chatlocal.onebot_segments import at as at_segment  # noqa: E402
from water_group.memory_engine import get_memory_engine  # noqa: E402

import wg_aliases  # noqa: E402

# base 上没有 NAME_LIMIT（#52 新增）：取不到按同一封顶值跑，红在行为不在 import
NAME_LIMIT = getattr(onebot_segments, "NAME_LIMIT", 100)

# 夹具一律假值；private 数据（群号/QQ 号）进仓会被 check_publication 拦。
GROUP = "777000333"
QQ_A = "10001"
DISPLAY = "小明"
LONG_NAME = "名" * (NAME_LIMIT + 20)
FAILURES = []


def at_or_fail(*args):
    """base 上 at() 只收一个参数：把 TypeError 翻成干净的行为断言。"""
    try:
        return at_segment(*args)
    except TypeError as exc:
        raise AssertionError(f"at() 必须收第二参 name（{len(args)} 参调用被拒）：{exc}")


def case_at_contract():
    """name 非空才进段；缺失/空白回落旧形状；超长按 NAME_LIMIT 截断。"""
    assert at_segment(QQ_A) == {"type": "at", "qq": QQ_A}, "无 name 必须回落旧形状"
    for blank in (None, "  "):
        assert at_or_fail(QQ_A, blank) == {"type": "at", "qq": QQ_A}, f"{blank!r} 同缺失"
    with_name = at_or_fail(QQ_A, DISPLAY)
    assert with_name == {"type": "at", "qq": QQ_A, "name": DISPLAY}, with_name
    clipped = at_or_fail(QQ_A, LONG_NAME)
    assert clipped.get("name") == "名" * NAME_LIMIT, f"name 超 {NAME_LIMIT} 必须截断"
    assert len(clipped["name"]) == NAME_LIMIT
    print("✓ 1. at() 契约：name 非空才进段、缺失回落、NAME_LIMIT 截断")


def case_forward_nodes_passthrough():
    """转发体两侧透传 name：sanitize 收 name，render 优先印 name。"""
    node = dict(sender={}, message=[dict(type="at", data=dict(qq=QQ_A, name=DISPLAY))])
    nodes = sanitize_nodes([node])
    segs = nodes[0]["segments"]
    assert segs == [dict(type="at", qq=QQ_A, name=DISPLAY)], f"sanitize 丢了 name: {segs}"
    item = ChatDetails.__new__(ChatDetails)  # render 不碰 self（quote_visibility 同范式）
    out = ChatDetails.render(item, segs, [], 0)
    assert out["content"] == "@" + DISPLAY, f"转发体必须印显示名: {out['content']}"
    assert out["segments"] == [dict(type="at", qq=QQ_A, name=DISPLAY)], out["segments"]
    # 旧持久化段没有 name：回落 @<qq>，形状不变
    legacy = sanitize_nodes([dict(sender={}, message=[dict(type="at", data=dict(qq=QQ_A))])])
    assert legacy[0]["segments"] == [dict(type="at", qq=QQ_A)], legacy[0]["segments"]
    out2 = ChatDetails.render(item, legacy[0]["segments"], [], 0)
    assert out2["content"] == "@" + QQ_A, out2["content"]
    assert out2["segments"] == [dict(type="at", qq=QQ_A)], out2["segments"]
    print("✓ 2. 转发体透传：sanitize/render 都带 name，旧段回落 @<qq>")


def case_receive_maps_name():
    """mcp_chat.receive 入库侧 AST 钉 name 映射（存在但没人调 = 死协议）。"""
    src = (ROOT / "chatlocal" / "mcp_chat.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    receive = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "receive"), None)
    assert receive is not None, "mcp_chat.receive 不存在"
    calls = [n for n in ast.walk(receive) if isinstance(n, ast.Call)]
    # 原始 name 必须过 at_name 归一化（NAME_LIMIT 的唯一执行点）
    assert any(isinstance(c.func, ast.Name) and c.func.id == "at_name"
               for c in calls), "receive() 必须调用 at_name 归一化原始 name"
    # at 段必须带上 name（第二参）
    at_calls = [c for c in calls
                if isinstance(c.func, ast.Name) and c.func.id == "at_segment"]
    assert at_calls, "receive() 必须调用 at_segment 建 at 段"
    assert any(len(c.args) >= 2 for c in at_calls), "at_segment 必须带第二参 name"
    # content 拼的是 '@' + (display or value)，不是裸 '@'+value
    at_parts = []
    for c in calls:
        if not (isinstance(c.func, ast.Attribute) and c.func.attr == "append"):
            continue
        for n in ast.walk(c):
            if (isinstance(n, ast.BinOp) and isinstance(n.left, ast.Constant)
                    and n.left.value == "@"):
                at_parts.append(n)
    assert at_parts, "receive() 必须拼 '@'+显示名 进 content"
    assert any(any(isinstance(x, ast.Name) and x.id == "display" for x in ast.walk(p))
               for p in at_parts), "content 的 @ 段必须优先用 display"
    print("✓ 3. mcp_chat.receive AST：at_name 归一化、at_segment 带 name、content 优先显示名")


def build_profiles():
    """建档夹具：aliases 里同时放外号和 QQ 号（复现实机自指路径）。"""
    get_memory_engine()  # 建表
    conn = sqlite3.connect(os.environ["IDLEJOT_WG_MEMORY_DB"])
    conn.execute(
        "INSERT INTO member_profiles (user_id, primary_name, aliases, tone_style,"
        " interests, traits, key_memories, created_at, updated_at)"
        " VALUES (?,?,?,'','[]','{}','[]',0,0)",
        (QQ_A, "群友甲", json.dumps(["甲哥", "老甲", QQ_A], ensure_ascii=False)))
    conn.commit()
    conn.close()


def case_body_hits_no_self_reference():
    """@<数字> 剥离 + 纯数字别名不命中；正常外号照指。"""
    build_profiles()
    wg_aliases._CACHE.clear()
    # 问题二原样复现：@ 段渲染出的 QQ 号 + aliases 里的同名 QQ 号 → 不许自指
    assert wg_aliases.body_hits("@" + QQ_A + " 在吗", GROUP) == [], "@<qq> 不许命中纯数字别名"
    assert wg_aliases.body_hits(QQ_A + " 在吗", GROUP) == [], "裸 QQ 号也不许命中"
    # 正常外号照指（#52 不削弱 #30）
    assert wg_aliases.body_hits("老甲 在吗", GROUP) == ["↪ 外号「老甲」= 群友甲(10001)"]
    # @<显示名> 命中别名视为真实提及，保留
    assert wg_aliases.body_hits("@老甲 在吗", GROUP) == ["↪ 外号「老甲」= 群友甲(10001)"]
    # 指向文本剥掉后，同一行里的真外号仍然命中
    assert wg_aliases.body_hits("@" + QQ_A + "老甲 在吗", GROUP) == ["↪ 外号「老甲」= 群友甲(10001)"]
    # 整行只有指向文本 → 无命中
    assert wg_aliases.body_hits("@" + QQ_A, GROUP) == []
    print("✓ 4. body_hits：@<数字>/纯数字别名不自指，正常外号与 @<显示名> 照指")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cases = [
        ("at() 契约（name 进段/回落/截断）", case_at_contract),
        ("转发体透传 name（sanitize+render）", case_forward_nodes_passthrough),
        ("receive 入库侧 AST 钉 name 映射", case_receive_maps_name),
        ("body_hits 防自指（@<数字>/纯数字别名）", case_body_hits_no_self_reference),
    ]
    for label, fn in cases:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001  收集全部红签名，不停在第一个
            print(f"✗ {label}：{type(exc).__name__}: {exc}")
            FAILURES.append(label)
    print()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} 项未通过 —— {', '.join(FAILURES)}")
        return 1
    print(f"PASS: at 段带显示名、正文命中不自指（{len(cases)} 项全过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
