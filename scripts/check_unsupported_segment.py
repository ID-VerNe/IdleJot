"""issue #165 gate: 白名单外的段必须留下原始类型，不能只剩「非文本消息」。

issue #165 的症状有两层，第二层比第一层更难发现：

  1. 卡片 / 小程序 / 分享 / 位置 / 戳一戳这些段落到 `chat_inbox` 后，`content`
     只剩 `[非文本消息]`，原始 `kind` 与整段 `data` 一起消失，Agent 既不知道
     是什么类型，也无处补读；
  2. 降级写法 `dict(type=label)` 把**中文标签**写进了 `segments[].type`——下游
     拿到的不是「类型未知」而是「类型就叫非文本消息」。一个字段说假话比缺字段
     更坏：任何按 `type` 分派的下游都再也认不出这是未知段。

所以本门禁不钉「输出里有没有某几个字」，而是钉四层可观察契约：

  1. `onebot_segments.unsupported()`：`type` 恒为英文 `unsupported`，`kind` 是
     归一后的原始段名，`raw` 受限长度且裁过带 `raw_truncated`（裸切片或丢字段
     都会红）；`label()` 只决定 content 里那几个字，永不回流到 `type`；
  2. 入库侧真实路径（`mcp_chat.receive` 写进 `chat_inbox` 的 payload）：合成
     `json` / `xml` / `poke` 段后 `segments[].kind` 保留，`content` 不再空壳，
     且**任何段**的 `type` 都不等于中文标签；
  3. 一条消息的原始体总量封顶：30 段巨型卡片不许把整条消息的 `segments` 挤空
     （payload 有 20000 字符整体封顶，逐段不合计就等于修一个丢字段换一个丢消息）；
  4. 补读侧（`wg_text.unsupported_lines` + `strip_placeholders`）：`--show` 说得出
     「类型 + 原始体在不在、裁没裁」，唤醒行把卡片标题剥成短替代而不占正文额度。

AST 墓碑（第 9 项）钉住两种翻车写法不许回来：`dict(type=label)` 与
`.get(kind,'非文本消息')`。文本扫描会被 docstring 里引用反例误伤，所以按语法树
判定——切的对象是 `label` 这个**变量名**才算命中。

未打补丁的 base 上本门禁必须红，红签名就是 issue 症状（`type` 是中文标签、
`kind` 不存在、content 是空壳），不是 import 裸奔。

    /usr/bin/env -u PYTHONPATH .venv/Scripts/python.exe scripts/check_unsupported_segment.py
"""
import ast
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 先钉数据根（#79/#89 同类）：memory_engine 的 WG_HOME 与 DEFAULT_DB_PATH 是
# import 期求值的，晚一步夹具档案就写进实机记忆库。
import check_fixture_support as fs  # noqa: F401,E402  (issue #169 PE: path 注入收口)
from check_fixture_support import pin_state_env, scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False)

_SCRATCH = scratch_dir(prefix="tulpa-unsupported-")
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
os.environ["IDLEJOT_WG_MEMORY_DB"] = str(_SCRATCH / "mem.sqlite3")
pin_state_env(_SCRATCH / "state")  # state 两腿都钉（#281）
os.environ["IDLEJOT_WG_DB"] = str(_SCRATCH / "mcp-access.sqlite3")
(_SCRATCH / "home").mkdir(parents=True, exist_ok=True)
(_SCRATCH / "state").mkdir(parents=True, exist_ok=True)

from chatlocal import onebot_segments  # noqa: E402
from chatlocal.mcp_access import MCPAccess  # noqa: E402
from chatlocal.mcp_actions import MCPActions  # noqa: E402
from chatlocal.mcp_chat import MCPChat  # noqa: E402
from chatlocal.mcp_chat_details import ChatDetails, sanitize_nodes  # noqa: E402
from chatlocal.store import Store  # noqa: E402

import wg_text  # noqa: E402

# 夹具一律假值；真群号/QQ 进仓会被 check_publication 拦。
ACCOUNT = "111"
GROUP = "777000222"
PEER = "333"
CID = f"{ACCOUNT}:group:{GROUP}"
SESSION = "s" * 32
FAILURES = []

# 段名形态：OneBot 段名都是小写字母开头的短标识。中文标签不是段名——这正是
# issue #165 第二层的病根，归一必须把它变成 unknown 而不是原样传下去。
CHINESE_LABELS = ("非文本消息", "图片", "语音", "视频", "文件", "表情", "合并转发", "引用", "@")

