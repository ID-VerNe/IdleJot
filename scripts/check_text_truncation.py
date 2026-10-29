"""issue #90 gate: long material must survive the memory path, or say it was cut.

The defect was never the size of the cut, it was the silence. `--dump` handed the
consolidation model 400 of 1524 characters with no field to notice, and
`record_event` capped the summary written back at 500 (then 1000) the same way.
A gate that only asserted "400 is gone" would pass again the day someone picked a
new number, so every check below pins an observable contract instead:

  1. `pending_rows()` returns the body verbatim and marks it not truncated;
  2. `--dump` prints that body whole in its JSON line;
  3. `consolidate_chat_memory(summary=...)` stores a 1600-character summary whole;
  4. a write past `EVENT_TEXT_CHARS` is clipped *and* reports the original
     length, and `query_member_dossier` surfaces both -- detectable, not silent;
  5. an old database gains the two columns without losing its rows;
  6. `clip()` backs up to a sentence boundary, and the chat reader shares it
     instead of keeping a second copy of the limit.

issue #32 把同一契约推到 Agent 实际读消息的 daemon 路径（唤醒行 / --list /
--context / --status），因为它们各写了一份裸切片，且段占位
（`[引用 QQ 消息 <id>]`）也占额度、截断后没有任何标记：

   7. daemon 渲染层不出现裸切片（墓碑：#32 的两种写法直接 fail）；
   8. `wg_text.render_body` 按正文字数裁（段占位不占额度），截断带 `…` 与原长；
   9. `reply.py --show <id>` 按 id 读到全文（wg_events → chat_inbox → 明说没有）。

    .venv/Scripts/python.exe scripts/check_text_truncation.py
"""
import ast
import contextlib
import io
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

from check_fixture_support import scratch_dir, temp_dir  # noqa: E402

# Pin the data root before anything resolves it. memory_engine's WG_HOME and
# DEFAULT_DB_PATH are evaluated at import time, and `get_memory_engine()` seeds
# on first use -- a later patch would be too late and this gate would write
# fixture profiles into the live library (the #79/#89 class of bug).
_SCRATCH = scratch_dir(prefix="tulpa-trunc-")
(_SCRATCH / "home").mkdir(parents=True, exist_ok=True)
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH / "home")
os.environ["IDLEJOT_WG_MEMORY_DB"] = str(_SCRATCH / "mem.sqlite3")

from chatlocal import agent_tools  # noqa: E402
from chatlocal.text_limits import (  # noqa: E402
    EVENT_TEXT_CHARS,
    READ_PREVIEW_CHARS,
    Clipped,
    clip,
)
import dossier  # noqa: E402
import wg_event_ref  # noqa: E402  (issue #216: 按 id 取行与来源顺序的唯一入口)
import wg_text  # noqa: E402  (issue #32: daemon 正文渲染的唯一归属)
from water_group.memory_engine import MemoryEngine  # noqa: E402

# Close to the message in the report: a pasted article whose conclusion sits in
# the second half, i.e. exactly the part the old 400-character slice dropped.
BODY = "资料转述：" + "正" * 1500 + "……后半段结论：两位创始人利用周末和假期无偿组织活动。"
SUMMARY = "沉淀摘要：" + "要" * 1595  # 1600 characters, the size that broke in the report


def test_clip_contract():
    """The shared helper: whole text, prefix + loss report, sentence-aware cut."""
    whole = clip("短消息", 100)
    assert whole == Clipped("短消息", False, 3), whole
    assert clip(None, 10) == Clipped("", False, 0)
    assert clip("123456789", 9).truncated is False  # exactly at the limit, no cut

    hard = clip("无标点" * 100, 60)
    assert hard.truncated and len(hard.text) == 60 and hard.original_length == 300, hard

    # A sentence ends 80 characters in; the window is 10% of 100, so the cut
    # backs up to the full stop instead of splitting the next clause.
    sentence = "甲" * 80 + "。" + "乙" * 100
    soft = clip(sentence, 100)
    assert soft.text.endswith("。") and len(soft.text) == 81, soft
    assert soft.original_length == len(sentence)

    # A boundary too far back is not worth the lost budget: hard cut wins.
    far = "丙" * 100 + "。" + "丁" * 100
    assert len(clip(far, 100).text) == 100

    try:
        clip("x", 0)
    except ValueError:
        pass
    else:
        raise AssertionError("clip() must reject a non-positive limit")
    print("✓ 1. clip() 契约：完整/截断标记/原长/句末回落/无标点硬切")


