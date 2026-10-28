"""issue #163 gate: 沉淀的群友画像必须真的到水群 Agent 手里。

画像一直是活的（`member_profiles` / `member_events` / `member_group_memories`），
但只有 MCP 那条路在用：`chatlocal/mcp_chat.py` 的 `guidance()` 把 L1 卡片塞进
`packet['member_dossiers']`，产物落进 `wg_state.json` 的 `chat_guidance`——而
Agent 挂机时只有 daemon 三件套（`listen.py` / `reply.py` / `up.py --status`），
一个都不读它。本门禁钉住补上的读出口（`water-group/daemon/wg_dossiers.py`）：

  契约 1  发送者有档案 → 唤醒行带他的画像；**且不带别人的标记**（排除型断言，
          对照组是同一次唤醒里另一个人的行——一条卡对不对，不能只看它自己）。
  契约 2  正文点名的群友也给画像（`wg_aliases.body_targets` 的同一条命中规则）；
          发送者与点名是同一人时只出一张卡。
  契约 3  档案里没有的人发消息 → 不附画像（认不出就不指，与 #30 同一姿势）。
  契约 4  画像是一行、有硬顶、**不挤掉正文额度**（正文仍按 80 字裁并带 --show）。
  契约 5  群级术语每块只印一次，且不跨群泄露。
  契约 6  引擎不可用 → 唤醒行照常输出，不抛。
  契约 7  `reply.py --list` / `--context` 与唤醒行同一份口径：`--context` 只给
          唤醒行和 `--around` 那一行（每条闲聊都挂卡，那一屏就成了档案流）。
  契约 8  `dossier.py --notes` 写 → 读闭环，`--dry-run` 不落库。
  契约 9  判据完整性：把渲染函数换成恒空 / 恒满，契约 1-3 的判据必须随之翻转。

已知边界（issue #153 的教训：别写「任何错都红」）：本门禁只覆盖「发送者 / 正文」
两条取人路径和画像卡的裁剪口径，不覆盖 `wg_aliases` 内部的排序规则（长名字优先、
单字屏蔽、纯数字别名不参与正文命中）——那些由 `check_alias_visibility` 钉。固定
夹具也抓不到「卡片内容本身写得不准」，它只保证字段来自档案库、顺序与上限对。

    .venv/Scripts/python.exe scripts/check_wake_dossier.py
"""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import scratch_dir  # noqa: E402

TMP = scratch_dir(prefix="wg_dossier_")
HOME = TMP / "home"
STATE = HOME / "state"
STATE.mkdir(parents=True, exist_ok=True)

# 数据根全部钉到临时目录：记忆库、状态、会话库、config 都不碰本机真身。
os.environ["WATER_GROUP_HOME"] = str(HOME)
os.environ["IDLEJOT_WG_CONFIG"] = str(HOME / "config.json")
os.environ["IDLEJOT_WG_MEMORY_DB"] = str(TMP / "mem.sqlite3")
os.environ["TULPA_WG_STATE"] = str(STATE)
os.environ["IDLEJOT_WG_STATE"] = str(STATE)
os.environ["TULPA_WG_DB"] = str(TMP / "chats.sqlite3")
os.environ["IDLEJOT_WG_DB"] = str(TMP / "chats.sqlite3")

fs.bootstrap_paths(scripts=False)

import wg_dossiers  # noqa: E402
import wg_text  # noqa: E402
from water_group.memory_engine import get_memory_engine  # noqa: E402

ME = "999000111"
GROUP = "777000111"
OTHER_GROUP = "888000222"
CID = f"{ME}:group:{GROUP}"

# 甲：有主名 / 外号 / 兴趣 / 记忆 / 本群事件——契约 1 的正样本。
# 乙：另一个有档案的人——契约 1 的**对照组**（他的标记不该出现在甲的行里）。
# 丙：档案库里没有——契约 3 的负样本。
# 丁：自己没有档案，但正文点得到甲——契约 2 的入口。
# 戊：一条 168 字长正文 + 同一人既是发送者又被点名——契约 2b / 契约 4。
NOW = int(time.time() * 1000)


