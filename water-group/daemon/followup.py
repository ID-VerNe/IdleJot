"""追问窗口：回复之后继续听群里说什么，没人接话就收回。

`mention_only` 让 collector 只投递 @ 和关键词，所以「回复之后群里又聊了什么」
在 wg_events.jsonl 里根本不存在。但 chat_inbox 里有全部消息——后端在决定要不要
推送之前就已经把它们写进库了。追问窗口就走这条路：回复成功后开窗，watch.py 每轮
顺带查一眼窗口之后的新消息，有闲聊就地唤醒，让话题接着往下走；攒满
`max_messages` 条、或者静默超过 `idle_seconds` 秒没人接话，窗口自己关掉，回到只等
@ 和关键词的状态。

窗口期间不动 collector：它继续只投递 @，追问读的是全量库。两条唤醒路径各走各的
——@ 唤醒看 wg_events.jsonl，追问唤醒看 chat_inbox——互不覆盖，也就不会有
「collector 正卡在 170 秒长轮询里，改 mention_only 要等它返回才生效」的竞态。

`#` 指令不走 chat_inbox 的投递过滤（它是直接从库里捞的），所以 `consume()` 里还有
第二道闸门：`up.is_command()` 认出的指令不算接话——不扣额度、不唤醒、不续期，
但游标照常推进（跳过 ≠ 卡住，issue #70）。

状态文件（TULPA_WG_STATE 下）：
  wg_followup.json  当前窗口；文件不存在就是没开窗
字段：after_id（从这条之后开始看，通常就是刚回复的那条）、not_before（发送开始
的墙上时间，窗口只收这之后的发言，给 after_id 补一个向过去的下界）、session
（所属会话，换会话即失效，防会话重建后 id 空间对不上）、opened_at、until
（静默截止）、budget（还能跟几条，自己被自己说的话不占额度）、max_messages、
idle_seconds、wakes。

Usage:
  followup.py --show   打印当前窗口状态（调试用）
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
import winconsole  # noqa: E402  (stdout 编码 + 控制台代码页，一次收口)
import wg_clock  # noqa: E402  (「现在」的唯一出口，issue #95)
import wg_images  # noqa: E402  (图片标记渲染，issue #35 追问行同款)
import wg_quotes  # noqa: E402  (引用原文渲染，issue #94)
from wg_paths import data_dir, state_dir
STATE = state_dir()
DB = Path(os.environ.get("IDLEJOT_WG_DB") or os.environ.get("TULPA_WG_DB", data_dir() / "mcp-access.sqlite3"))
CONFIG = ROOT / "water-group" / "config.json"
WINDOW = STATE / "wg_followup.json"

# 跟不跟、跟多少、跟多久，都可以在 config.json 的 followup 段里改。
DEFAULTS = {"enabled": True, "max_messages": 10, "idle_seconds": 180}


def settings():
    """Config 的 followup 段，缺项用代码默认值补齐。"""
    data = dict(DEFAULTS)
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        raw = cfg.get("followup") or {}
        for key in DEFAULTS:
            if key in raw:
                data[key] = raw[key]
    except (OSError, ValueError):
        pass
    return data


def enabled():
    return bool(settings().get("enabled", True))


def load():
    try:
        data = json.loads(WINDOW.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # 写一半的脏文件可能是 list/str；watch.py 的循环没有 try 兜底，
    # 这种窗口宁可当没有，也不能把监听打死。
    return data if isinstance(data, dict) else None


def save(state):
    STATE.mkdir(parents=True, exist_ok=True)
    WINDOW.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def close():
    try:
        WINDOW.unlink()
    except OSError:
        pass


def active_session_id():
    """当前会话 id，读库不读 wg_session.txt——后者会滞后于会话重建。"""
    try:
        db = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return ""
    try:
        row = db.execute(
            "SELECT id FROM chat_sessions WHERE active=1 ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        db.close()
    return row[0] if row else ""


def open(after_id, max_messages=None, idle_seconds=None, session=None, not_before=None):
    """回复成功后开窗。after_id 通常就是刚回复的那条消息。

    故意不用「当前最新消息 id」当起点：发送过程中群里照旧在说话，拿最新 id 会把
    这几句吞掉。从被回复的那条往后看，它们才算「回复之后群里说的」。

    not_before 是发送开始的墙上时间，给锚点补一个向过去的下界：Agent 从拿到唤醒
    到真正把回复发出去，中间经常隔着十几分钟，这段积压的 id 都大于被回复的那条，
    只按 id 扫会把它们全算成接话，一次吃光 budget、窗口开出来就被自己关掉
    （issue #29）。带上时间下界后，窗口只覆盖「发送开始之后」的发言，等价于锚点
    取 max(after_id, 发送开始后的第一条)。
    """
    cfg = settings()
    if max_messages is None:
        max_messages = int(cfg["max_messages"])
    if idle_seconds is None:
        idle_seconds = int(cfg["idle_seconds"])
    now = time.time()
    state = {
        "after_id": int(after_id),
        "not_before": float(not_before or 0),
        "session": session or active_session_id(),
        "opened_at": now,
        "until": now + float(idle_seconds),
        "budget": int(max_messages),
        "max_messages": int(max_messages),
        "idle_seconds": int(idle_seconds),
        "wakes": 0,
    }
    save(state)
    return state


def stale(window, now=None):
    """窗口为什么该关了：None 表示还开着。"""
    if not window:
        return "gone"
    now = time.time() if now is None else now
    if window.get("session") and window["session"] != active_session_id():
        # 会话重建过，after_id 指的是旧会话的编号，接着看会对错人。
        return "session"
    if window.get("budget", 0) <= 0:
        return "budget"
    if now >= float(window.get("until") or 0):
        return "idle"
    return None


def since(after_id, session, not_before=None, limit=50):
    """窗口之后的全部消息，含自己说的（调用方按 is_self 过滤要不要唤醒）。

    只捞 id、payload 两列，跟 `reply.recent_chatter` 同一份库同一套字段；
    区别只在它是往前看而不是取最近 N 条。

    not_before 过滤掉发送开始前的历史（issue #29）。行数超过 limit 时循环分页
    读完：以前只取前 50 条，consume() 却把游标推到最后一行，第 51 条起会被
    静默跳过，之后再也不会被任何一轮看到。
    """
    if not session:
        return []
    try:
        db = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    limit = max(1, int(limit))
    sql = "SELECT id, payload, media FROM chat_inbox WHERE session_id=? AND id>?"
    params = [session, int(after_id or 0)]
    if not_before:
        sql += " AND at>=?"
        params.append(float(not_before))
    sql += " ORDER BY id LIMIT ?"
    out = []
    try:
        while True:
            rows = db.execute(sql, (*params, limit)).fetchall()
            if not rows:
                break
            for event_id, payload, raw_media in rows:
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                try:
                    media = json.loads(raw_media or "[]")
                except ValueError:
                    media = []
                out.append({
                    "id": event_id,
                    "sender": data.get("sender") or "",
                    # 追问行也要能取到群号：别名指向按群取覆盖行并集（issue #30）。
                    "conversation_id": data.get("conversation_id") or "",
                    # 闸门用原始文本：text 已经 collapse 空白，会把 " #ping" 洗成
                    # "#ping"；而 commands.command_key 要求行首、不 strip（#70）。
                    "raw": data.get("content") or "",
                    "text": " ".join((data.get("content") or "").split()),
                    "ts": data.get("timestamp"),
                    "self": bool(data.get("is_self")),
                    "synthetic": bool(data.get("synthetic")),
                    "recalled": bool(data.get("recalled")),
                    # 追问行和 @ 唤醒行一样要能看出引用原文（issue #94）。
                    "quote": wg_quotes.render_payload(db, session, data),
                    # 追问窗里收到的图也要有路径/补读提示，不能只剩裸 [图片]（issue #35）。
                    "images": wg_images.render(dict(data, id=event_id), media),
                })
            params[1] = rows[-1][0]
            if len(rows) < limit:
                break
    except sqlite3.Error:
        pass
    finally:
        db.close()
    return out


def consume(window, rows):
    """消化一批查询结果：返回该唤醒的闲聊，并原地更新窗口。

    自己说的话、系统提示和 `#` 指令也推进 after_id——不然每轮都要重新扫过它们
    ——但不占 budget、不唤醒：机器人自言自语不算接话，`#` 指令归 commands.py
    （issue #70）。
    有人接话就续期（until 推后）；这一批把 budget 扣到 0 就**当场收窗**，不留到
    下一轮 stale()（issue #98：以前那批照样唤醒，报告还写着「额度还剩 0 条」，
    Agent 白跑一趟）。最后一批照旧全部返回——不唤醒等于吞消息，比多一次往返更糟。
    """
    if not rows:
        return []
    # 延迟导入：up.report() 会 import followup，顶层互导会让 `python up.py --status`
    # 把 up 二次加载成 `__main__` 和 `up` 两份。
    import up  # noqa: PLC0415

    woke = [r for r in rows
            if not r["self"] and not r["synthetic"]
            and not up.is_command(r.get("raw") or r["text"])]
    window["after_id"] = max(r["id"] for r in rows)
    if woke:
        # 钳在 0：背着聊天记录回来时一批可能远超额度，那是「这段时间群里都在聊」，
        # 一次唤醒给全比拆成几轮更省事；额度只是窗口的上限，不是要透支成负数。
        window["budget"] = max(0, int(window.get("budget") or 0) - len(woke))
        window["until"] = time.time() + float(window.get("idle_seconds") or 180)
        window["wakes"] = int(window.get("wakes") or 0) + 1
    # 有没有唤醒都要落盘：整批都是 `#` 指令时 after_id 也必须推进，否则下一轮
    # 会把同一批重新捞出来（跳过 ≠ 卡住）。
    if int(window.get("budget") or 0) <= 0:
        # 额度用完：这一批就是最后一批，窗口立刻失效。留着它只会让下一轮再喊一次
        # 「额度还剩 0 条」（issue #98）。
        close()
    else:
        save(window)
    return woke


def wake_report(window, batch):
    """唤醒时给 Agent 看的那句报告（issue #98）。

    额度扣到 0 的那一批仍然是有效唤醒——它带着真实的接话，Agent 得看见；但报告
    不能再写「还剩 0 条」，那是自相矛盾的，读到的人只会以为窗口还有额度。
    """
    budget = int(window.get("budget") or 0)
    if budget <= 0:
        return f"[追问] 群里还在聊：本批 {int(batch)} 条，窗口到此为止（额度用完）"
    return (f"[追问] 群里还在聊：额度还剩 {budget} 条，"
            f"静默 {window.get('idle_seconds')}s 后收回")


def _group_of(payload):
    """(group_id, onebot_message_id) from an inbox payload, or None."""
    parts = str(payload.get("conversation_id") or "").split(":")
    if len(parts) != 3 or parts[1] != "group":
        return None
    message_id = payload.get("onebot_message_id")
    if not message_id:
        return None
    return parts[2], str(message_id)


def lookup(event_id):
    """任意事件的 (group_id, onebot_message_id)，闲聊也找得到。

    reply.py 以前只查 wg_events.jsonl，而追问唤醒要回的消息是被 mention_only
    滤掉的闲聊，压根不在那里面。chat_inbox 两条信息都有，拿它兜底，引用才挂得上去。
    id 是 AUTOINCREMENT 的全局 rowid，跨会话唯一，不用再按会话过滤。
    """
    try:
        db = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = db.execute("SELECT payload FROM chat_inbox WHERE id=?",
                         (int(event_id),)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    if not row:
        return None
    try:
        return _group_of(json.loads(row[0]))
    except ValueError:
        return None


def latest_group_id():
    """当前会话最新一条消息所在的群；没有会话时返回空串。"""
    sid = active_session_id()
    if not sid:
        return ""
    try:
        db = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return ""
    try:
        row = db.execute(
            "SELECT payload FROM chat_inbox WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (sid,)).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        db.close()
    if not row:
        return ""
    try:
        found = _group_of(json.loads(row[0]))
    except ValueError:
        return ""
    return found[0] if found else ""


def show():
    # 先给基准：「静默至 20:09:07」是未来时刻，没有现在就算不出还剩多久（issue #95）。
    print(wg_clock.now_line())
    window = load()
    if not window:
        print("追问窗口：未开窗")
        return 0
    reason = stale(window)
    if reason:
        print(f"追问窗口：过期（{reason}），下次监听路过时关掉")
    else:
        until = window.get("until") or 0
        left = wg_clock.seconds_left(until)
        left_text = f"，还剩 {left}s" if left is not None else ""
        print(f"追问窗口：进行中，从 id={window.get('after_id')} 之后看，"
              f"还可跟 {window.get('budget')} 条，静默至 "
              f"{wg_clock.clock(until)}{left_text}")
    return 0


def main():
    winconsole.ensure_utf8_stdio()
    if "--show" in sys.argv[1:]:
        return show()
    print(__doc__.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
