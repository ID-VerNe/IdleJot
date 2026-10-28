"""Refresh member dossiers while the group is not talking to the bot.

`mention_only` keeps ordinary chatter out of the wake path, but every message
still lands in `chat_inbox`: Tulpa inserts each event before deciding whether to
deliver it. The raw material for a profile refresh is therefore already on disk --
this script only decides when there is enough of it and hands it over.

The trigger is deliberately boring, and there are three of them, checked in this
order (issue #183): consecutive listening rounds nobody named us in (the primary
path, issue #162), a *recent* burst of messages, then the old idle-and-backlog
pair. Quiet goes first because it is the only path that says why we are being
woken; a burst only says "a lot arrived", and when it is measured against the
whole backlog it is true forever once the cursor stalls. So burst counts only
the last `burst_window_minutes` -- a real spike, not an old pile.

Being woken is itself a state (issue #183). `--check` records nothing, so the
caller does: after a DUE, `--tick due` stores `due_event_id` / `due_at`, and
`--check` answers `NO due-hold` for the same batch until either new material
arrives, the caller summarises it (`--mark`), or `--digest --reap` releases the
cursor. Without that, a cursor nobody advances makes every round announce the
same 400 messages again, which is indistinguishable from a real backlog.

Every number lives in `config.json` under `dossier` so it can be tuned without
touching code.

Usage:
  dossier.py --check            exit 0 when a refresh is due, 3 when it is not
  dossier.py --dump [N]         print the material, one JSON object per line
  dossier.py --mark             record everything up to now as summarised
  dossier.py --tick [round|reset|due]  count a quiet round / clear / record a DUE
  dossier.py --digest [--reap]  rule-based pass; --reap may release a stalled cursor
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

The quiet cadence (issue #162) needs a counter that outlives one process: each
`listen.py --cycles 1` call runs exactly one round, so "three rounds in a row"
spans three invocations. `--tick` writes that counter into the same mark file and
`--mark` / a wake clears it. `quiet_rounds` is the primary test ("three times in
a row"), `quiet_minutes` is only a floor -- `--timeout 30` makes three rounds 90
seconds, and pinning rounds alone would turn "about five minutes" into a lie.
`quiet_min_messages` keeps an empty group from burning a model call on nothing.

`save_mark` is read-modify-write for exactly this reason: the old whole-object
overwrite meant `--mark` erased whatever a new key had accumulated. The symptom
was either "due again right after a refresh" or "the counter never grows", and
both look intermittent.

Who advances `last_event_id` (issue #183) -- one owner, one fallback:

* the model-backed pass, via `--mark`, after `--dump` was actually summarised.
  This is the only authoritative advance: the material was read, not just
  skimmed.
* `--digest --reap` is the fallback, and only after `reap_after_minutes` have
  passed since a DUE nobody acted on. It advances to the last event this rule
  pass covered, so the backlog stops lying about being "about to be summarised".
  The rows stay in `chat_inbox`; only the pending accounting moves.
* `--digest` alone still does not advance, and that is deliberate: the cheap
  keyword pass and the model pass want the same rows, and silently eating them
  here is how the cursor stalled in the first place.

Known boundary: `wg_dossier.json` is one global file, not one per group, so a
multi-group deployment shares one counter and one cursor (`doc/MULTI_GROUP_PLAN.md`
owns that split; this issue does not widen the surface).
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
import wg_clock  # noqa: E402  (时刻渲染的唯一出口，issue #95；--check 的 YES 行要用)
import wg_config  # noqa: E402  (config.json 的唯一读取口，issue #164 PC)
from wg_paths import data_dir, state_dir
STATE = state_dir()
DB = data_dir() / "mcp-access.sqlite3"
MARK = STATE / "wg_dossier.json"
REPLIED = STATE / "wg_replied.jsonl"
# 配置路径**不**在 import 期取（issue #164 PC 把读取口收到 `wg_config.path()`）：
# 门禁会把数据根指到临时目录，import 期求值会读到开发机的 config.json。
# `settings()` 里每次现读。

DEFAULTS = {
    "idle_minutes": 20,     # quiet this long before a refresh is considered
    "min_messages": 25,     # ...and at least this much unread chatter
    "burst_messages": 80,   # or this many messages regardless of how recent
    "max_material": 80,     # how many messages to hand over in one go
    # 连续多少轮 watch.py 超时（= 这一轮没人指名）后判 DUE（issue #162）。
    # 轮次是主判据：需求说的是「连续 3 次挂 listen」。
    "quiet_rounds": 3,
    # 下限保护：这几轮加起来不足这么多分钟就不触发。`--timeout 30` 时 3 轮只有
    # 90 秒，光钉轮次会让「挂满约 5 分钟」变成假话。默认 90s/轮 × 3 = 270s =
    # 4.5 分钟，下限取 4 留 30s 余量——**不能取 5**：5 > 4.5 会让默认配置下 3 轮
    # 永远过不去，轮次判据就成了死码（issue #162 修复过程中实测到过这个坑）。
    "quiet_minutes": 4,
    # 静默路径的素材下限：0 条时触发等于空跑一次模型调用（issue 待讨论 1）。
    "quiet_min_messages": 1,
    # burst 是「最近一波」而不是「从游标起的积压」（issue #183）：后者只增不减，
    # 游标一停就把几百条历史积压永远报成爆发，quiet 分支再也走不到。
    "burst_window_minutes": 10,
    # DUE 之后等多久还没人 `--mark`，才允许 `--digest --reap` 兜底推进游标
    # （issue #183）。给模型深加工留出处理窗口；调小会让素材过早被规则层吃掉。
    "reap_after_minutes": 15,
}

DUE = 0
NOT_YET = 3


def settings():
    cfg = dict(DEFAULTS)
    try:
        data = json.loads(wg_config.path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    for key, value in (data.get("dossier") or {}).items():
        if key in cfg and isinstance(value, (int, float)):
            cfg[key] = value
    return cfg


def connect():
    """Read-only connection; the backend owns every write.

    issue #169 PC：`?mode=ro` 的 URI 形态合流到 `chatlocal.sqlite_util.connect_ro`，
    经 `wg_text.connect_ro` 延迟导入（chatlocal/config.py 在 import 期建数据目录，
    daemon 不能模块级 import chatlocal）。
    """
    import wg_text
    return wg_text.connect_ro(DB, timeout=5)


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


def save_mark(updates):
    """Merge `updates` into the mark file (read-modify-write, issue #162).

    It used to write the whole object, so whichever verb ran last erased the
    other's keys: `--mark` dropped the quiet counter and `--tick` dropped the
    cursor. Callers now pass only what they own.
    """
    data = load_mark()
    if not isinstance(data, dict):
        data = {}
    data.update(updates or {})
    MARK.parent.mkdir(parents=True, exist_ok=True)
    MARK.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


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


def quiet_state():
    """(rounds, since) from the mark file; `since` is a float or None (issue #162).

    Bad values degrade to "no counter" rather than crashing: this file is shared
    with `--mark`, and a half-written JSON or a string where a number belongs
    must not take the whole listener down (same stance as `wg_listener_state`).
    """
    data = load_mark()
    if not isinstance(data, dict):
        return 0, None
    rounds = data.get("quiet_rounds")
    since = data.get("quiet_since")
    rounds = rounds if isinstance(rounds, int) and not isinstance(rounds, bool) else 0
    try:
        # `bool` is an `int` subclass, so `quiet_since: false` would otherwise
        # read as epoch 0 and turn the floor into "always passed" (same trap
        # `wg_listener_state._pid()` documents for issue #124).
        since = (float(since) if since is not None
                 and not isinstance(since, bool) else None)
    except (TypeError, ValueError):
        since = None
    return max(0, rounds), since


def quiet_minutes_elapsed(now=None):
    """Minutes since the first unanswered round; None when there is no such round."""
    _, since = quiet_state()
    if since is None:
        return None
    moment = time.time() if now is None else float(now)
    return max(0.0, (moment - since) / 60)


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


def quiet_progress(cfg):
    """One line segment describing the quiet counter: `2/3(4.1min)`.

    The `NO` line carries it so "why has this not fired yet" is answerable from
    the output instead of from the state file (issue #162). Without a first
    unanswered round there is no elapsed time to show, and printing `0.0min`
    would read like a clock rather than an absence.
    """
    rounds, since = quiet_state()
    target = cfg["quiet_rounds"]
    elapsed = quiet_minutes_elapsed()
    if since is None or elapsed is None:
        return f"{rounds}/{target}"
    return f"{rounds}/{target}({elapsed:.1f}min)"


def _pending_latest(rows):
    """Newest event id in a pending batch; 0 when there is nothing to read."""
    return rows[-1]["id"] if rows else 0


def recent_count(rows, cfg, now=None):
    """How many of `rows` arrived inside the burst window (issue #183).

    `burst_messages` promises "a lot happened just now". Counting everything
    newer than the cursor instead turns a stalled cursor into a permanent burst:
    the number only grows, and `--check` reports the same 400-message backlog as
    a spike forever. A row without a usable timestamp cannot be shown to be
    recent, so it is left out -- fail closed towards "not a burst" rather than
    inventing one.
    """
    window = cfg["burst_window_minutes"]
    if window <= 0:
        return len(rows)
    moment = time.time() if now is None else float(now)
    cutoff_ms = (moment - window * 60) * 1000
    recent = 0
    for row in rows:
        try:
            ts = float(row.get("ts"))
        except (TypeError, ValueError):
            continue
        if ts >= cutoff_ms:
            recent += 1
    return recent


def due_state():
    """(event_id, at) of the DUE we last announced; (0, None) when none.

    Same degradation rule as `quiet_state`: the file is shared with `--mark`
    and hand edits, so a string or a bool must read as "no DUE" instead of
    making `--check` crash or hold forever.
    """
    data = load_mark()
    if not isinstance(data, dict):
        return 0, None
    event_id = data.get("due_event_id")
    at = data.get("due_at")
    event_id = event_id if isinstance(event_id, int) and not isinstance(event_id, bool) else 0
    try:
        at = (float(at) if at is not None and not isinstance(at, bool) else None)
    except (TypeError, ValueError):
        at = None
    return max(0, event_id), at


def due_hold(latest, cfg):
    """True while the batch we already announced is still the newest one.

    Re-announcing the same unsorted backlog every round is what made
    `DOSSIER_DUE` spin (issue #183): the caller cannot tell "new work" from "the
    same work I ignored". New material, `--mark`, or `--digest --reap` all end
    the hold, so silence here is always traceable to one of the three.
    """
    event_id, at = due_state()
    if not event_id or at is None or latest > event_id:
        return False
    return (time.time() - at) / 60 < cfg["reap_after_minutes"]


def reap_stalled(cfg, batch, now=None):
    """Release a cursor nobody advanced (issue #183).

    Returns `(through, stalled_minutes)`. `through` is 0 when nothing moved;
    `stalled_minutes` is None when there was no DUE to begin with, so the caller
    can tell "not applicable" from "held, but still fresh".

    Only a DUE older than `reap_after_minutes` is reaped, and only up to the
    last row this rule pass actually covered -- advancing to the newest id would
    skip the middle of a backlog that `--dump` is still chewing through
    oldest-first.
    """
    event_id, at = due_state()
    if not event_id or at is None:
        return 0, None
    moment = time.time() if now is None else float(now)
    stalled = max(0.0, (moment - at) / 60)
    if stalled < cfg["reap_after_minutes"]:
        return 0, None
    cursor = int(load_mark().get("last_event_id") or 0)
    through = min(_pending_latest(batch), event_id) if batch else cursor
    if through <= cursor:
        # Nothing this pass could cover: drop the stale DUE so the next real
        # batch is announced instead of being held by a ghost.
        save_mark({"due_event_id": None, "due_at": None})
        return 0, stalled
    save_mark({"last_event_id": through, "last_run": int(moment),
               "due_event_id": None, "due_at": None,
               "quiet_rounds": 0, "quiet_since": None})
    return through, stalled


def cmd_check(cfg):
    session_id, _ = active_session()
    if not session_id:
        print("NO no active session")
        return NOT_YET
    rows = pending_rows(session_id, int(load_mark().get("last_event_id") or 0))
    count = len(rows)
    latest = _pending_latest(rows)
    idle = idle_minutes()
    # Same batch, already announced, caller still inside its window: saying it
    # again adds no information (issue #183). `--check` stays read-only here;
    # the DUE itself is recorded by `listen.py` via `--tick due`.
    if count and due_hold(latest, cfg):
        print(f"NO due-hold pending={count} latest={latest}")
        return NOT_YET
    # Quiet comes first (issue #183). It is the path that can say *why* we are
    # due; burst only says "a lot arrived", and while burst was measured
    # against the whole backlog it was true forever once the cursor stalled --
    # which is exactly how the quiet path became dead code.
    rounds, since = quiet_state()
    elapsed = quiet_minutes_elapsed()
    if (rounds >= cfg["quiet_rounds"]
            and count >= cfg["quiet_min_messages"]
            and elapsed is not None and elapsed >= cfg["quiet_minutes"]):
        stamp = wg_clock.clock(since) if since is not None else "-"
        print(f"YES quiet rounds={rounds} since={stamp} pending={count}")
        return DUE
    # Burst is a spike, not a backlog (issue #183): only rows inside
    # `burst_window_minutes` count, so an old pile left behind by a stalled
    # cursor no longer masquerades as a fresh explosion.
    recent = recent_count(rows, cfg)
    if recent >= cfg["burst_messages"]:
        print(f"YES burst pending={count} recent={recent}")
        return DUE
    if count >= cfg["min_messages"] and idle >= cfg["idle_minutes"]:
        print(f"YES idle={idle:.0f}min pending={count}")
        return DUE
    print(f"NO idle={idle:.0f}min pending={count} quiet={quiet_progress(cfg)}")
    return NOT_YET


def cmd_tick(reset=False, round_seconds=None, due=False):
    """Count one round nobody named us, clear the counter, or record a DUE.

    Called by `listen.py` right after `watch.py` returns 2 (timed out = this
    round had no mention). `reset` is the wake path: somebody named us, which is
    the counter-example to "N rounds in a row".

    `due` records the batch `--check` just announced (issue #183). It lives here
    rather than in `--check` because that verb is documented as read-only: a
    gate or a human running `--check` must not create state that changes what
    the next listener does.

    `quiet_since` is written on the first counted round and never moved after
    that -- moving it would keep pushing the floor ahead of the rounds and the
    path would never fire.

    It is written as the *start* of that first round, not its end: `--tick`
    runs after `watch.py --timeout N` has already returned, so "now" is the
    moment round 1 ended. Anchoring there makes N rounds span only N-1
    intervals, and the `quiet_minutes` floor then quietly pushes the default
    configuration from round 3 out to round 5 -- the rounds criterion would
    still look alive in the `NO` line while never firing (issue #162).
    `round_seconds` is how long that round actually waited, so the elapsed
    time the `NO` line prints is the time the group was really quiet.
    """
    if due:
        session_id, _ = active_session()
        if not session_id:
            print("due skipped: no active session")
            return 1
        latest = _pending_latest(
            pending_rows(session_id, int(load_mark().get("last_event_id") or 0)))
        if not latest:
            print("due skipped: no pending material")
            return 0
        save_mark({"due_event_id": latest, "due_at": time.time()})
        print(f"due recorded event={latest}")
        return 0
    if reset:
        save_mark({"quiet_rounds": 0, "quiet_since": None})
        print("quiet reset rounds=0")
        return 0
    rounds, since = quiet_state()
    updates = {"quiet_rounds": rounds + 1}
    if since is None:
        try:
            back = max(0.0, float(round_seconds or 0))
        except (TypeError, ValueError):
            back = 0.0
        updates["quiet_since"] = time.time() - back
    data = save_mark(updates)
    print(f"quiet rounds={data.get('quiet_rounds')} since="
          f"{wg_clock.clock(data.get('quiet_since')) if data.get('quiet_since') else '-'}")
    return 0


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


def cmd_digest(cfg, limit=150, reap=False):
    """Cheap half: hand recent chatter to the engine's rule-based digester.

    No model involved -- keyword hits and preference declarations only -- so it can
    run on every idle tick. It is idempotent: save_profile updates fields instead of
    appending, and a member whose tone_style is already set is left alone.

    The rule pass itself still does not advance the cursor; the model-backed
    path owns it (`--mark`). `reap=True` is the narrow exception (issue #183):
    once a DUE has gone unanswered for `reap_after_minutes`, these rows have
    already been through the rule layer and parking the cursor only makes
    `--check` re-announce the same pile. See `reap_stalled`.

    Batches go oldest-first, same as `--dump`: a newest-first slice combined
    with an advancing cursor would jump over the middle of a backlog and strand
    it there forever.
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
    batch_rows = rows[:limit] if limit and limit > 0 else rows
    if batch_rows:
        batch = [{"sender_id": r["sender_id"], "sender_name": r["sender"],
                  "content": r["text"]} for r in batch_rows]
        result = get_memory_engine().digest_idle_batch(group_id, batch)
        print(f"digested={result.get('digested_count')} "
              f"updated={result.get('updated_members')}")
    else:
        print("nothing to digest")
    if reap:
        through, stalled = reap_stalled(cfg, batch_rows)
        if through:
            print(f"reaped through event {through} (stalled={stalled:.0f}min)")
        elif stalled is not None:
            print(f"due cleared (stalled={stalled:.0f}min, nothing left)")
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
    # Clearing the quiet counter belongs to marking (issue #162): the material
    # was just summarised, so the next round must not immediately be due again.
    # Clearing the DUE record belongs here too (issue #183): the batch it named
    # has now been summarised, so holding the next one would be a lie.
    save_mark({"last_event_id": int(top), "last_run": int(time.time()),
               "quiet_rounds": 0, "quiet_since": None,
               "due_event_id": None, "due_at": None})
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


def cmd_notes(source=None, group=None, dry_run=False, as_json=False):
    """群级结构化术语的读与写（issue #31 的 daemon 侧入口，issue #163 接链）。

    不给 ``source`` 就是读：把本群的术语表原样列出来（`term=value`，附 kind 与
    更新时间）。给了就是一个 JSON 载荷
    ``[{"term": "遥遥领先", "value": "自嘲用法", "kind": "glossary"}]``，走
    `upsert_group_notes` 按 ``(群号, term)`` 覆盖写——同一个词在一个群里只有一个
    意思，不累积历史。

    写侧本来只有 MCP 的 `consolidate_chat_memory(notes=...)` 调得到，而水群 Agent
    走的是 daemon CLI：不补这个入口，`group_notes` 会一直是 0 行，唤醒行的
    「【群级术语】」也就永远不触发。
    """
    gid = resolve_group(group)
    if not gid:
        print("no group id: pass --group, or bind a session first", file=sys.stderr)
        return 2
    mem = engine()
    if source is None:
        notes = mem.query_group_notes(gid)
        lines = [f"=== 群级术语 group={gid} 共 {len(notes)} 条 ==="]
        lines += [f"  {n['term']}={n['value']}（{n['kind']}）" for n in notes] or ["  （空）"]
        emit({"group_id": gid, "notes": notes}, as_json, lines)
        return 0
    try:
        payload = json.loads(read_payload(source))
    except OSError as exc:
        print(f"cannot read {source}: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"payload is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, list) or not payload:
        print("payload must be a non-empty list of {term, value, kind}", file=sys.stderr)
        return 2
    bad = [f"notes[{i}] needs term" for i, n in enumerate(payload)
           if not isinstance(n, dict) or not str(n.get("term") or "").strip()]
    if bad:
        for line in bad:
            print(line, file=sys.stderr)
        return 2
    if dry_run:
        print(f"would upsert {len(payload)} note(s) group={gid}")
        for n in payload:
            print(f"  {n['term']}={n.get('value', '')}")
        return 0
    result = mem.upsert_group_notes(gid, payload)
    emit(result, as_json, [f"notes updated={result.get('updated', 0)} group={gid}"])
    return 0 if result.get("success") else 1


def main():
    parser = argparse.ArgumentParser(description="在没人 @ 的时候沉淀群友画像")
    parser.add_argument("--check", action="store_true", help="该提炼了吗")
    parser.add_argument("--digest", action="store_true", help="规则版消化（不调模型，可随时跑）")
    parser.add_argument("--reap", action="store_true",
                        help="配合 --digest：DUE 超过 reap_after_minutes 仍无人 --mark 时，"
                             "把游标兜底推进到本批已消化的末尾（issue #183）")
    parser.add_argument("--dump", nargs="?", const=0, type=int, metavar="N",
                        help="输出待提炼素材（text 为完整正文，不截断），可限制条数")
    parser.add_argument("--mark", action="store_true", help="标记到当前最新消息")
    parser.add_argument("--tick", nargs="?", const="round", choices=("round", "reset", "due"),
                        metavar="{round,reset,due}",
                        help="记一轮「没人指名」（缺省 round）；reset 清零（有人指名时用）；"
                             "due 记录刚叫醒过的那批素材，避免同一批每轮重复 DUE")
    parser.add_argument("--round-seconds", type=float, metavar="S",
                        help="配合 --tick round：这一轮实际等了多久（`listen.py` 传 --timeout）；"
                             "首轮据此把 quiet_since 回推到本轮开始，否则 N 轮只算 N-1 个间隔")
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
    parser.add_argument("--notes", action="store_true",
                        help="读本群术语表（黑话 / 店名 / 约定，issue #31）")
    parser.add_argument("--notes-apply", metavar="JSON",
                        help="按 JSON 载荷写群级术语（[{term,value,kind}]），- 读 stdin")
    parser.add_argument("--json", action="store_true", help="机器可读输出（配合上面三个动词）")
    parser.add_argument("--group", metavar="ID",
                        help="群号；缺省取活动会话或载荷里的 group_id")
    parser.add_argument("--dry-run", action="store_true", help="只校验并打印计划，不写库")
    args = parser.parse_args()

    winconsole.ensure_utf8_stdio()

    cfg = settings()
    if args.mark:
        return cmd_mark(args.through)
    if args.tick is not None:
        return cmd_tick(args.tick == "reset", args.round_seconds, due=args.tick == "due")
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
    if args.notes or args.notes_apply is not None:
        return cmd_notes(args.notes_apply, args.group, args.dry_run, args.json)
    if args.digest:
        return cmd_digest(cfg, reap=args.reap)
    if args.dump is not None:
        return cmd_dump(cfg, args.dump or cfg["max_material"])
    if args.check:
        return cmd_check(cfg)
    parser.error("需要 --check / --dump / --mark / --tick / --digest / --ingest / --apply / "
                 "--notes / --notes-apply / "
                 "--audit-pollution / --scrub-pollution / --restore-pollution 之一")


if __name__ == "__main__":
    sys.exit(main())