CARD_JSON = json.dumps({
    "app": "com.tencent.miniapp_01",
    "prompt": "[QQ小程序]群签到",
    "meta": {"detail_1": {"title": "群签到小助手", "desc": "今天已签到 3 人"}},
}, ensure_ascii=False)

CARD_XML = ("<?xml version='1.0' encoding='utf-8'?><msg serviceID='1'><item>"
            "<title>分享了一篇文章</title><summary>关于周末活动的说明</summary>"
            "</item></msg>")


# --------------------------------------------------------------- 1. 段构造契约

def case_segment_contract():
    """`unsupported()` 保真、`label()` 只做显示、`kind` 归一，三件都不许混。"""
    unsupported = onebot_segments.unsupported
    if not hasattr(onebot_segments, "unsupported"):
        raise AssertionError("onebot_segments 缺少 unsupported()（#165 PA 的唯一构造点）")

    segment = unsupported("json", {"data": CARD_JSON})
    assert segment["type"] == "unsupported", f"type 必须是英文标识，实得 {segment['type']!r}"
    assert segment["kind"] == "json", segment
    assert segment["type"] not in CHINESE_LABELS
    # raw 是整段 data 的 JSON 文本，内层引号被转义——按内容子串断言，不按原文比对。
    assert "群签到" in segment.get("raw", ""), f"原始体必须留下来（否则无处补读）：{segment}"
    assert "raw_truncated" not in segment, f"没裁就不许标裁过：{segment}"

    # 段名归一：认识的保留，不是段名形态的记 unknown。把「非文本消息」当 kind
    # 传下去等于换了个地方说假话。
    assert onebot_segments.kind_of("poke") == "poke"
    assert onebot_segments.kind_of("JSON") == "json"
    assert onebot_segments.kind_of("非文本消息") == "unknown", "中文标签不是段名"
    assert onebot_segments.kind_of("") == "unknown"
    assert onebot_segments.kind_of("a" * 200) == "unknown", "超长的是坏数据不是新类型"

    # label() 只服务 content：认识的给中文名，不认识的点名原始 kind。
    assert onebot_segments.label("json") == "卡片"
    assert onebot_segments.label("poke") == "戳一戳"
    assert onebot_segments.label("mystery") == "不支持:mystery", "不认识的要点名，不许含糊"
    assert onebot_segments.label("非文本消息") == "非文本消息", "归一后是 unknown，退回旧文案"
    for kind in ("json", "xml", "poke", "mystery", "非文本消息"):
        assert onebot_segments.label(kind) != "unsupported", "label 不许回流到 type"
    print("✓ 1. unsupported()/label()/kind_of()：type 恒英文、kind 归一、raw 保真")


def case_raw_clipping():
    """原始体按 text_limits 裁（不许裸切片），裁过必须自己说出来。"""
    huge = {"data": "长" * 50000}
    segment = onebot_segments.unsupported("json", huge)
    assert segment["type"] == "unsupported" and segment["kind"] == "json", segment
    assert len(segment.get("raw", "")) <= onebot_segments.UNSUPPORTED_RAW_CHARS, len(segment.get("raw", ""))
    assert segment.get("raw_truncated") is True, f"裁过必须标记：{segment}"
    # 裸切片会切在半个 JSON 上；clip() 走的是同一份截断契约，长文本不丢标记。
    assert segment["raw"], "裁完仍要有内容，不许变成空串"
    print(f"✓ 2. 原始体按 clip 裁到 {onebot_segments.UNSUPPORTED_RAW_CHARS} 字并标 raw_truncated")


def case_budget_exhausted():
    """预算耗尽时说「超预算未保留」，不许假装原始体本来就是空的。"""
    budget = {"raw": 10}
    first = onebot_segments.unsupported("json", {"data": "甲" * 100}, budget)
    assert first.get("raw") and budget["raw"] == 0, (first, budget)
    second = onebot_segments.unsupported("xml", {"data": "乙" * 100}, budget)
    assert second["type"] == "unsupported" and second["kind"] == "xml", second
    assert "raw" not in second, f"预算用完不该再留原始体：{second}"
    assert second.get("raw_truncated") is True, f"「没保留」与「本来就空」必须分开：{second}"
    print("✓ 3. 原始体预算耗尽：标 raw_truncated，不伪装成空原始体")