def build():
    engine = get_memory_engine()
    engine.save_profile("10001", "群友甲", aliases=["甲哥", "老甲"],
                        tone_style="嘴贫", interests=["听八卦", "打游戏"],
                        key_memories=["上周说要戒奶茶，第二天就破功"])
    engine.record_event(user_id="10001", group_id=GROUP, quote="我又喝奶茶了",
                        context="又破戒了")
    engine.save_profile("10002", "群友乙", aliases=["乙总"], tone_style="稳重",
                        interests=["炒股"], key_memories=["亏了三千"])
    # 己：第三个人。正文同时点名甲乙己时，`MAX_PEOPLE` 必须咬住（契约 2c）。
    engine.save_profile("10004", "群友己", aliases=["己哥"], interests=["健身"])
    engine.upsert_group_notes(GROUP, [{"term": "遥遥领先", "value": "自嘲用法"}])
    # 别的群的术语：契约 5 的跨群隔离对照。
    engine.upsert_group_notes(OTHER_GROUP, [{"term": "别群黑话", "value": "不该出现"}])

    cfg = json.loads((ROOT / "water-group" / "config.example.json").read_text(encoding="utf-8"))
    cfg.update({"self_account": ME, "onebot_http_url": "http://127.0.0.1:1",
                "onebot_ws_url": "ws://127.0.0.1:1", "onebot_token": "t"})
    cfg.setdefault("pending_reply", {})["max_age_seconds"] = 3600
    # issue #164：`wg_isolation` 会把 `groups` 当群身份用——config.example.json
    # 自带一个示例群（`123456789`），而本夹具的事件全在 `GROUP`（777000111）里。
    # 不覆盖的话「config 里唯一启用群」这一档解析出示例群，读侧过滤把夹具事件全部
    # 滤掉，本文件 11 条 `watch`/`--list` 用例集体红在「输出里没有那一行」。
    # 这里把 groups 换成夹具自己的群：门禁测的是画像渲染，群身份必须与事件一致。
    cfg["groups"] = {GROUP: {"enabled": True}}
    (HOME / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    long_body = "很长的正文" * 30
    # 事件 id 与 chat_inbox 的 rowid 是同一个空间（`show_event` 就靠这个回退读
    # 全文），所以 `--context` 的「哪几行是唤醒」必须与这里的 id 对齐：id 1 同时
    # 是 chat_inbox 第一行和一条唤醒，10 以后只存在于 wg_events 里。
    events = [
        _wake(1, "群友甲", "在吗，问个事"),
        _wake(10, "路人丙", "我是没有档案的人"),
        _wake(11, "群友乙", "我在"),
        _wake(12, "路人丁", "老甲 你看这个"),
        _wake(13, "群友甲", "老甲 我来了"),
        _wake(14, "群友甲", long_body),
    ]
    (STATE / "wg_events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")

    chats = sqlite3.connect(TMP / "chats.sqlite3")
    chats.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
    chats.execute("CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id TEXT,"
                  " message_id TEXT, payload TEXT, media TEXT NOT NULL DEFAULT '[]',"
                  " details TEXT NOT NULL DEFAULT '[]', delivered INTEGER NOT NULL DEFAULT 0)")
    chats.execute("INSERT INTO chat_sessions VALUES ('s1', ?, 1)", (CID,))
    for index, (sender, text) in enumerate([
            ("群友甲", f"@{ME} 在吗"),      # 1 唤醒行
            ("群友乙", "甲哥今天又喝奶茶"),   # 2 普通闲聊（不带画像）
            ("群友甲", "老甲别说了"),        # 3 普通闲聊
            ("路人丙", "散了吧"),            # 4 普通闲聊
    ], start=1):
        payload = {"sender": sender, "content": text, "timestamp": NOW + index * 1000,
                   "conversation_id": CID}
        chats.execute("INSERT INTO chat_inbox (id, session_id, message_id, payload)"
                      " VALUES (?, 's1', ?, ?)",
                      (index, f"m{index}", json.dumps(payload, ensure_ascii=False)))
    chats.commit()
    chats.close()


def _wake(event_id, sender, body):
    return {"id": event_id, "timestamp": NOW, "sender": sender,
            "content": f"@{ME} {body}", "segments": [{"type": "at", "qq": ME}],
            "conversation_id": CID}


WATCH_CACHE = {}


def run_watch(env_override=None):
    """跑一次真实 `watch.py`，返回它的 stdout。

    每跑一次先删游标：`watch.py` 成功后会推进 `wg_watch.txt`，第二次就只看得到
    「没有新唤醒」，后面的契约会集体变成假绿（空输出里当然没有别人的标记）。
    """
    try:
        (STATE / "wg_watch.txt").unlink()
    except OSError:
        pass
    env = dict(os.environ)
    env.update(env_override or {})
    proc = subprocess.run(
        [sys.executable, str(ROOT / "water-group" / "daemon" / "watch.py"),
         "--timeout", "1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        timeout=120,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def watch_out():
    """契约 1-5 共用的那一次唤醒输出（跑一次，五个断言各看一段）。"""
    if "out" not in WATCH_CACHE:
        WATCH_CACHE["out"] = run_watch()
    return WATCH_CACHE["out"]


def line_with(text, marker):
    return next((ln for ln in text.splitlines() if ln.startswith(marker)), "")


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---------- 契约 1：发送者的画像进了唤醒行，且没串到别人身上 ----------

@case
def 唤醒行带发送者画像():
    code, out = watch_out()
    line = line_with(out, "id=1 ")
    return (code == 0 and "画像 群友甲(10001)" in line
            and "外号 甲哥/老甲" in line and "记 上周说要戒奶茶" in line)


@case
def 画像不串到别人身上():
    """排除型断言 + 对照组：甲的行里不许有乙的标记，乙自己的行里必须有。"""
    code, out = watch_out()
    mine, theirs = line_with(out, "id=1 "), line_with(out, "id=11 ")
    return (code == 0
            and "乙总" not in mine and "亏了三千" not in mine
            and "画像 群友乙(10002)" in theirs)


@case
def 本群八卦在卡里():
    code, out = watch_out()
    return code == 0 and "本群 又破戒了" in line_with(out, "id=1 ")


# ---------- 契约 2：正文点名的人，以及「同一个人只出一张卡」 ----------

@case
def 正文点名的人也有画像():
    """丁自己没档案，正文点到「老甲」——那一行要能指着甲说话。"""
    code, out = watch_out()
    line = line_with(out, "id=12 ")
    return code == 0 and "画像 群友甲(10001)" in line


@case
def 同一个人只出一张卡():
    """发送者就是被点名的那个人：卡片只能有一张（不是两张同款）。"""
    code, out = watch_out()
    line = line_with(out, "id=13 ")
    return code == 0 and line.count("画像 群友甲") == 1


@case
def 最多两个人():
    """正文一次点三个有档案的人：只出 `MAX_PEOPLE` 张卡（唤醒行不是档案流）。"""
    cards = wg_dossiers.render_for_event("路人丙", "老甲 乙总 己哥 都在", GROUP)
    named = sum(("群友甲" in c or "群友乙" in c or "群友己" in c) for c in cards)
    return len(cards) == named == wg_dossiers.MAX_PEOPLE


# ---------- 契约 3：认不出就不附 ----------

@case
def 没有档案的人不附画像():
    code, out = watch_out()
    line = line_with(out, "id=10 ")
    return code == 0 and line.strip() != "" and "画像" not in line


# ---------- 契约 4：一行、有顶、不挤正文 ----------

@case
def 画像卡是一行且有硬顶():
    card = wg_dossiers.card_for("10001", GROUP)
    # 硬顶之上只允许那一个省略号（`render_fitted` 的截断标记）。
    return bool(card) and "\n" not in card and len(card) <= wg_dossiers.CARD_CHARS + 1


@case
def 超长记忆会被截断():
    engine = get_memory_engine()
    engine.save_profile("10003", "群友庚", aliases=["庚子"],
                        key_memories=["这是一个非常非常长的记忆条目，用来确认截断真的会发生，"
                                      "而不是把整段塞进唤醒行里把别的信息挤掉"])
    try:
        card = wg_dossiers.card_for("10003", GROUP)
    finally:
        conn = sqlite3.connect(TMP / "mem.sqlite3")
        conn.execute("DELETE FROM member_profiles WHERE user_id='10003'")
        conn.commit()
        conn.close()
        wg_dossiers._CACHE.pop(("10003", GROUP), None)
    return card.endswith("…") and len(card) <= wg_dossiers.CARD_CHARS + 1


@case
def 正文额度没被画像挤掉():
    """长正文行：正文仍是 80 字 + `--show` 指引，画像另起一段挂在后面。"""
    code, out = watch_out()
    line = line_with(out, "id=14 ")
    expected, _, _ = wg_text.render_body(f"@{ME} " + "很长的正文" * 30,
                                        wg_text.WAKE_BODY_CHARS, 14)
    return (code == 0 and expected in line
            and f"--show 14 读全文" in line and "画像 群友甲(10001)" in line)


# ---------- 契约 5：群级术语一次一块、不跨群 ----------

@case
def 群级术语每块只印一次():
    code, out = watch_out()
    return code == 0 and out.count("【群级术语】") == 1


@case
def 群级术语不跨群泄露():
    code, out = watch_out()
    return (code == 0 and "遥遥领先=自嘲用法" in out
            and "别群黑话" not in out)


# ---------- 契约 6：引擎不可用照常输出 ----------

@case
def 引擎不可用不拖垮唤醒行():
    """记忆库路径指成一个目录：引擎起不来，唤醒行必须还在，画像为空。"""
    broken = TMP / "broken-mem"
    broken.mkdir(exist_ok=True)
    code, out = run_watch({"IDLEJOT_WG_MEMORY_DB": str(broken)})
    line = line_with(out, "id=1 ")
    return (code == 0 and "WAKE" in out and line.strip() != ""
            and "画像" not in out)


# ---------- 契约 7：--list / --context 与唤醒行同一份口径 ----------

@case
def list_带画像与术语():
    import reply

    wg_dossiers._CACHE.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = reply.list_wakes()
    text = buf.getvalue()
    return (code == 0 and "画像 群友甲(10001)" in line_with(text, "id=1 ")
            and text.count("【群级术语】") == 1)


@case
def context_只给唤醒行和_around_挂画像():
    import reply

    wg_dossiers._CACHE.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = reply.show_context(10)
    text = buf.getvalue()
    wakes = line_with(text, "1 ")
    chatter = line_with(text, "3 ")
    return (code == 0 and "画像 群友甲(10001)" in wakes
            and "画像" not in chatter)


@case
def context_around_那一行也挂画像():
    import reply

    wg_dossiers._CACHE.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = reply.show_context(10, around=2)
    line = line_with(buf.getvalue(), "2 ")
    return code == 0 and "回这条" in line and "画像 群友甲(10001)" in line


# ---------- 契约 8：dossier.py --notes 写读闭环 ----------

@case
def notes_写读闭环():
    import dossier

    payload = TMP / "notes.json"
    payload.write_text(json.dumps([{"term": "老王", "value": "隔壁店老板"}],
                                  ensure_ascii=False), encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        wrote = dossier.cmd_notes(str(payload), GROUP)
        read_text = dossier.cmd_notes(None, GROUP)
        read_json = dossier.cmd_notes(None, GROUP, as_json=True)
    out = buf.getvalue()
    return (wrote == 0 and read_text == 0 and read_json == 0
            and "老王=隔壁店老板" in out and '"老王"' in out)


@case
def notes_dry_run_不落库():
    import dossier

    payload = TMP / "notes-dry.json"
    payload.write_text(json.dumps([{"term": "只预演", "value": "不该入库"}],
                                  ensure_ascii=False), encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = dossier.cmd_notes(str(payload), GROUP, dry_run=True)
    notes = get_memory_engine().query_group_notes(GROUP)
    return code == 0 and "只预演" not in [n["term"] for n in notes]


@case
def notes_命令行可用():
    """从命令行真跑一次：`--notes` 是写侧入口能对上 argv 的唯一证据。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "water-group" / "daemon" / "dossier.py"),
         "--notes", "--group", GROUP, "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ), timeout=120)
    return (proc.returncode == 0 and "老王" in proc.stdout
            and proc.stderr.strip() == "")


@case
def who_给得下完整卡():
    """`--who` 是专门问一个人：额度比唤醒行宽，但仍是一行、仍有顶。"""
    card = wg_dossiers.who_card("10001", GROUP)
    wake = wg_dossiers.card_for("10001", GROUP)
    return (card.startswith(wake[:10]) and "\n" not in card
            and len(card) <= wg_dossiers.WHO_CARD_CHARS + 1)


# ---------- 契约 9：判据完整性（恒空 / 恒满都要翻转） ----------

@case
def 判据随渲染函数翻转():
    """契约 3 的判据（没档案 → 没画像）必须真的由渲染函数决定，而不是恒真。

    两向各一次：恒满时「没档案的人也长出画像」（判据翻成 False），恒空时
    「有档案的人也没有画像」。任一方向不翻转，说明断言盯错了对象。
    """
    real = wg_dossiers.render_for_event
    try:
        wg_dossiers.render_for_event = lambda *a, **k: ["画像 假人"]
        full = not _no_card_for_stranger()
        wg_dossiers.render_for_event = lambda *a, **k: []
        empty = "画像" in wg_dossiers.anchors("群友甲", "老甲 在吗", GROUP)[2]
    finally:
        wg_dossiers.render_for_event = real
    return full and not empty and _no_card_for_stranger()


def _no_card_for_stranger():
    return "画像" not in wg_dossiers.anchors("路人丙", "随便说说", GROUP)[2]


def main():
    return fs.run_cases(CASES, setup=build, prefix=False)


if __name__ == "__main__":
    sys.exit(main())