def _seed_inbox(db_path, bodies, session_id="s1"):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE chat_inbox ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, payload TEXT NOT NULL)")
    for body in bodies:
        conn.execute(
            "INSERT INTO chat_inbox (session_id, payload) VALUES (?, ?)",
            (session_id, json.dumps({
                "sender": "群主",
                "sender_id": 10001,
                "content": body,
                "timestamp": 1.0,
                "truncated": False,
            }, ensure_ascii=False)))
    conn.commit()
    conn.close()


def test_pending_rows_keeps_body(tmp):
    """The reader the consolidation model calls must not shorten anything."""
    db_path = tmp / "inbox.sqlite3"
    _seed_inbox(db_path, [BODY])
    with patch.object(dossier, "DB", db_path):
        rows = dossier.pending_rows("s1", 0)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["text"] == BODY, (len(row["text"]), len(BODY))
    assert row["truncated"] is False, row
    print(f"✓ 2. pending_rows() 完整返回 {len(BODY)} 字正文，truncated=False")


def test_dump_keeps_body(tmp):
    """`--dump` is the JSON the model actually reads; the whole body must be there."""
    db_path = tmp / "inbox-dump.sqlite3"
    _seed_inbox(db_path, [BODY])
    buffer = io.StringIO()
    with patch.object(dossier, "DB", db_path), \
            patch.object(dossier, "active_session", lambda: ("s1", "qq:10001")), \
            patch.object(dossier, "load_mark", lambda: {}):
        with contextlib.redirect_stdout(buffer):
            code = dossier.cmd_dump({"max_material": 80}, 0)
    assert code == 0, code
    lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, lines
    assert lines[0]["text"] == BODY and lines[0]["truncated"] is False, lines[0]
    print(f"✓ 3. --dump 单行 JSON 完整输出 {len(BODY)} 字，无静默截断")


def test_summary_is_stored_whole(tmp):
    """The write side: a long conclusion must not land as its first 500 chars."""
    assert len(SUMMARY) == 1600
    engine = MemoryEngine(tmp / "memory-summary.sqlite3")
    result = engine.consolidate_chat_memory(group_id="g1", summary=SUMMARY)
    assert result["success"], result
    with engine._connect() as conn:
        row = conn.execute(
            "SELECT quote, truncated, original_length FROM member_events "
            "WHERE source='summary'").fetchone()
    assert row is not None, "summary event missing"
    assert row["quote"] == SUMMARY, (len(row["quote"]), len(SUMMARY))
    assert row["truncated"] == 0 and row["original_length"] == 0, dict(row)
    print(f"✓ 4. consolidate_chat_memory 的 {len(SUMMARY)} 字 summary 完整落库")


def test_over_limit_event_is_marked(tmp):
    """Beyond the ceiling the row must admit it, and say how much was there."""
    engine = MemoryEngine(tmp / "memory-cap.sqlite3")
    engine.save_profile(user_id="u1", primary_name="某群友", group_id="g1")
    oversized = "文" * (EVENT_TEXT_CHARS + 500)
    event_id = engine.record_event(
        user_id="u1", group_id="g1", quote=oversized, context="超长原文", source="test")
    with engine._connect() as conn:
        row = conn.execute(
            "SELECT length(quote) AS size, truncated, original_length FROM member_events WHERE id=?",
            (event_id,)).fetchone()
    assert row["size"] == EVENT_TEXT_CHARS, dict(row)
    assert row["truncated"] == 1, dict(row)
    assert row["original_length"] == len(oversized), dict(row)

    # The reader has to see the flag, otherwise the agent cannot compensate.
    dossier_out = engine.query_member_dossier("u1", group_id="g1")
    event = dossier_out["recent_events"][0]
    assert event["truncated"] is True and event["original_length"] == len(oversized), event
    print(f"✓ 5. 超限写入截到 {EVENT_TEXT_CHARS} 并标记 truncated/original_length，查询可见")