def case_card_summary():
    """PB：json / xml / share 取得到标题；取不到返回空串而不是编一个。"""
    summary = onebot_segments.card_summary({"data": CARD_JSON})
    assert "群签到小助手" in summary or "群签到" in summary, summary
    xml_summary = onebot_segments.card_summary({"data": CARD_XML})
    assert "分享了一篇文章" in xml_summary, xml_summary
    assert onebot_segments.card_summary({"title": "分享标题"}) == "分享标题"
    # 从落盘后的 raw 文本里也要读得出来：--show 走的是这条路。
    segment = onebot_segments.unsupported("json", {"data": CARD_JSON})
    assert "群签到" in onebot_segments.card_summary(segment.get("raw")), segment
    for empty in (None, {}, {"data": ""}, {"data": "{}"}, {"data": "不是 JSON 也不是 XML"}):
        assert onebot_segments.card_summary(empty) == "", f"{empty!r} 不该有摘要"
    print("✓ 4. card_summary：json/xml/share 都取得到标题，取不到返回空串")


# --------------------------------------------------------- 2. 入库侧真实路径

def build_chat():
    """起一个只碰临时库的 MCPChat：假 grant + 假会话，不发任何 QQ 请求。

    chat_sessions / chat_inbox 的表结构由 MCPChat 的构造器建（它是唯一归属），
    所以先构造再插会话行——自己另写一份 DDL 就成了第二份事实来源，改列时
    两边会漂移。

    `scope['conversations']` 的落库形态是**每条一个 JSON 字符串**（`MCPAccess.create`
    里 `json.dumps(pair)` 后逐条塞进列表），`live_target` 也按这个形态 `json.loads`。
    夹具写成 `[["qq", CID]]` 会在授权边界上抛 TypeError，看起来像段保真的 bug。
    """
    store = Store(_SCRATCH / "chats.sqlite3")
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
        db.execute("INSERT INTO chat_sessions(id,grant_id,conversation_id,name,persona,"
                   "participation,request_key,signature,active,created,updated,cursor,"
                   "offered,note,last_contact,stop_reason,source)"
                   " VALUES(?,?,?,?,?,?,?,?,1,?,?,0,0,'',0,'','onebot')",
                   (SESSION, "g1", CID, "fixture", "fixture", "natural", "k", "sig",
                    time.time() - 60, time.time()))
    return chat


def receive(chat, segments, message_id=1):
    """走 `receive()` 的真实入库路径，回读 chat_inbox 里那行 payload。"""
    chat.receive(dict(post_type="message", message_type="group", self_id=int(ACCOUNT),
                      group_id=int(GROUP), message_id=message_id, user_id=int(PEER),
                      sender=dict(user_id=int(PEER)), time=time.time(),
                      message=segments))
    with chat.access.connect() as db:
        row = db.execute("SELECT payload FROM chat_inbox WHERE session_id=? AND message_id=?",
                         (SESSION, str(message_id))).fetchone()
    assert row is not None, f"receive() 没有落盘 message_id={message_id}（消息段被整条丢弃）"
    return json.loads(row["payload"])


def case_ingest_card_json():
    """issue #165 主症状：json 段落盘后 kind 在、content 不再是空壳。"""
    chat = build_chat()
    payload = receive(chat, [dict(type="json", data=dict(data=CARD_JSON))], 4101)
    segments = payload.get("segments") or []
    assert segments, "json 段被整条丢了"
    first = segments[0]
    assert first.get("kind") == "json", f"原始段类型没留下来：{first}"
    assert first.get("type") == "unsupported", f"type 必须是英文标识：{first}"
    assert first.get("type") not in CHINESE_LABELS, f"中文标签又进了 type：{first}"
    assert first.get("raw"), "原始体没留下来，Agent 无处补读"
    content = payload.get("content") or ""
    assert content != "[非文本消息]", f"content 还是空壳：{content!r}"
    assert "卡片" in content, f"卡片段至少要报出类型：{content!r}"
    assert "群签到" in content, f"能解析的卡片要带标题（PB）：{content!r}"
    print(f"✓ 5. 入库 json 段：kind=json、type=unsupported、content={content!r}")


