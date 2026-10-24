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
  dossier.py --ingest FILE      write one summary into the group timeline
  dossier.py --apply FILE.json  write a batch of member dossiers / events

`--ingest` and `--apply` exist so a refresh does not need a hand-written script
per batch: the payload is data, the plumbing lives here (issue #6). Both take `-`
for stdin, accept `--through` to advance the cursor in the same call, and refuse
to write when the payload cannot be applied cleanly.

The #19 write-boundary fix stops new pollution; these verbs deal with what it
left behind (issue #34):

  dossier.py --audit-pollution            read-only scale report, writes nothing
  dossier.py --scrub-pollution            drop group-owned facts from the cross-group rows
  dossier.py --restore-pollution          put a scrub back, from key_memories_backup

All three take --json for a machine-readable report, and the two writers take
--dry-run. --scrub-pollution removes only what the group snapshot already holds,
so no group's memory view changes; the cross-group row is what shrinks.

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


def engine():
    """In-process engine, same route as `--digest` (`:202`).

    Going through MCP would need a minted grant and a running stack; a
    consolidation pass is exactly the moment when neither is guaranteed. The
    price is one import (~300ms), paid only by the two writing verbs.
    """
    sys.path.insert(0, str(ROOT))
    from water_group.memory_engine import get_memory_engine
    return get_memory_engine()


def event_ceiling():
    """One memory-event field's ceiling, taken from its single home (issue #90)."""
    sys.path.insert(0, str(ROOT))
    from chatlocal.text_limits import EVENT_TEXT_CHARS
    return EVENT_TEXT_CHARS


def resolve_group(explicit=None, payload_group=None):
    """`--group` > payload > the active session's conversation tail.

    Same derivation `cmd_digest` uses, so a caller never has to learn a second
    rule for "which group am I writing to".
    """
    for candidate in (explicit, payload_group):
        value = str(candidate or "").strip()
        if value:
            return value
    _, conversation = active_session()
    return conversation.split(":")[-1] if conversation else ""


def read_payload(source):
    """Read a payload verbatim; `-` means stdin. Raises OSError / ValueError."""
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8")


def cmd_ingest(source, group, through=None, dry_run=False):
    """One summary → one group-level event, then optionally advance the cursor.

    The ceiling is refused, not silently applied: a summary that loses its tail
    is the #90 bug class, and the CLI is the right place to ask for a split
    because the caller still has the original text in hand.
    """
    try:
        text = read_payload(source).strip()
    except OSError as exc:
        print(f"cannot read {source}: {exc}", file=sys.stderr)
        return 2
    if not text:
        print("empty payload", file=sys.stderr)
        return 2
    gid = resolve_group(group)
    if not gid:
        print("no group id: pass --group, or bind a session first", file=sys.stderr)
        return 2
    ceiling = event_ceiling()
    if len(text) > ceiling:
        print(f"payload {len(text)} chars exceeds {ceiling}; split it into batches",
              file=sys.stderr)
        return 2
    if dry_run:
        print(f"would ingest chars={len(text)} group={gid} through={through or '-'}")
        return 0
    result = engine().consolidate_chat_memory(group_id=gid, summary=text)
    events = result.get("recorded_events_count", 0)
    print(f"ingested chars={len(text)} events={events} group={gid}")
    if through:
        # Only after a successful write: marking first is how material gets lost.
        return cmd_mark(through)
    return 0


MEMBER_FIELDS = {
    "identifier", "key_memories", "traits", "aliases", "interests",
    "tone_style", "role", "primary_name", "overwrite", "event",
}


def plan_apply(payload, group):
    """Validate a payload. Returns (group_id, members, events, summary, errors)."""
    errors = []
    if not isinstance(payload, dict):
        return "", [], [], None, ["payload must be a JSON object"]
    gid = resolve_group(group, payload.get("group_id"))
    members = payload.get("members") or []
    events = payload.get("events") or []
    summary = payload.get("summary")
    if not gid:
        errors.append("no group id: pass --group, or put group_id in the payload")
    if not isinstance(members, list):
        errors.append("members must be a list")
        members = []
    for index, item in enumerate(members):
        if not isinstance(item, dict):
            errors.append(f"members[{index}] must be an object")
            continue
        if not str(item.get("identifier") or "").strip():
            errors.append(f"members[{index}] needs identifier")
        unknown = set(item) - MEMBER_FIELDS
        if unknown:
            # Fail closed: a typo in a field name would otherwise look like a
            # successful write that changed nothing.
            errors.append(f"members[{index}] has unknown fields: {sorted(unknown)}")
    if not isinstance(events, list):
        errors.append("events must be a list")
        events = []
    if summary is not None and len(str(summary)) > event_ceiling():
        errors.append(f"summary exceeds {event_ceiling()} chars; split it")
    return gid, members, events, summary, errors


def cmd_apply(source, group, dry_run=False):
    """Batch member dossiers (+ optional events / summary) from a JSON payload.

    Members go straight to `update_member_dossier` with an explicit `group_id`:
    the `records` path inside `consolidate_chat_memory` drops the group id
    (`memory_engine.py:1411-1421`), which would land group-scoped facts in the
    cross-group row and break the #5 write boundary.
    """
    try:
        payload = json.loads(read_payload(source))
    except OSError as exc:
        print(f"cannot read {source}: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"payload is not valid JSON: {exc}", file=sys.stderr)
        return 2
    gid, members, events, summary, errors = plan_apply(payload, group)
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 2
    if dry_run:
        names = [str(item.get("identifier")) for item in members]
        print(f"would apply group={gid} members={len(names)} events={len(events)} "
              f"summary={bool(summary)}")
        for name in names:
            print(f"  member {name}")
        return 0
    mem = engine()
    failed = 0
    for item in members:
        fields = dict(item)
        event = fields.pop("event", None)
        result = mem.update_member_dossier(group_id=gid, **fields)
        if not result.get("success"):
            failed += 1
            print(f"member FAIL {item.get('identifier')}: {result.get('error')}",
                  file=sys.stderr)
            continue
        print(f"member ok {result.get('primary_name')} scope={result.get('memory_scope')}")
        if isinstance(event, dict) and event.get("quote"):
            mem.record_event(
                user_id=result["user_id"],
                group_id=gid,
                quote=event.get("quote", ""),
                context=event.get("context", ""),
                source=event.get("source", "consolidation"),
                tags=event.get("tags") or [],
            )
    if events or summary:
        result = mem.consolidate_chat_memory(group_id=gid, events=events, summary=summary)
        print(f"events={result.get('recorded_events_count', 0)} group={gid}")
    if failed:
        print(f"{failed} member(s) failed", file=sys.stderr)
        return 1
    return 0


def emit(report, as_json, lines):
    """One report, two shapes: --json for scripts, text for a terminal."""
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for line in lines:
            print(line)


def cmd_audit_pollution(as_json=False):
    """Read-only scale report for the pre-#19 cross-group pollution (issue #34)."""
    report = engine().audit_pollution()
    groups = "/".join(report["groups"]) or "-"
    lines = [
        "=== 跨群主档污染审计（只读，未写库） ===",
        f"profile 行数 {report['profiles_total']}，有覆盖行 {report['members_with_overlay']}，"
        f"无覆盖行 {report['no_overlay_members']}，涉及群 {groups}",
        f"主档记忆总数 {report['global_memories_total']}，其中覆盖行已持有（可证明污染）"
        f"{report['provable_pollution_total']} 条",
        f"其中在 scope 内、可安全剔除 {report['scrubbable_total']} 条",
        f"仅主档持有（保守保留）{report['global_only_total']} 条，"
        f"其中有覆盖行群友的 {report['global_only_with_overlay_total']} 条（可人工复核是否并入覆盖行）",
        f"越 scope 群友（跳过不清理）{report['out_of_scope_members']} 位",
    ]
    affected = [m for m in report["members"] if m["overlay_groups"]]
    if affected:
        lines.append("有覆盖行的群友：")
        for m in affected:
            flag = "" if m["in_scope"] else "  [越 scope，跳过]"
            lines.append(
                f"  {m['primary_name']} [{ '/'.join(m['overlay_groups']) }] "
                f"主档 {m['global_memories']} / 可剔 {m['provable_pollution']} / "
                f"保留 {m['global_only']}{flag}"
            )
    emit(report, as_json, lines)
    return 0


def cmd_scrub_pollution(as_json=False, dry_run=False):
    report = engine().scrub_pollution(dry_run=dry_run)
    verb = "将剔除" if dry_run else "已剔除"
    lines = [
        f"=== 跨群主档污染清理（{'预演，未写库' if dry_run else '已写库'}） ===",
        f"{verb}群友 {report['scrubbed_members']} 位，"
        f"剔除 {report['removed_total']} 条，保留 {report['kept_total']} 条",
    ]
    for s in report["skipped"]:
        lines.append(f"  跳过 {s['primary_name']}：{s['reason']}（{s['would_remove']} 条待人工确认）")
    for m in report["members"]:
        lines.append(
            f"  {m['primary_name']} 剔除 {m['removed']} 条 / 保留 {m['kept']} 条"
            + ("（覆盖了旧备份）" if m["backup_overwritten"] else "")
        )
    if not dry_run and report["scrubbed_members"]:
        lines.append("原主档列表已写入 key_memories_backup，--restore-pollution 可还原。")
    emit(report, as_json, lines)
    return 0


def cmd_restore_pollution(as_json=False, dry_run=False):
    report = engine().restore_pollution(dry_run=dry_run)
    verb = "将还原" if dry_run else "已还原"
    lines = [
        f"=== 还原跨群主档（{'预演，未写库' if dry_run else '已写库'}） ===",
        f"{verb}群友 {report['restored_members']} 位，共 {report['restored_total']} 条",
    ]
    for m in report["members"]:
        lines.append(f"  {m['primary_name']} 还原 {m['restored_memories']} 条")
    emit(report, as_json, lines)
    return 0


def main():
    parser = argparse.ArgumentParser(description="在没人 @ 的时候沉淀群友画像")
    parser.add_argument("--check", action="store_true", help="该提炼了吗")
    parser.add_argument("--digest", action="store_true", help="规则版消化（不调模型，可随时跑）")
    parser.add_argument("--dump", nargs="?", const=0, type=int, metavar="N",
                        help="输出待提炼素材（text 为完整正文，不截断），可限制条数")
    parser.add_argument("--mark", action="store_true", help="标记到当前最新消息")
    parser.add_argument("--through", type=int, metavar="ID",
                        help="配合 --mark / --ingest：只推进到这批素材的最后一条，便于分批处理积压")
    parser.add_argument("--ingest", metavar="FILE",
                        help="把一段正文作为群级沉淀入库；- 读 stdin")
    parser.add_argument("--apply", metavar="JSON",
                        help="按 JSON 载荷批量写画像/事件；- 读 stdin")
    parser.add_argument("--audit-pollution", action="store_true",
                        help="只读审计 #19 修复前落库的跨群主档污染规模（issue #34）")
    parser.add_argument("--scrub-pollution", action="store_true",
                        help="剔除主档中覆盖行已持有的群内事实，原列表入 key_memories_backup")
    parser.add_argument("--restore-pollution", action="store_true",
                        help="按 key_memories_backup 还原上一次 scrub")
    parser.add_argument("--json", action="store_true", help="机器可读输出（配合上面三个动词）")
    parser.add_argument("--group", metavar="ID",
                        help="群号；缺省取活动会话或载荷里的 group_id")
    parser.add_argument("--dry-run", action="store_true", help="只校验并打印计划，不写库")
    args = parser.parse_args()

    winconsole.ensure_utf8_stdio()

    cfg = settings()
    if args.mark:
        return cmd_mark(args.through)
    if args.ingest is not None:
        return cmd_ingest(args.ingest, args.group, args.through, args.dry_run)
    if args.apply is not None:
        return cmd_apply(args.apply, args.group, args.dry_run)
    if args.audit_pollution:
        return cmd_audit_pollution(args.json)
    if args.scrub_pollution:
        return cmd_scrub_pollution(args.json, args.dry_run)
    if args.restore_pollution:
        return cmd_restore_pollution(args.json, args.dry_run)
    if args.digest:
        return cmd_digest()
    if args.dump is not None:
        return cmd_dump(cfg, args.dump or cfg["max_material"])
    if args.check:
        return cmd_check(cfg)
    parser.error("需要 --check / --dump / --mark / --digest / --ingest / --apply / "
                 "--audit-pollution / --scrub-pollution / --restore-pollution 之一")


if __name__ == "__main__":
    sys.exit(main())
