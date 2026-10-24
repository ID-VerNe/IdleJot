"""Dossier layer: L1 cards, L2 queries, and batch consolidation (issue #11 PA).

The MCP-facing surface (``query_member_dossier`` / ``update_member_dossier`` /
``consolidate_chat_memory``) plus the Tier-1 context builder. All group-scoped
reads go through ``MemoryProfileMixin._memory_view`` (issue #5).
"""
import json
import time

from .memory_format import merge_text_list, normalize_list, render_l1_card
from .memory_store import GROUP_NOTE_L1_CAP, INTEREST_SLOTS, MEMORY_SLOTS


class MemoryDossierMixin:

    def build_l1_context(self, group_id, user_ids=None, user_names=None, max_members=3):
        """Builds Tier 1 compact synthesized profiles for active speakers (~80 tokens per member).

        Injected into Agent's context when members speak.
        """
        user_ids = [str(u).strip() for u in (user_ids or []) if str(u).strip()]
        user_names = [str(n).strip() for n in (user_names or []) if str(n).strip()]

        seen_uids = set()
        cards = []

        # Find profiles for active speakers
        identifiers = user_ids + user_names
        for ident in identifiers:
            if len(cards) >= max_members:
                break
            prof = self.find_profile(ident, group_id=group_id)
            if not prof:
                continue
            uid = prof["user_id"]
            if uid in seen_uids:
                continue
            seen_uids.add(uid)

            # Per-group memory view: union of the cross-group profile and this
            # group's snapshot (issue #5). Copy first -- the row came from the
            # caller's scope and must not be rewritten in place.
            group_row = prof.get("_group") if isinstance(prof, dict) else None
            prof = dict(prof)
            prof["key_memories"] = json.dumps(self._memory_view(prof)[0], ensure_ascii=False)
            if group_row:
                # Merge group-specific aliases into the card's alias list.
                base_aliases = json.loads(prof.get("aliases") or "[]")
                for a in json.loads(group_row.get("aliases") or "[]"):
                    if a not in base_aliases:
                        base_aliases.append(a)
                prof["aliases"] = json.dumps(base_aliases, ensure_ascii=False)

            # Check for latest group-scoped event note if any
            group_event_note = ""
            if group_id:
                with self.lock, self._connect() as conn:
                    ev = conn.execute(
                        "SELECT context, quote FROM member_events WHERE user_id=? AND group_id=? ORDER BY timestamp DESC LIMIT 1",
                        (uid, str(group_id)),
                    ).fetchone()
                    if ev:
                        group_event_note = (ev["context"] or ev["quote"]).strip()

            cards.append(render_l1_card(prof, group_event_note))

        # Issue #31: a capped snippet of this group's structured notes (black
        # talk, shop names, venue, in-group conventions) rides along with the
        # member cards so the Agent can resolve a term without a separate read.
        # Capped -- the full set stays behind query_group_notes. Rendered as a
        # card string like the rest of the list: `member_dossiers` stays a list
        # of one-line cards, and a note line never displaces a member card.
        if group_id:
            notes = self.query_group_notes(group_id, limit=GROUP_NOTE_L1_CAP)
            if notes:
                note_str = "；".join(f"{n['term']}={n['value']}" for n in notes)
                cards.append(f"【群级术语】{note_str}")

        return cards

    def upsert_group_notes(self, group_id, notes, source_event_id=None):
        """Writes structured group-level notes keyed by ``(group_id, term)``.

        A term is a black-talk word, shop-name short name, venue or in-group
        convention; ``value`` is what it points to. Re-writing the same term
        overwrites its value in place (upsert) -- there is exactly one meaning
        per term per group, so history is not accumulated here. Requires a
        group id: notes are group-scoped by definition (issue #31).
        """
        gid = str(group_id).strip() if group_id else ""
        if not gid or not notes:
            return {"success": False, "updated": 0}
        now = time.time()
        updated = 0
        with self.lock, self._connect() as conn:
            for note in notes:
                if not isinstance(note, dict):
                    continue
                term = str(note.get("term") or "").strip()
                if not term:
                    continue
                value = str(note.get("value") or "").strip()
                kind = str(note.get("kind") or "glossary").strip()[:32] or "glossary"
                conn.execute(
                    """
                    INSERT INTO group_notes (group_id, term, kind, value, updated_at, source_event_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(group_id, term) DO UPDATE SET
                        kind=excluded.kind, value=excluded.value, updated_at=excluded.updated_at,
                        source_event_id=excluded.source_event_id
                    """,
                    (gid, term, kind, value, now, source_event_id),
                )
                updated += 1
        return {"success": True, "group_id": gid, "updated": updated}

    def query_group_notes(self, group_id, query=None, limit=20):
        """Reads back structured group notes for a group.

        With ``query`` it matches the term (exact) or the value (substring);
        without it, returns the most recently updated notes (used for the
        capped L1 injection). Group-scoped: a term written in group A is
        never visible from group B (issue #31).
        """
        gid = str(group_id).strip() if group_id else ""
        if not gid:
            return []
        limit = int(limit or 20)
        q = str(query or "").strip()
        with self.lock, self._connect() as conn:
            if q:
                rows = conn.execute(
                    """
                    SELECT group_id, term, kind, value, updated_at, source_event_id
                    FROM group_notes
                    WHERE group_id=? AND (term=? OR value LIKE ?)
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (gid, q, f"%{q}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT group_id, term, kind, value, updated_at, source_event_id
                    FROM group_notes
                    WHERE group_id=?
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (gid, limit),
                ).fetchall()
        return [
            {
                "term": r["term"],
                "kind": r["kind"],
                "value": r["value"],
                "updated_at": r["updated_at"],
                "source_event_id": r["source_event_id"],
            }
            for r in rows
        ]

    def query_member_dossier(self, identifier, group_id=None, limit=10):
        """Tier 2 (L2 DB Storage) query for MCP tool `query_member_dossier`.

        Enforces boundary rules:
        - Cross-group shared personality/XP profiles (same QQ account shares profile across groups).
        - Group-scoped private event timelines and gossip (events recorded in group A are filtered by group_id).
        """
        prof = self.find_profile(identifier, group_id=group_id)
        if not prof:
            return {
                "found": False,
                "identifier": identifier,
                "note": f"小本本上暂时没有找到群友【{identifier}】的专属档案。",
            }

        uid = prof["user_id"]
        traits = json.loads(prof["traits"] or "{}")
        group_row = prof.get("_group") if isinstance(prof, dict) else None
        # Issue #5: the dossier reports the same view the L1 card renders --
        # the union of the cross-group profile and this group's snapshot.
        key_memories, memory_scope = self._memory_view(prof)
        if group_row:
            interests = json.loads(group_row["interests"] or "[]") if (group_row.get("interests") or "[]") not in ("", "[]") else json.loads(prof["interests"] or "[]")
            tone = group_row["tone_style"] if (group_row.get("tone_style") or "").strip() else prof["tone_style"]
        else:
            interests = json.loads(prof["interests"] or "[]")
            tone = prof["tone_style"]
        # The backup column is the rollback slot a shrinking overwrite fills in;
        # report it only when it holds something, so the normal response shape
        # is unchanged.
        backup_row = group_row if (group_row and memory_scope == "group") else prof
        memory_backup = json.loads((backup_row or {}).get("key_memories_backup") or "[]")
        aliases = json.loads(prof["aliases"] or "[]")
        if group_row:
            for a in json.loads(group_row["aliases"] or "[]"):
                if a not in aliases:
                    aliases.append(a)

        # Query group-scoped private event timelines
        events = []
        with self.lock, self._connect() as conn:
            if group_id:
                # Scoped to specific group
                rows = conn.execute(
                    "SELECT id, group_id, timestamp, quote, context, source, tags, truncated, original_length FROM member_events WHERE user_id=? AND group_id=? ORDER BY timestamp DESC LIMIT ?",
                    (uid, str(group_id), limit),
                ).fetchall()
            else:
                # All events across groups if no group_id specified
                rows = conn.execute(
                    "SELECT id, group_id, timestamp, quote, context, source, tags, truncated, original_length FROM member_events WHERE user_id=? ORDER BY timestamp DESC LIMIT ?",
                    (uid, limit),
                ).fetchall()

            for r in rows:
                events.append(
                    {
                        "id": r["id"],
                        "group_id": r["group_id"],
                        "timestamp": r["timestamp"],
                        "quote": r["quote"],
                        "context": r["context"],
                        "source": r["source"],
                        "tags": json.loads(r["tags"] or "[]"),
                        # issue #90: the reader can tell a prefix from a whole
                        # quote, and knows how much to re-read elsewhere.
                        "truncated": bool(r["truncated"]),
                        "original_length": int(r["original_length"] or 0),
                    }
                )

        result = {
            "found": True,
            "profile": {
                "user_id": uid,
                "primary_name": prof["primary_name"],
                "aliases": aliases,
                "tone_style": tone,
                "interests": interests,
                "traits": traits,
                "key_memories": key_memories,
                "updated_at": prof["updated_at"],
            },
            "recent_events": events,
            "memory_scope": memory_scope,
            "boundary_scope": f"本群专属 ({group_id})" if group_id else "全部群跨组事件",
            "reply_hint": f"调侃建议：可称呼其外号（{'/'.join(aliases[:3])}），根据其XP喜好（{'/'.join(interests[:3])}）自然插话，偶尔可用'我小本本上可记着...'翻黑料调侃。",
        }
        if memory_backup:
            result["profile"]["key_memories_backup"] = memory_backup
            result["backup_hint"] = "上一份被覆盖的记忆在 profile.key_memories_backup，可据此恢复。"
        return result

    def update_member_dossier(
        self,
        identifier,
        key_memories=None,
        traits=None,
        aliases=None,
        interests=None,
        tone_style=None,
        role=None,
        primary_name=None,
        overwrite=False,
        group_id=None,
    ):
        """Tier 2 (L2 DB Storage) update for MCP tool `update_member_dossier`.

        Allows external agents to natively update a member's dossier (key memories,
        traits, aliases, roles, interests, tone style) without touching raw SQLite.
        """
        if not identifier or not str(identifier).strip():
            return {"success": False, "error": "identifier_required", "note": "必须提供群友标识（QQ号、昵称、名片或既有外号）。"}

        ident_str = str(identifier).strip()
        gid = str(group_id).strip() if group_id else ""
        # Issue #5: without the group id the overlay was never loaded, so a
        # group-scoped call could neither see it nor tell which memories were
        # global-only -- and the write below then replaced both.
        prof = self.find_profile(ident_str, group_id=gid or None)

        norm_aliases = normalize_list(aliases)
        norm_interests = normalize_list(interests)
        norm_memories = normalize_list(key_memories)
        norm_traits = dict(traits) if isinstance(traits, dict) else ({} if traits is not None else None)

        now = time.time()
        backup_list = None
        with self.lock, self._connect() as conn:
            if prof:
                uid = prof["user_id"]
                p_name = primary_name.strip() if (primary_name and primary_name.strip()) else prof["primary_name"]

                cur_aliases = json.loads(prof["aliases"] or "[]")
                cur_interests = json.loads(prof["interests"] or "[]")
                cur_traits = json.loads(prof["traits"] or "{}")
                cur_memories = json.loads(prof["key_memories"] or "[]")
                # Group scope owns its own snapshot; the global row keeps the
                # cross-group facts. Reading the overlay alone and writing the
                # result back into the global row is exactly what deleted them.
                base_memories = self._memory_view(prof)[0] if gid else cur_memories

                if overwrite:
                    new_aliases = norm_aliases if norm_aliases is not None else cur_aliases
                    new_interests = norm_interests if norm_interests is not None else cur_interests
                    new_traits = norm_traits if norm_traits is not None else cur_traits
                    # Same tail-first rule as the merge path: the newest entries
                    # are the ones worth keeping when an overwrite exceeds the cap.
                    new_memories = (norm_memories if norm_memories is not None else base_memories)[-MEMORY_SLOTS:]
                    new_tone = tone_style.strip() if (tone_style is not None and str(tone_style).strip()) else (prof["tone_style"] if tone_style is None else "")
                    # A shrinking overwrite is destructive by definition; keep
                    # the previous list so "restore" is a read, not a memory.
                    if any(m not in new_memories for m in base_memories):
                        backup_list = list(base_memories)
                else:
                    new_aliases = list(dict.fromkeys(cur_aliases + (norm_aliases or [])))
                    new_interests = merge_text_list(cur_interests, norm_interests, limit=INTEREST_SLOTS)
                    new_traits = {**cur_traits, **(norm_traits or {})}
                    new_memories = self._merge_memories(base_memories, norm_memories)
                    new_tone = tone_style.strip() if (tone_style and str(tone_style).strip()) else prof["tone_style"]

                if role:
                    new_traits["role"] = str(role).strip()

                if p_name not in new_aliases:
                    new_aliases.insert(0, p_name)

                # A group-scoped call must never shrink the cross-group row; its
                # memories land in the overlay below instead.
                memories_for_row = cur_memories if gid else new_memories

                conn.execute(
                    """
                    UPDATE member_profiles
                    SET primary_name=?, aliases=?, tone_style=?, interests=?, traits=?, key_memories=?, updated_at=?
                    WHERE user_id=?
                    """,
                    (
                        p_name,
                        json.dumps(new_aliases, ensure_ascii=False),
                        new_tone,
                        json.dumps(new_interests, ensure_ascii=False),
                        json.dumps(new_traits, ensure_ascii=False),
                        json.dumps(memories_for_row, ensure_ascii=False),
                        now,
                        uid,
                    ),
                )
            else:
                uid = ident_str
                p_name = primary_name.strip() if (primary_name and primary_name.strip()) else ident_str
                new_aliases = list(dict.fromkeys([p_name, ident_str] + (norm_aliases or [])))
                new_interests = merge_text_list([], norm_interests, limit=INTEREST_SLOTS)
                new_traits = dict(norm_traits or {})
                if role:
                    new_traits["role"] = str(role).strip()
                new_memories = self._merge_memories([], norm_memories)
                new_tone = tone_style.strip() if (tone_style and str(tone_style).strip()) else "普通群友"

                # A group-scoped insert must not leak group-only facts into the
                # cross-group row; they land in the overlay below (issue #19).
                # This mirrors the UPDATE branch's `memories_for_row` guard.
                memories_for_row = [] if gid else new_memories

                conn.execute(
                    """
                    INSERT INTO member_profiles (user_id, primary_name, aliases, tone_style, interests, traits, key_memories, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        p_name,
                        json.dumps(new_aliases, ensure_ascii=False),
                        new_tone,
                        json.dumps(new_interests, ensure_ascii=False),
                        json.dumps(new_traits, ensure_ascii=False),
                        json.dumps(memories_for_row, ensure_ascii=False),
                        now,
                        now,
                    ),
                )

            if gid:
                # The overlay stores the full view as a snapshot (merge=False):
                # whatever the caller read is what a later read returns, so the
                # "read -> edit -> overwrite" loop is idempotent.
                self._upsert_group_memory(
                    conn, uid, gid,
                    key_memories=new_memories if norm_memories is not None else None,
                    tone_style=tone_style,
                    interests=norm_interests,
                    aliases=norm_aliases,
                    now=now,
                    merge=False,
                )
                if backup_list is not None:
                    conn.execute(
                        "UPDATE member_group_memories SET key_memories_backup=? WHERE user_id=? AND group_id=?",
                        (json.dumps(backup_list, ensure_ascii=False), uid, gid),
                    )
            elif backup_list is not None:
                conn.execute(
                    "UPDATE member_profiles SET key_memories_backup=? WHERE user_id=?",
                    (json.dumps(backup_list, ensure_ascii=False), uid),
                )

        updated_prof = self.find_profile(uid, group_id=gid or None)
        view, scope = self._memory_view(updated_prof)
        result = {
            "success": True,
            "user_id": uid,
            "primary_name": updated_prof["primary_name"],
            "profile": {
                "user_id": updated_prof["user_id"],
                "primary_name": updated_prof["primary_name"],
                "aliases": json.loads(updated_prof["aliases"] or "[]"),
                "tone_style": updated_prof["tone_style"],
                "interests": json.loads(updated_prof["interests"] or "[]"),
                "traits": json.loads(updated_prof["traits"] or "{}"),
                # Report the view, not the raw global column: the caller must
                # see the same set it will overwrite next time (issue #5).
                "key_memories": view,
                "updated_at": updated_prof["updated_at"],
            },
            "memory_scope": scope,
            "note": f"已成功更新群友【{updated_prof['primary_name']}】的小本本画像档案。",
        }
        if backup_list is not None:
            result["profile"]["key_memories_backup"] = backup_list
            result["note"] += "覆盖前的一份记忆已存入 key_memories_backup，可据此恢复。"
        return result

    def consolidate_chat_memory(
        self,
        group_id=None,
        session_id=None,
        records=None,
        events=None,
        messages=None,
        summary=None,
        notes=None,
    ):
        """Batch consolidates recent chat insights and events into structured dossiers.

        Allows external agents to summarize recent chat batch events, update
        multiple members' profiles and record gossips/events in one call.
        ``notes`` writes group-scoped structured terms (black talk, shop
        names, venues, in-group conventions) via ``upsert_group_notes``
        (issue #31) -- the structured counterpart to the flat ``summary`` row.
        """
        updated_profiles = []
        recorded_events_count = 0
        gid = str(group_id).strip() if group_id else ""

        # 1. Process records (member profile updates + optional scoped event)
        if records and isinstance(records, list):
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                ident = rec.get("identifier") or rec.get("user_id") or rec.get("name")
                if not ident:
                    continue
                res = self.update_member_dossier(
                    identifier=ident,
                    key_memories=rec.get("key_memories"),
                    traits=rec.get("traits"),
                    aliases=rec.get("aliases"),
                    interests=rec.get("interests"),
                    tone_style=rec.get("tone_style"),
                    role=rec.get("role"),
                    primary_name=rec.get("primary_name"),
                    overwrite=bool(rec.get("overwrite", False)),
                    # Group-scoped records must land in the overlay, not the
                    # cross-group main row (issue #19). Empty gid -> None keeps
                    # the legacy cross-group behaviour.
                    group_id=gid or None,
                )
                if res.get("success"):
                    updated_profiles.append(res["primary_name"])
                    ev = rec.get("event")
                    ev_gid = str(ev.get("group_id") or gid).strip() if isinstance(ev, dict) else ""
                    if isinstance(ev, dict) and ev_gid:
                        self.record_event(
                            user_id=res["user_id"],
                            group_id=ev_gid,
                            quote=ev.get("quote", ""),
                            context=ev.get("context", ""),
                            source=ev.get("source", "consolidation"),
                            tags=ev.get("tags", []),
                        )
                        recorded_events_count += 1

        # 2. Process independent events
        if events and isinstance(events, list):
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                ident = ev.get("identifier") or ev.get("user_id")
                if not ident:
                    continue
                ev_gid = str(ev.get("group_id") or gid).strip()
                if not ev_gid:
                    continue
                prof = self.find_profile(ident)
                uid = prof["user_id"] if prof else str(ident).strip()
                self.record_event(
                    user_id=uid,
                    group_id=ev_gid,
                    quote=ev.get("quote", ""),
                    context=ev.get("context", ""),
                    source=ev.get("source", "consolidation"),
                    tags=ev.get("tags", []),
                )
                recorded_events_count += 1

        # 3. Process raw messages digest if provided
        digested_count = 0
        if messages and isinstance(messages, list):
            digest_res = self.digest_idle_batch(gid, messages)
            digested_count = digest_res.get("digested_count", 0)
            for m_name in digest_res.get("updated_members", []):
                if m_name not in updated_profiles:
                    updated_profiles.append(m_name)

        # 4. If summary is provided and group_id is present, record a group-level gossip milestone.
        # The summary is a conclusion, not chatter: the tail usually holds the
        # qualification or correction that makes the first half true. It goes in
        # whole; record_event applies the shared ceiling and reports if it bit
        # (issue #90 -- a 1600-character summary used to land as 500).
        if summary and str(summary).strip() and gid:
            self.record_event(
                user_id="group_summary",
                group_id=gid,
                quote=str(summary).strip(),
                context="话题阶段性提炼归档",
                source="summary",
                tags=["话题总结", "群聊纪要"],
            )
            recorded_events_count += 1

        # 5. Structured group notes (issue #31): black talk, shop names,
        # venues, in-group conventions -- keyed by (group_id, term), upserted.
        notes_count = 0
        if notes and isinstance(notes, list) and gid:
            notes_res = self.upsert_group_notes(gid, notes)
            notes_count = notes_res.get("updated", 0)

        updated_unique = list(dict.fromkeys(updated_profiles))
        return {
            "success": True,
            "group_id": gid or None,
            "updated_profiles_count": len(updated_unique),
            "updated_members": updated_unique,
            "recorded_events_count": recorded_events_count,
            "digested_messages_count": digested_count,
            "notes_count": notes_count,
            "note": f"成功归档：已提炼更新 {len(updated_unique)} 位群友档案，沉淀 {recorded_events_count} 条事件记忆。",
        }

    # ------------------------------------------------------------------
    # issue #34: cross-group rows holding group-owned facts
    #
    # #19 fixed the write paths (a group-scoped call no longer lands group
    # facts in `member_profiles`), but the rows written before that fix still
    # carry them. An overlay is a *snapshot of the full view* (issue #5 stores
    # the union with `merge=False`), so an entry that appears in both rows is
    # not by itself proof of pollution -- a legitimate cross-group fact is
    # mirrored into every snapshot taken after it. What makes removal safe is
    # the other direction: the group snapshot already holds the entry, so the
    # group reader keeps it either way.
    # ------------------------------------------------------------------

    def _pollution_rows(self, conn):
        """Reads the three tables the pollution audit needs, inside one connection.

        Kept separate from the computation so `scrub_pollution` can read and
        write in a single transaction: the daemon is a live writer, and a plan
        built from a read that a concurrent write has already invalidated would
        restore a stale `key_memories` over newer facts.
        """
        profiles = conn.execute(
            "SELECT user_id, primary_name, key_memories FROM member_profiles"
        ).fetchall()
        overlays = {}
        for row in conn.execute(
            "SELECT user_id, group_id, key_memories FROM member_group_memories"
        ).fetchall():
            overlays.setdefault(row["user_id"], []).append(
                {"group_id": row["group_id"], "memories": json.loads(row["key_memories"] or "[]")}
            )
        seen_groups = {}
        for row in conn.execute(
            "SELECT DISTINCT user_id, group_id FROM member_events"
        ).fetchall():
            seen_groups.setdefault(row["user_id"], set()).add(row["group_id"])
        return profiles, overlays, seen_groups

    def _pollution_plan(self, profiles, overlays, seen_groups):
        """Splits every cross-group row into provable pollution and the rest.

        `remove` holds entries the group snapshot already has; `keep` holds the
        rest. `in_scope` is the guard that makes `remove` lossless: a member
        with no snapshot has nothing to prove a fact is group-owned, and a
        member seen in a group *without* a snapshot would lose the fact for
        that group's readers, so neither is touched.
        """
        plan = []
        for prof in profiles:
            uid = prof["user_id"]
            rows = overlays.get(uid) or []
            overlay_memories = [m for row in rows for m in row["memories"]]
            overlay_set = set(overlay_memories)
            global_memories = json.loads(prof["key_memories"] or "[]")
            overlay_groups = {row["group_id"] for row in rows}
            other_groups = seen_groups.get(uid, set()) - overlay_groups
            plan.append({
                "user_id": uid,
                "primary_name": prof["primary_name"],
                "overlay_groups": sorted(overlay_groups),
                "global_memories": global_memories,
                "remove": [m for m in global_memories if m in overlay_set],
                "keep": [m for m in global_memories if m not in overlay_set],
                "in_scope": bool(rows) and not other_groups,
            })
        return plan

    def audit_pollution(self):
        """Read-only scale report for the pre-#19 cross-group pollution (issue #34).

        Touches nothing. Per member it reports how much of the cross-group row
        the group snapshot already holds (`provable_pollution` -- removable
        without changing any group's view), how much only the cross-group row
        holds (`global_only` -- kept, because no snapshot can prove those are
        group-owned), and whether the member is in scope for a lossless
        cleanup.
        """
        with self.lock, self._connect() as conn:
            profiles, overlays, seen_groups = self._pollution_rows(conn)
        plan = self._pollution_plan(profiles, overlays, seen_groups)

        members = []
        totals = {"global": 0, "provable": 0, "global_only": 0,
                  "global_only_with_overlay": 0, "no_overlay": 0,
                  "out_of_scope": 0, "scrubbable": 0}
        for item in plan:
            has_overlay = bool(item["overlay_groups"])
            totals["global"] += len(item["global_memories"])
            totals["provable"] += len(item["remove"])
            totals["global_only"] += len(item["keep"])
            if has_overlay:
                totals["global_only_with_overlay"] += len(item["keep"])
            else:
                totals["no_overlay"] += 1
            if has_overlay and not item["in_scope"]:
                totals["out_of_scope"] += 1
            if item["in_scope"]:
                totals["scrubbable"] += len(item["remove"])
            members.append({
                "user_id": item["user_id"],
                "primary_name": item["primary_name"],
                "overlay_groups": item["overlay_groups"],
                "global_memories": len(item["global_memories"]),
                "provable_pollution": len(item["remove"]),
                "global_only": len(item["keep"]),
                "in_scope": item["in_scope"],
            })
        return {
            "profiles_total": len(plan),
            "members_with_overlay": sum(1 for item in plan if item["overlay_groups"]),
            "no_overlay_members": totals["no_overlay"],
            "groups": sorted({g for item in plan for g in item["overlay_groups"]}),
            "global_memories_total": totals["global"],
            "provable_pollution_total": totals["provable"],
            "scrubbable_total": totals["scrubbable"],
            "global_only_total": totals["global_only"],
            "global_only_with_overlay_total": totals["global_only_with_overlay"],
            "out_of_scope_members": totals["out_of_scope"],
            "members": members,
        }

    def scrub_pollution(self, dry_run=False):
        """Removes the group-owned facts from the cross-group rows (issue #34).

        Only what the group snapshot already holds is removed, so every group's
        `_memory_view` union is byte-identical afterwards; the change is visible
        solely to a reader that asks for no group at all, which is exactly the
        leak. The whole pre-scrub list is written to ``key_memories_backup``
        (the same rollback slot a shrinking overwrite fills), which makes the
        cleanup a read to undo rather than a reconstruction. Members outside
        the scope guard are reported and left alone.
        """
        now = time.time()
        scrubbed = []
        skipped = []
        with self.lock, self._connect() as conn:
            profiles, overlays, seen_groups = self._pollution_rows(conn)
            for item in self._pollution_plan(profiles, overlays, seen_groups):
                if not item["in_scope"] or not item["remove"]:
                    if item["overlay_groups"] and item["remove"]:
                        skipped.append({
                            "user_id": item["user_id"],
                            "primary_name": item["primary_name"],
                            "reason": "seen in a group without a snapshot",
                            "would_remove": len(item["remove"]),
                        })
                    continue
                row = conn.execute(
                    "SELECT key_memories, key_memories_backup FROM member_profiles WHERE user_id=?",
                    (item["user_id"],),
                ).fetchone()
                if row is None:
                    continue
                # Re-read inside the write transaction: the daemon may have
                # merged new facts since the plan was built, and restoring a
                # stale list here would drop them.
                current = json.loads(row["key_memories"] or "[]")
                keep = [m for m in current if m not in set(item["remove"])]
                if keep == current:
                    continue
                backup_before = bool((row["key_memories_backup"] or "").strip()
                                     and row["key_memories_backup"] not in ("", "[]"))
                if not dry_run:
                    conn.execute(
                        "UPDATE member_profiles SET key_memories=?, key_memories_backup=?, updated_at=? WHERE user_id=?",
                        (json.dumps(keep, ensure_ascii=False),
                         json.dumps(current, ensure_ascii=False), now, item["user_id"]),
                    )
                scrubbed.append({
                    "user_id": item["user_id"],
                    "primary_name": item["primary_name"],
                    "overlay_groups": item["overlay_groups"],
                    "removed": len(current) - len(keep),
                    "kept": len(keep),
                    "backup_overwritten": backup_before,
                })
        return {
            "dry_run": bool(dry_run),
            "scrubbed_members": len(scrubbed),
            "removed_total": sum(s["removed"] for s in scrubbed),
            "kept_total": sum(s["kept"] for s in scrubbed),
            "members": scrubbed,
            "skipped": skipped,
        }

    def restore_pollution(self, dry_run=False):
        """Undoes a scrub by writing the backed-up list back (issue #34).

        The backup holds the cross-group row exactly as it was before the
        scrub, so restoring is a verbatim write and the pre-scrub ordering --
        which drives `_merge_memories` eviction priority -- survives. The
        backup slot is cleared afterwards: its job is done, and leaving it set
        would make a later query report a rollback hint for a state that is
        already current.
        """
        restored = []
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, primary_name, key_memories, key_memories_backup "
                "FROM member_profiles "
                "WHERE key_memories_backup IS NOT NULL AND key_memories_backup != '' "
                "AND key_memories_backup != '[]'"
            ).fetchall()
            for row in rows:
                backup = json.loads(row["key_memories_backup"] or "[]")
                if not backup:
                    continue
                current = json.loads(row["key_memories"] or "[]")
                if current == backup:
                    # Already restored (or the backup predates any change):
                    # clearing keeps the slot from lying about a rollback.
                    if not dry_run:
                        conn.execute(
                            "UPDATE member_profiles SET key_memories_backup=NULL WHERE user_id=?",
                            (row["user_id"],),
                        )
                    continue
                if not dry_run:
                    conn.execute(
                        "UPDATE member_profiles SET key_memories=?, key_memories_backup=NULL, updated_at=? WHERE user_id=?",
                        (json.dumps(backup, ensure_ascii=False), time.time(), row["user_id"]),
                    )
                restored.append({
                    "user_id": row["user_id"],
                    "primary_name": row["primary_name"],
                    "restored_memories": len(backup),
                })
        return {
            "dry_run": bool(dry_run),
            "restored_members": len(restored),
            "restored_total": sum(r["restored_memories"] for r in restored),
            "members": restored,
        }