def case_ingest_xml_and_unknown():
    """xml 段同型；戳一戳这种没有摘要的段至少报出类型。"""
    chat = build_chat()
    payload = receive(chat, [dict(type="xml", data=dict(data=CARD_XML))], 4102)
    first = (payload.get("segments") or [{}])[0]
    assert first.get("kind") == "xml", first
    assert "分享了一篇文章" in (payload.get("content") or ""), payload.get("content")

    poke = receive(chat, [dict(type="poke", data=dict(type="poke", id="1"))], 4103)
    first = (poke.get("segments") or [{}])[0]
    assert first.get("kind") == "poke", f"戳一戳的 kind 丢了：{first}"
    assert "戳一戳" in (poke.get("content") or ""), poke.get("content")
    assert first.get("type") not in CHINESE_LABELS, first

    # 完全不认识的段名也要留证据，而不是笼统一句「非文本消息」。
    weird = receive(chat, [dict(type="brand_new_segment", data=dict(x="1"))], 4104)
    first = (weird.get("segments") or [{}])[0]
    assert first.get("kind") == "brand_new_segment", f"新段名要原样保留：{first}"
    assert "brand_new_segment" in (weird.get("content") or ""), weird.get("content")
    print("✓ 6. 入库 xml/poke/未知段：kind 各自保留，content 报得出类型")


def case_no_chinese_type_anywhere():
    """任何段、任何层都不许把中文标签当 type（含旧代码里那六种）。"""
    chat = build_chat()
    payload = receive(chat, [dict(type=kind, data=dict(x="1")) for kind in
                             ("json", "xml", "share", "location", "poke", "dice", "face")], 4105)
    for segment in payload.get("segments") or []:
        assert segment.get("type") not in CHINESE_LABELS, f"中文标签进了 type：{segment}"
        assert segment.get("type") == "unsupported", segment
        assert segment.get("kind"), f"未知段必须带 kind：{segment}"
    print(f"✓ 7. {len(payload.get('segments') or [])} 种未知段：type 全是 unsupported、kind 各自保留")


def case_budget_caps_whole_message():
    """30 段巨型卡片：原始体合计封顶，整条消息的 segments 不许被挤空。"""
    chat = build_chat()
    fat = [dict(type="json", data=dict(data=json.dumps({"prompt": "肥" * 4000}, ensure_ascii=False)))
           for _ in range(30)]
    payload = receive(chat, fat, 4106)
    segments = payload.get("segments") or []
    assert len(segments) == 30, f"段清单被挤掉了 {30 - len(segments)} 条：{len(segments)}"
    kept = sum(len(s.get("raw") or "") for s in segments)
    assert kept <= onebot_segments.UNSUPPORTED_TOTAL_CHARS, f"原始体合计 {kept} 超过总预算"
    assert payload.get("truncated") is True or kept, "封顶不许静默"
    # 逐段各自封顶 2000 的话合计 60000，会把 payload 撑过 20000 而清空 segments。
    assert len(json.dumps(payload, ensure_ascii=False)) < 20000, "payload 仍要装得下"
    print(f"✓ 8. 30 段巨型卡片：段清单完整、原始体合计 {kept} 字在总预算内")


# ------------------------------------------------------- 3. 转发节点与补读侧

def case_forward_nodes_fidelity():
    """合并转发节点内同型降级：sanitize 保留 kind，render 不写中文 type。"""
    node = dict(sender={"user_id": 1, "nickname": "a"},
                message=[dict(type="json", data=dict(data=CARD_JSON))])
    nodes = sanitize_nodes([node])
    segments = nodes[0]["segments"]
    assert segments and segments[0].get("type") == "unsupported", segments
    assert segments[0].get("kind") == "json", f"节点内 kind 丢了：{segments[0]}"

    item = ChatDetails.__new__(ChatDetails)  # render 不碰 self（#52 同范式）
    out = ChatDetails.render(item, segments, [], 0)
    assert out["segments"][0].get("type") == "unsupported", out["segments"]
    assert out["segments"][0].get("kind") == "json", out["segments"]
    assert "群签到" in out["content"], f"节点内卡片也要带标题：{out['content']!r}"
    for segment in out["segments"]:
        assert segment.get("type") not in CHINESE_LABELS, segment
    print(f"✓ 9. 转发节点：sanitize/render 两侧都保留 kind，content={out['content']!r}")


