"""Refresh member dossiers while the group is not talking to the bot.

`mention_only` keeps ordinary chatter out of the wake path, but every message
still lands in `chat_inbox`: Tulpa inserts each event before deciding whether to
deliver it. The raw material for a profile refresh is therefore already on disk --
this script only decides when there is enough of it and hands it over.

The trigger is deliberately boring. A burst of messages is enough on its own;
otherwise the caller has to be idle for a while *and* have a decent pile of
unread chatter. Both numbers live in `config.json` under `dossier` so they can be
tuned without touching code.

Usage:
  dossier.py --check            exit 0 when a refresh is due, 3 when it is not
  dossier.py --dump [N]         print the material, one JSON object per line
  dossier.py --mark             record everything up to now as summarised

The caller is expected to turn `--dump` into `consolidate_chat_memory` records and
then call `--mark`. Marking without summarising just loses the material, so it is
kept out of `--check`.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import winconsole  # noqa: E402  (stdout 编码 + 控制台代码页，一次收口)
from wg_paths import data_dir, state_dir
STATE = state_dir()
DB = data_dir() / "mcp-access.sqlite3"
MARK = STATE / "wg_dossier.json"
REPLIED = STATE / "wg_replied.jsonl"
CONFIG = ROOT / "water-group" / "config.json"

DEFAULTS = {
    "idle_minutes": 20,     # quiet this long before a refresh is considered
    "min_messages": 25,     # ...and at least this much unread chatter
    "burst_messages": 80,   # or this many messages regardless of how recent
    "max_material": 80,     # how many messages to hand over in one go
}

DUE = 0
NOT_YET = 3


def settings():
    cfg = dict(DEFAULTS)
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    for key, value in (data.get("dossier") or {}).items():
        if key in cfg and isinstance(value, (int, float)):
            cfg[key] = value
    return cfg


def connect():
    """Read-only connection; the backend owns every write."""
    return sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def active_session():
    """(session_id, conversation_id) of the session the collector is holding."""
    try:
        db = connect()
    except sqlite3.Error:
        return None, None
    try:
        row = db.execute(
            "SELECT id, conversation_id FROM chat_sessions "
            "WHERE active=1 ORDER BY rowid DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None, None
    finally:
        db.close()
    return (row[0], row[1]) if row else (None, None)


def load_mark():
    try:
        return json.loads(MARK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_mark(data):
    MARK.parent.mkdir(parents=True, exist_ok=True)
    MARK.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def idle_minutes():
    """Minutes since the last reply we sent, falling back to the last refresh."""
    latest = 0.0
    try:
        for line in REPLIED.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                latest = max(latest, float(json.loads(line).get("ts") or 0))
            except (ValueError, TypeError):
                continue
    except OSError:
        pass
    if not latest:
        latest = float(load_mark().get("last_run") or 0)
    if not latest:
        return float("inf")
    return (time.time() - latest) / 60


def pending_rows(session_id, after_id):
    """Messages worth profiling, newest last.

    ``text`` is the body verbatim. It used to be cut at 400 characters with no
    marker, which is how a 1524-character report reached the consolidation model
    as a half-sentence that looked complete (issue #90). Capacity belongs to the
    consumer, not the reader: ``--dump`` bounds the batch and the model bounds
    its own context. ``truncated`` stays in the payload anyway, so a caller can
    use the same "is this the whole message" contract the chat reader gives it.

    Self messages and synthetic notices are dropped: the bot quoting itself back
    teaches nothing about the group, and system events are not speech.
    """
    try:
        db = connect()
    except sqlite3.Error:
        return []
    try:
        rows = db.execute(
            "SELECT id, payload FROM chat_inbox WHERE session_id=? AND id>? ORDER BY id",
            (session_id, after_id)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    out = []
    for event_id, payload in rows:
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if data.get("is_self") or data.get("synthetic") or data.get("recalled"):
            continue
        text = (data.get("content") or "").strip()
        if not text:
            continue
        out.append({
            "id": event_id,
            "sender": data.get("sender") or "",
            "sender_id": str(data.get("sender_id") or ""),
            "text": text,
            "truncated": False,
            "ts": data.get("timestamp"),
        })
    return out


def cmd_check(cfg):
    session_id, _ = active_session()
    if not session_id:
        print("NO no active session")
        return NOT_YET
    rows = pending_rows(session_id, int(load_mark().get("last_event_id") or 0))
    count = len(rows)
    idle = idle_minutes()
    if count >= cfg["burst_messages"]:
        print(f"YES burst pending={count}")
        return DUE
    if count >= cfg["min_messages"] and idle >= cfg["idle_minutes"]:
        print(f"YES idle={idle:.0f}min pending={count}")
        return DUE
    print(f"NO idle={idle:.0f}min pending={count}")
    return NOT_YET


def cmd_dump(cfg, limit):
    """Oldest pending messages first.

    Taking the tail instead would leave the middle of a long backlog unread
    forever: `--mark` moves the cursor to the newest id, so anything the dump
    skipped is never offered again. Oldest-first lets a big backlog be chewed
    through a batch at a time.
    """
    session_id, _ = active_session()
    if not session_id:
        print("no active session", file=sys.stderr)
        return 1
    rows = pending_rows(session_id, int(load_mark().get("last_event_id") or 0))
    if limit and limit > 0:
        rows = rows[:limit]
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    return 0


def cmd_digest(limit=150):
    """Cheap half: hand recent chatter to the engine's rule-based digester.

    No model involved -- keyword hits and preference declarations only -- so it can
    run on every idle tick. It is idempotent: save_profile updates fields instead of
    appending, and a member whose tone_style is already set is left alone. The
    cursor is deliberately not advanced; that belongs to the model-backed path, so
    the same messages stay available for a deeper pass.
    """
    sys.path.insert(0, str(ROOT))
    try:
        from water_group.memory_engine import get_memory_engine
    except Exception as exc:  # noqa: BLE001
        print(f"engine unavailable: {type(exc).__name__}: {exc}")
        return 1
    session_id, conversation = active_session()
    if not session_id or not conversation:
        print("no active session")
        return 1
    group_id = conversation.split(":")[-1]
    rows = pending_rows(session_id, int(load_mark().get("last_event_id") or 0))
    if not rows:
        print("nothing to digest")
        return 0
    batch = [{"sender_id": r["sender_id"], "sender_name": r["sender"], "content": r["text"]}
             for r in rows[-limit:]]
    result = get_memory_engine().digest_idle_batch(group_id, batch)
    print(f"digested={result.get('digested_count')} updated={result.get('updated_members')}")
    return 0


def cmd_mark(through=None):
    session_id, _ = active_session()
    if not session_id:
        print("no active session", file=sys.stderr)
        return 1
    if through:
        top = int(through)
    else:
        try:
            db = connect()
            top = db.execute("SELECT MAX(id) FROM chat_inbox WHERE session_id=?",
                             (session_id,)).fetchone()[0] or 0
            db.close()
        except sqlite3.Error:
            return 1
    save_mark({"last_event_id": int(top), "last_run": int(time.time())})
    print(f"marked through event {top}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="在没人 @ 的时候沉淀群友画像")
    parser.add_argument("--check", action="store_true", help="该提炼了吗")
    parser.add_argument("--digest", action="store_true", help="规则版消化（不调模型，可随时跑）")
    parser.add_argument("--dump", nargs="?", const=0, type=int, metavar="N",
                        help="输出待提炼素材（text 为完整正文，不截断），可限制条数")
    parser.add_argument("--mark", action="store_true", help="标记到当前最新消息")
    parser.add_argument("--through", type=int, metavar="ID",
                        help="配合 --mark：只推进到这批素材的最后一条，便于分批处理积压")
    args = parser.parse_args()

    winconsole.ensure_utf8_stdio()

    cfg = settings()
    if args.mark:
        return cmd_mark(args.through)
    if args.digest:
        return cmd_digest()
    if args.dump is not None:
        return cmd_dump(cfg, args.dump or cfg["max_material"])
    if args.check:
        return cmd_check(cfg)
    parser.error("需要 --check / --dump / --mark 之一")


if __name__ == "__main__":
    sys.exit(main())