def test_old_database_migrates(tmp):
    """Real installs predate these columns; adding them must not lose rows."""
    db_path = tmp / "memory-legacy.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE member_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            quote TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'interaction',
            tags TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL
        );
        INSERT INTO member_events (user_id, group_id, timestamp, quote, context, source, tags, created_at)
        VALUES ('u9', 'g1', 1.0, '历史事件', '', 'legacy', '[]', 1.0);
        """)
    conn.commit()
    conn.close()

    engine = MemoryEngine(db_path)
    with engine._connect() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(member_events)").fetchall()}
        row = conn.execute(
            "SELECT quote, truncated, original_length FROM member_events").fetchone()
    assert {"truncated", "original_length"} <= columns, columns
    # History is left unmarked on purpose: the text is already gone, so claiming
    # it was whole (0) is the only honest default.
    assert row["quote"] == "历史事件" and row["truncated"] == 0 and row["original_length"] == 0
    print("✓ 6. 旧库幂等迁移：新增两列，历史行保留且默认未标记")


def test_chat_reader_shares_the_limit():
    """One implementation: the reader must not grow a second copy of the slice."""
    assert agent_tools.clip is clip, agent_tools.clip
    assert READ_PREVIEW_CHARS == 2000
    print("✓ 7. agent_tools 复用 chatlocal.text_limits.clip，无第二份截断实现")


# ---------------------------------------------------------------------------
# issue #32: daemon 渲染层的截断契约
# ---------------------------------------------------------------------------

# Agent 真正读消息的四个脚本 + 两个渲染层。墓碑断言（#7 项）钉住它们：任何一处
# 退回手写裸切片，这条门禁就红。
#
# issue #169 PA：三个自带 `_clip` 的 handler 模块加进清单。加进来之前先补判据
# （变量切片 + 任意非空常量尾巴，见下），否则「加进清单也不会红」——那是门禁
# 说谎，正是本分节要修的。补完判据这三个文件必须红，再由 PB 分节把实现合流到
# `wg_text.render_body` 修绿。
DAEMON_RENDER_FILES = (
    "water-group/daemon/watch.py",
    "water-group/daemon/reply.py",
    "water-group/daemon/up.py",
    "water-group/daemon/wg_quotes.py",
    "water-group/daemon/wg_text.py",
    "water-group/daemon/sixty.py",
    "water-group/daemon/miyoushe.py",
    "water-group/daemon/busboss.py",
)

# #32 的两种翻车写法。裸切片用 AST 判定（`_bare_slice_hits`）——文本扫描会把
# docstring 里引用反例的 `(content or '')[:80]` 一起误伤；自带 `…` 的第二份
# 截断实现留在文本层：那是 `x[:N] + '…'` 的拼接形态，AST 看不出省略号的语义。
#
# issue #169 PA：旧正则只认 `[:\d+] + '…'`（字面数字 + 省略号）。三份 `_clip`
# 的真实写法是 `text[:limit] + " …（已截断）"` 与 `text[:limit - 1] + "…"`——
# 上限是变量、尾巴是任意非空字符串。正则保留旧字面数字形态做最小兜底，新形态
# 走 AST（`_tombstone_concat_hits`）：`正文变量切片 + 非空字符串常量`。两条
# 都要，因为 `text[:80] + "…"` 这种字面写法 AST 也能抓，但旧正则已覆盖且更便宜，
# 留着不删——墓碑断言多一道判据只会更牢，不会误伤（裸切片判据 `_bare_slice_hits`
# 已经把 `text[:80]` 命中，这里只管「切片后再拼尾巴」的第二份实现）。
_TOMBSTONE_ELLIPSIS = re.compile(r"\[\s*:\s*\d+\s*\]\s*\+\s*['\"]…")

# 裸切片按 AST 判定：切的对象名字叫 content 或 text。`[:3]` 这类枚举用途
# （拆气泡、取前几个候选）不在此列——名字就不是正文。
#
# issue #169 PA：上限不再要求是字面整数。`text[:limit]` / `text[:limit - 1]`
# 这种变量切片同样是裸切片——上限是 `limit` 还是 `80` 不改变「正文被切了且
# 调用方自己处理尾巴」的事实，而第二份截断实现正是靠变量上限 + 自拼尾巴存在
# 的。判据只看「正文变量名 + 任何上限的 `[:...]`」，枚举用途的 `text[:3]` 仍
# 由名字+上限形态排除（它是 `content`/`text` 上的整数切片，但取前几条候选不是
# 正文裁长——本判据宁可多抓再由调用方解释，也不漏掉变量切片这条 #169 红线）。
_BARE_NAMES = ("content", "text")


def _slice_upper(node):
    """`node.slice` 在 py3.9+ 直接是 `ast.Slice`（无 Index 包装）；返回 upper 节点。"""
    sl = node.slice
    if isinstance(sl, ast.Slice):
        return sl.upper
    return None


def _is_truncation_slice(node):
    """`text[:N]` 形态：起点为空（lower is None），上限非空。

    这是截断切片的签名——「取前 N 字」。`text[pos:hit.start()]`（在 @ 边界切
    子串）与 `text[pos:]`（取后缀）都不是截断：前者下界非空、后者上限为空。
    issue #169 PA 最初把判据写成「正文变量名 + 任意上限」，结果误伤 reply.py
    的 `text[pos:hit.start()]`——那条是切子串不是截断。钉住「下界为空」才能
    把 `text[:limit]` / `text[:limit - 1]` / `text[:80]` 三种截断形态与合法
    子串操作分开。
    """
    sl = node.slice
    if not isinstance(sl, ast.Slice):
        return False
    return sl.lower is None and sl.upper is not None


def _bare_slice_hits(tree):
    """AST 里 `(content or '')[:N]` / `text[:N]` 两种正文字符截断切片。

    issue #169 PA：上限 N 不再限定为字面整数——`text[:limit]` / `text[:limit - 1]`
    这种变量切片同样命中。上限形态记录进报错串，方便定位是哪种写法。判据钉死
    「下界为空」（`_is_truncation_slice`），不误伤 `text[pos:hit.start()]` 这类
    合法子串切分。
    """
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        target = node.value
        name = None
        if isinstance(target, ast.Name):
            name = target.id
        elif isinstance(target, ast.BoolOp):
            for value in target.values:
                if isinstance(value, ast.Name):
                    name = value.id
                    break
        if name not in _BARE_NAMES:
            continue
        if not _is_truncation_slice(node):
            continue
        upper = _slice_upper(node)
        hits.append(f"line {node.lineno}: {name}[:{_upper_label(upper)}]")
    return hits


def _upper_label(upper):
    """把上限 AST 节点还原成可读串：`80` / `limit` / `limit - 1`。"""
    if isinstance(upper, ast.Constant):
        return repr(upper.value)
    if isinstance(upper, ast.Name):
        return upper.id
    if isinstance(upper, ast.BinOp):
        try:
            return ast.unparse(upper)
        except Exception:
            return type(upper).__name__
    return type(upper).__name__


def _tombstone_concat_hits(tree):
    """issue #169 PA：`正文截断切片 + 非空字符串常量` 的第二份截断实现。

    形态是 `BinOp(left=Subscript(正文截断切片), op=Add, right=Constant(非空 str))`。
    旧 `_TOMBSTONE_ELLIPSIS` 正则只认字面数字切片 + 省略号；三份 `_clip` 的真实
    写法（`text[:limit] + " …（已截断）"` / `text[:limit - 1] + "…"`）上限是变量、
    尾巴是任意非空字符串，正则逃逸。这里用 AST 抓「正文截断切片后拼了非空尾巴」：
    尾巴空字符串不算（`text[:limit] + ""` 是无意义拼接，不是截断标记）。左操作数
    必须是截断切片（`_is_truncation_slice`），不误伤 `text[pos:end] + sep` 这类
    合法拼接。
    """
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
            continue
        left, right = node.left, node.right
        if not isinstance(left, ast.Subscript):
            continue
        target = left.value
        name = None
        if isinstance(target, ast.Name):
            name = target.id
        elif isinstance(target, ast.BoolOp):
            for value in target.values:
                if isinstance(value, ast.Name):
                    name = value.id
                    break
        if name not in _BARE_NAMES:
            continue
        if not _is_truncation_slice(left):
            continue
        if not isinstance(right, ast.Constant) or not isinstance(right.value, str):
            continue
        if not right.value:
            continue
        hits.append(f"line {node.lineno}: {name}[...] + {right.value!r}")
    return hits


def test_daemon_has_no_bare_slices():
    """墓碑：#32 的裸切片与自带省略号的第二份截断都不许回来。

    issue #169 PA：判据补了两条——变量切片（`text[:limit]` / `text[:limit - 1]`）
    与「正文切片 + 任意非空常量尾巴」（`text[:limit] + " …（已截断）"`）。三个
    handler 模块（sixty/miyoushe/busboss）加进 `DAEMON_RENDER_FILES` 后，这两条
    新判据必须命中它们的 `_clip`——证明门禁真能抓，再由 PB 分节合流到
    `wg_text.render_body` 修绿。
    """
    for rel in DAEMON_RENDER_FILES:
        source = (ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(source, str(ROOT / rel))
        hit = _TOMBSTONE_ELLIPSIS.search(source)
        assert hit is None, f"{rel} 自带省略号的第二份截断（#32 墓碑）：{hit.group(0)!r}"
        bare = _bare_slice_hits(tree)
        assert not bare, f"{rel} 退回裸切片（#32/#169 墓碑）：{bare}"
        concat = _tombstone_concat_hits(tree)
        assert not concat, f"{rel} 自带尾巴的第二份截断（#169 墓碑）：{concat}"
    print(f"✓ 8. daemon 渲染层 {len(DAEMON_RENDER_FILES)} 个文件无裸切片/无第二份截断实现")


def test_render_body_budget_is_prose_only():
    """段占位不占额度，截断必须带 `…` 与原长（#90 的可检出契约）。"""
    # 唤醒行的真实形态：引用占位 + @ + 一句长指令。正文本身超过 80 字——
    # 剥占位前 72 字符，剥完 79 字符正文才是额度的账（#32 报的就是：
    # 老代码按 72 切，正文只活到一半）。
    prose = ("记一下，这家店在西湖文化广场往北两百米的银泰二楼，"
             "主营手冲咖啡和自烘焙豆子，周末经常满座，"
             "另外他们家的瑰夏值得一试，记住周末别去太晚会排队，"
             "老板人很好可以续杯，店里有插座适合办公。")
    placeholder = "[引用 QQ 消息 123456789]"
    at_token = "@10001 "
    body = f"{placeholder}{at_token}{prose}"
    assert len(prose) > wg_text.WAKE_BODY_CHARS, len(prose)
    assert len(placeholder) + len(at_token) == 27, "占位+@ 应占 27 字符"

    text, truncated, original = wg_text.render_body(body, wg_text.WAKE_BODY_CHARS, 42)

    assert truncated is True, text
    assert text.endswith(f"（原 {len(body)} 字，--show 42 读全文）"), text
    assert "…" in text, text
    # 占位被换成短替代：`[引用]` 3 字符而不是 `[引用 QQ 消息 123456789]` 24 字符。
    assert "[引用]" in text and "[引用 QQ 消息" not in text, text
    # 80 字额度全花在正文上：结尾的店名和地址必须还在（issue 报的断点就在这）。
    assert "银泰二楼" in text and "自烘焙豆子" in text, text

    # 同样 80 字额度下，剥掉占位让正文多活了 27 个字符——#32 的直接度量。
    # clip() 会回落到句末，所以这里 >= 额度即算数（89 > 80：句末在额度之后）。
    plain, _, _ = wg_text.render_body(prose, wg_text.WAKE_BODY_CHARS)
    assert len(plain) >= wg_text.WAKE_BODY_CHARS, len(plain)

    # 原长按剥离前的整条 content 算：读者要能对着库里的行核对。
    assert original == len(body), (original, len(body))

    # 不超限时原样返回、不追加任何标记。
    whole, cut, size = wg_text.render_body("在吗", wg_text.WAKE_BODY_CHARS)
    assert (whole, cut, size) == ("在吗", False, 2), (whole, cut, size)

    # 额度非法即拒绝，而不是静默按 0 处理。
    try:
        wg_text.render_body("x", 0)
    except ValueError:
        pass
    else:
        raise AssertionError("render_body() must reject a non-positive limit")

    # 短显示（引用原文后缀）只加 `…`，不挂读全文指引。
    assert wg_text.render_fitted("甲" * 100, 60) == "甲" * 60 + "…"
    assert wg_text.render_fitted("短", 60) == "短"
    print("✓ 9. render_body 段占位不占额度，截断带 …/原长/--show 指引")