def case_show_reports_raw_state():
    """PC：`--show` 说得出「类型 + 原始体在不在、裁没裁」。"""
    lines = wg_text.unsupported_lines([
        dict(type="unsupported", kind="json", raw='{"prompt":"甲"}'),
        dict(type="unsupported", kind="xml", raw="乙" * 10, raw_truncated=True),
        dict(type="unsupported", kind="poke", raw_truncated=True),
        dict(type="unsupported", kind="share"),
        dict(type="text", text="正文不参与"),
        dict(type="image", image_index=0),
    ])
    joined = "\n".join(lines)
    assert "段 json：原始体完整" in joined, joined
    assert "段 xml：原始体已截断" in joined, joined
    assert "段 poke：原始体超预算未保留" in joined, joined
    assert "段 share：无原始体" in joined, joined
    assert "正文不参与" not in joined and "image" not in joined, "只报降级段"
    assert len(lines) == 4, lines
    print("✓ 10. --show 的段说明：类型 + 原始体完整/已截断/超预算未保留/无原始体")


def case_wake_line_keeps_budget():
    """唤醒行：卡片标题剥成短替代，不占正文额度；旧占位仍认。"""
    body = "[卡片：群签到小助手，今天已签到 3 人]" + "正文" * 40
    stripped, original = wg_text.strip_placeholders(body)
    assert "[卡片]" in stripped, stripped
    assert "群签到" not in stripped, f"标题不该占唤醒行额度：{stripped}"
    assert original == len(body), (original, len(body))
    assert wg_text.strip_placeholders("[不支持:mystery]正文")[0] == "[非文本]正文"
    # 旧记录里的占位仍要剥（兼容），否则历史唤醒行会突然变长。
    assert wg_text.strip_placeholders("[非文本消息]正文")[0] == "[非文本]正文"
    assert wg_text.strip_placeholders("[表情]正文")[0] == "[非文本]正文"
    print("✓ 11. 唤醒行占位表：卡片/不支持段剥成短替代，旧占位仍认")


# ------------------------------------------------------------- 4. 墓碑（AST）

_TOMBSTONES = (
    ("dict(type=label)", "中文标签写进 segments[].type（issue #165 第二层病根）"),
    ("dict(type=label", "中文标签写进 segments[].type（issue #165 第二层病根）"),
    ("'非文本消息')", "把中文标签当默认 kind 传下去"),
)


def _label_type_calls(tree):
    """AST 里 `dict(type=label…)` 这种把变量 label 当 type 值的写法。"""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "dict"):
            continue
        for keyword in node.keywords:
            if keyword.arg != "type":
                continue
            value = keyword.value
            if isinstance(value, ast.Name) and value.id == "label":
                hits.append(f"line {node.lineno}: dict(type=label)")
            elif (isinstance(value, ast.BinOp) and isinstance(value.left, ast.Constant)
                  and value.left.value == "[" ):
                hits.append(f"line {node.lineno}: dict(type='[' + …)")
    return hits


def case_no_chinese_type_in_source():
    """墓碑：`dict(type=label)` 与 `.get(kind,'非文本消息')` 不许回来。"""
    for rel in ("chatlocal/mcp_chat.py", "chatlocal/mcp_chat_details.py",
                "chatlocal/onebot_segments.py"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        hits = _label_type_calls(ast.parse(source, str(ROOT / rel)))
        assert not hits, f"{rel} 又把中文标签写进 type（#165 墓碑）：{hits}"
        assert ".get(kind,'非文本消息')" not in source.replace(" ", ""), \
            f"{rel} 仍在用中文标签兜底 kind（#165 墓碑）"
        assert ".get(kind, '非文本消息')" not in source, \
            f"{rel} 仍在用中文标签兜底 kind（#165 墓碑）"
    print("✓ 12. 墓碑：源码里没有 dict(type=label)、没有中文标签兜底 kind")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 72)
    print("        [测试] issue #165 未知段保真：类型与原始体都要留下来")
    print("=" * 72)
    cases = [
        ("段构造契约（type/kind/label 三件分开）", case_segment_contract),
        ("原始体按 clip 裁并标记", case_raw_clipping),
        ("原始体预算耗尽如实说", case_budget_exhausted),
        ("卡片摘要（json/xml/share）", case_card_summary),
        ("入库 json 段保真", case_ingest_card_json),
        ("入库 xml/poke/未知段保真", case_ingest_xml_and_unknown),
        ("任何段 type 都不是中文标签", case_no_chinese_type_anywhere),
        ("一条消息的原始体总预算", case_budget_caps_whole_message),
        ("合并转发节点保真", case_forward_nodes_fidelity),
        ("--show 报原始体状态", case_show_reports_raw_state),
        ("唤醒行占位表兼容", case_wake_line_keeps_budget),
        ("墓碑：源码里没有中文标签当 type", case_no_chinese_type_in_source),
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
    print(f"PASS: 未知段保真（{len(cases)} 项全过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
