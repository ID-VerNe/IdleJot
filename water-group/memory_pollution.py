"""Pollution layer: cross-group row audit, scrub, restore (issue #34).

The pre-#19 write paths landed group-owned facts in the cross-group
`member_profiles` row. This layer reports / removes / restores them
losslessly: only entries the group snapshot already holds are removed, so
every group's `_memory_view` union is byte-identical afterwards. Split out of
`memory_dossier.py` (issue #169 PF-3) so the dossier class is not pinned
against the 800-line class-span wall.

Runtime coupling is `self.lock` / `self._connect()` from `MemoryStoreMixin`
(resolved via MRO on the composed `MemoryEngine`), same as every other mixin.
"""
import json
import time


class MemoryPollutionMixin:

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