def test_show_event_reads_the_whole_message(tmp):
    """按 id 读全文：wg_events → chat_inbox → 明说没有，不再有直连 sqlite 的理由。"""
    long_body = "记一下：" + "这家店在西湖文化广场往北两百米的银泰二楼，" * 12
    assert len(long_body) > wg_text.WAKE_BODY_CHARS * 3

    # 第一路：wg_events.jsonl（唤醒流）。
    events = tmp / "wg_events.jsonl"
    events.write_text(json.dumps({
        "id": 7, "sender": "群友甲", "content": long_body,
        "timestamp": 1792759758000, "onebot_message_id": "1001",
        # 段在 = 有引用，这是 wg_quotes.render_event 认引用的前提（issue #94）。
        "segments": [{"type": "reply", "onebot_message_id": "900"}],
        "referenced_messages": [{"type": "reply", "onebot_message_id": "900",
                                 "state": "available", "sender": "群友乙",
                                 "content": "上一句原话", "timestamp": 1792759757000}],
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    buffer = io.StringIO()
    with patch.object(wg_event_ref, "EVENTS", events):
        with contextlib.redirect_stdout(buffer):
            code = wg_text.show_event(7)
    assert code == 0, code
    out = buffer.getvalue()
    assert long_body in out, out[:200]          # 全文，一个字符不少
    assert "上一句原话" in out, out              # 引用原文跟着展开

    # 第二路：chat_inbox 只读库（追问要读的闲聊不在 wg_events 里）。
    # 库文件用本用例专属名字：temp_dir 是全 suite 共享目录，别的用例也建表。
    db_path = tmp / "show-event.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE chat_inbox ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, payload TEXT NOT NULL)")
    conn.execute("INSERT INTO chat_inbox (session_id, payload) VALUES (?, ?)",
                 ("s1", json.dumps({"sender": "群友乙", "content": long_body,
                                    "timestamp": 1792759760000}, ensure_ascii=False)))
    conn.commit()
    conn.close()
    with patch.object(wg_event_ref, "DB", db_path):
        with contextlib.redirect_stdout(buffer):
            code = wg_text.show_event(1)
    assert code == 0, code
    assert long_body in buffer.getvalue(), buffer.getvalue()[:200]

    # 第三路：两条都没有时明说，不靠空输出表达失败。
    with patch.object(wg_event_ref, "EVENTS", tmp / "none.jsonl"), \
            patch.object(wg_event_ref, "DB", tmp / "none.sqlite3"):
        with contextlib.redirect_stdout(buffer):
            code = wg_text.show_event(999)
    assert code == 1, code
    assert "本地没有 id=999 的全文" in buffer.getvalue()
    print("✓ 10. --show 按 id 读全文：wg_events / chat_inbox / 明说没有")


def test_show_event_is_wired_into_reply_cli():
    """`reply.py --show <id>` 必须是真入口，不只是 helper 存在。"""
    import reply

    source = (ROOT / "water-group" / "daemon" / "reply.py").read_text(encoding="utf-8")
    assert '"--show"' in source, "reply.py 缺 --show 参数"
    assert "wg_text.show_event(args.show)" in source, "--show 没有接到 wg_text.show_event"
    print("✓ 11. reply.py --show 已接到 wg_text.show_event")


# ---------------------------------------------------------------------------
# issue #169 PA: 截断判据反例自检
# ---------------------------------------------------------------------------

# 判据反例夹具：每条都钉住「判据换成恒 True / 恒 False，用例必须随之翻转」，
# 照 check_module_size._self_test_body 的纪律。没有这一条，新增判据可能写成
# 「怎么跑都绿」，门禁就形同虚设。

# 裸切片判据（`_bare_slice_hits`）夹具。下界为空 + 上限非空才算截断切片：
# - `text[:80]` / `text[:limit]` / `text[:limit - 1]` 三种截断形态命中；
# - `text[pos:hit.start()]`（@ 边界切子串，下界非空）放过；
# - `text[pos:]`（取后缀，上限为空）放过。
_BARE_POSITIVE_INT = "x = text[:80]\n"        # #32 原症状：字面整数裸切片
_BARE_POSITIVE_VAR = "x = text[:limit]\n"     # #169：变量上限裸切片
_BARE_POSITIVE_BINOP = "x = text[:limit - 1]\n"  # #169：BinOp 上限裸切片
_BARE_NEGATIVE_SUBSTR = "x = text[pos:hit.start()]\n"  # 切子串，下界非空
_BARE_NEGATIVE_SUFFIX = "x = text[pos:]\n"    # 取后缀，上限为空
# 第二份截断判据（`_tombstone_concat_hits`）夹具。左操作数是截断切片 + 非空 str 尾巴：
_CONCAT_POSITIVE_VAR = 'x = text[:limit] + " …（已截断）"\n'  # #169：变量切片 + 非空尾巴
_CONCAT_POSITIVE_BINOP = 'x = text[:limit - 1] + "…"\n'       # #169：BinOp 切片 + 非空尾巴
_CONCAT_NEGATIVE_EMPTY = 'x = text[:limit] + ""\n'            # 空尾巴：无意义拼接，不是截断
_CONCAT_NEGATIVE_NONSTR = 'x = text[:limit] + 1\n'            # 非字符串尾巴：不是截断标记
_CONCAT_NEGATIVE_NONTEXT = 'x = items[:limit] + "…"\n'        # 切的不是正文（items）
_CONCAT_NEGATIVE_SUBSTR = 'x = text[pos:end] + "…"\n'         # 左边是子串切片，不是截断切片


def _judgement_hits(source, judge):
    return judge(ast.parse(source))


def _assert_set(hits, want_nonzero, label):
    if want_nonzero and not hits:
        raise AssertionError(f"{label}: 判据应该命中但返回空（判据失效，可能写成怎么跑都不抓）")
    if not want_nonzero and hits:
        raise AssertionError(f"{label}: 判据不该命中但抓到了（判据过宽，会误伤合法写法）：{hits}")


def test_bare_slice_judgement_ab():
    """#169 裸切片判据：字面整数 / 变量 / BinOp 三种上限都抓；非正文不抓。"""
    # 旧 #32 症状：字面整数裸切片仍命中（判据不能因为补了变量上限就漏掉旧的）。
    _assert_set(_judgement_hits(_BARE_POSITIVE_INT, _bare_slice_hits), True,
                "text[:80] 字面整数裸切片")
    # #169 症状：变量上限裸切片命中。
    _assert_set(_judgement_hits(_BARE_POSITIVE_VAR, _bare_slice_hits), True,
                "text[:limit] 变量上限裸切片")
    _assert_set(_judgement_hits(_BARE_POSITIVE_BINOP, _bare_slice_hits), True,
                "text[:limit - 1] BinOp 上限裸切片")
    print("✓ 12a. 裸切片判据命中字面整数/变量/BinOp 三种上限")


def test_bare_slice_judgement_negative():
    """#169 裸切片判据反例：子串切片（下界非空）与取后缀（上限为空）都不抓。"""
    _assert_set(_judgement_hits(_BARE_NEGATIVE_SUBSTR, _bare_slice_hits), False,
                'text[pos:hit.start()] 子串切片')
    _assert_set(_judgement_hits(_BARE_NEGATIVE_SUFFIX, _bare_slice_hits), False,
                'text[pos:] 取后缀')
    print("✓ 12b. 裸切片判据放过子串切片/取后缀（下界非空或上限为空不是截断）")


def test_tombstone_concat_judgement_ab():
    """#169 第二份截断判据：正文切片 + 非空字符串尾巴抓；空尾巴/非字符串/非正文不抓。"""
    _assert_set(_judgement_hits(_CONCAT_POSITIVE_VAR, _tombstone_concat_hits), True,
                'text[:limit] + " …（已截断）"')
    _assert_set(_judgement_hits(_CONCAT_POSITIVE_BINOP, _tombstone_concat_hits), True,
                'text[:limit - 1] + "…"')
    print("✓ 13a. 第二份截断判据命中变量/BinOp 切片 + 非空尾巴")


def test_tombstone_concat_judgement_negative():
    """#169 第二份截断判据反例：空尾巴、非字符串尾巴、切非正文变量、子串切片都不抓。"""
    _assert_set(_judgement_hits(_CONCAT_NEGATIVE_EMPTY, _tombstone_concat_hits), False,
                'text[:limit] + "" 空尾巴')
    _assert_set(_judgement_hits(_CONCAT_NEGATIVE_NONSTR, _tombstone_concat_hits), False,
                'text[:limit] + 1 非字符串尾巴')
    _assert_set(_judgement_hits(_CONCAT_NEGATIVE_NONTEXT, _tombstone_concat_hits), False,
                'items[:limit] + "…" 切非正文变量')
    _assert_set(_judgement_hits(_CONCAT_NEGATIVE_SUBSTR, _tombstone_concat_hits), False,
                'text[pos:end] + "…" 左边是子串切片')
    print("✓ 13b. 第二份截断判据放过空尾巴/非字符串/非正文/子串切片（不误伤合法拼接）")


def main():
    print("========================================================================")
    print("        [测试] issue #90 长文本截断：可检出，不静默")
    print("========================================================================")
    with temp_dir("tulpa-trunc-") as tmp:
        test_clip_contract()
        test_pending_rows_keeps_body(tmp)
        test_dump_keeps_body(tmp)
        test_summary_is_stored_whole(tmp)
        test_over_limit_event_is_marked(tmp)
        test_old_database_migrates(tmp)
        test_chat_reader_shares_the_limit()
        test_daemon_has_no_bare_slices()
        test_render_body_budget_is_prose_only()
        test_show_event_reads_the_whole_message(tmp)
        test_show_event_is_wired_into_reply_cli()
        test_bare_slice_judgement_ab()
        test_bare_slice_judgement_negative()
        test_tombstone_concat_judgement_ab()
        test_tombstone_concat_judgement_negative()
    print("========================================================================")
    print(f"✓ 全部 {11 + 4} 项截断契约测试通过！")
    print("========================================================================")


if __name__ == "__main__":
    main()
