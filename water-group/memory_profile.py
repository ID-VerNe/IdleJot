"""Profile layer: identity records, seeding, matching, and sync (issue #11 PA).

Everything that reads or writes ``member_profiles`` / ``member_group_memories``
rows lives here, including the issue #5 read-side union (``_memory_view``) that
keeps "what the caller read" equal to "what the caller overwrites".
"""
import json
import time
from pathlib import Path

from .memory_format import merge_text_list, normalize_list
from .memory_store import GENERIC_ALIASES, MEMORY_SLOTS


class MemoryProfileMixin:

    def get_member_count(self):
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM member_profiles").fetchone()
            return row[0] if row else 0

    def seed_from_persona(self, persona_path=None):
        """Seeds illustrative placeholder member profiles.

        Real profiles are created via MCP tools or private persona cards; no
        real nicknames or personal data are shipped in source (issue #76).
        """
        if persona_path is not None and not Path(persona_path).exists():
            # Explicit path = caller's contract: no card, no seeding.
            return 0
        # Seeds are illustrative placeholders only. Real member profiles must be
        # created via MCP tools or private persona cards; no real nicknames/XPs
        # are shipped in source (see issue #76).

        now = time.time()
        seeds = [
            {
                "user_id": "seed:example_a",
                "primary_name": "示例群友A",
                "aliases": ["示例群友A", "示例A"],
                "tone_style": "占位示例，部署后请删除或替换",
                "interests": ["示例游戏", "示例游戏2"],
                "traits": {"role": "示例"},
                "key_memories": ["占位种子：一个话痨示例档案，部署后应替换为真实档案"],
            },
            {
                "user_id": "seed:example_b",
                "primary_name": "示例群友B",
                "aliases": ["示例群友B", "示例B"],
                "tone_style": "占位示例，部署后请删除或替换",
                "interests": ["示例XP", "健身"],
                "traits": {"role": "示例"},
                "key_memories": ["占位种子：另一个话痨示例档案，部署后应替换为真实档案"],
            },
        ]

        inserted = 0
        with self.lock, self._connect() as conn:
            for s in seeds:
                # Check if profile already exists by user_id or primary_name
                row = conn.execute(
                    "SELECT user_id, aliases, interests, key_memories, traits FROM member_profiles WHERE user_id=? OR primary_name=?",
                    (s["user_id"], s["primary_name"]),
                ).fetchone()

                if not row:
                    conn.execute(
                        """
                        INSERT INTO member_profiles (user_id, primary_name, aliases, tone_style, interests, traits, key_memories, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            s["user_id"],
                            s["primary_name"],
                            json.dumps(s["aliases"], ensure_ascii=False),
                            s["tone_style"],
                            json.dumps(s["interests"], ensure_ascii=False),
                            json.dumps(s["traits"], ensure_ascii=False),
                            json.dumps(s["key_memories"], ensure_ascii=False),
                            now,
                            now,
                        ),
                    )
                    inserted += 1
                else:
                    # Merge aliases & interests if missing
                    existing_aliases = json.loads(row["aliases"] or "[]")
                    merged_aliases = list(dict.fromkeys(existing_aliases + s["aliases"]))
                    existing_interests = json.loads(row["interests"] or "[]")
                    merged_interests = list(dict.fromkeys(existing_interests + s["interests"]))
                    existing_memories = json.loads(row["key_memories"] or "[]")
                    merged_memories = self._merge_memories(
                        existing_memories, s["key_memories"]
                    )

                    conn.execute(
                        """
                        UPDATE member_profiles SET aliases=?, interests=?, key_memories=?, updated_at=?
                        WHERE user_id=?
                        """,
                        (
                            json.dumps(merged_aliases, ensure_ascii=False),
                            json.dumps(merged_interests, ensure_ascii=False),
                            json.dumps(merged_memories, ensure_ascii=False),
                            now,
                            row["user_id"],
                        ),
                    )
        return inserted

    def match_profiles(self, identifier):
        """Every profile that answers to `identifier`, split by how well it fits.

        `find_profile` stops at the first hit. That is the right answer to "who is
        this", and the wrong one for an @-mention: 「时之」 is a substring of both
        群友A and 群友B, so taking the first silently aims the @ at one of
        them. Handing back the whole set lets the caller see the tie and say so
        instead of guessing.

        Returns (exact, loose), lists of profile dicts in table order. `exact`
        means the identifier is the user_id, the primary name, or an alias
        verbatim; `loose` means it is a >=2 character substring of one. Exact
        hits come primary-name-first so `find_profile` can keep taking [0].
        """
        if not identifier:
            return [], []
        ident = str(identifier).strip()
        if not ident:
            return [], []

        with self.lock, self._connect() as conn:
            # A QQ number is nobody's nickname; settling it first keeps a stray
            # alias from outranking the account it names.
            by_id = conn.execute(
                "SELECT * FROM member_profiles WHERE user_id=?", (ident,)
            ).fetchone()
            if by_id:
                return [dict(by_id)], []
            rows = [dict(r) for r in conn.execute("SELECT * FROM member_profiles").fetchall()]

        lower = ident.casefold()
        by_name, by_alias, loose = [], [], []
        for r in rows:
            name = str(r["primary_name"]).casefold()
            aliases = [str(a) for a in json.loads(r["aliases"] or "[]")]
            if lower == name:
                by_name.append(r)
            elif any(lower == a.casefold() for a in aliases):
                by_alias.append(r)
            elif len(lower) >= 2 and (
                lower in name
                or any(lower in a.casefold() for a in aliases if a not in GENERIC_ALIASES)
            ):
                loose.append(r)
        return by_name + by_alias, loose

    def alias_index(self, group_id=None, min_len=2):
        """Every known name in the dossier, as ``{name: (user_id, primary_name)}``.

        Rendering layers need to answer "who does this nickname point at" for a
        whole line of chatter, not one profile at a time (issue #30): a per-line
        ``match_profiles`` hands back full rows (key_memories included), and a
        wake batch would hit the database once per message. This is one read of
        the same view ``query_member_dossier`` reports -- the cross-group
        profile unioned with this group's overlay (issue #5) -- shaped for
        scanning instead of for one lookup.

        Generic placeholders (「领导」「群友」) and anything shorter than
        ``min_len`` are dropped: they name nobody, and matching them would
        invent pointers nobody asked for. On an alias collision the first
        profile in table order wins, same as ``find_profile`` taking [0]; a
        rendering hint does not need to relitigate ties.
        """
        with self.lock, self._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT user_id, primary_name, aliases FROM member_profiles").fetchall()]
            overlays = {}
            gid = str(group_id).strip() if group_id else ""
            if gid:
                for r in conn.execute(
                        "SELECT user_id, aliases FROM member_group_memories WHERE group_id=?",
                        (gid,)).fetchall():
                    overlays[str(r["user_id"])] = json.loads(r["aliases"] or "[]")
        index = {}
        for r in rows:
            uid = str(r["user_id"])
            names = [str(r["primary_name"])] + [str(a) for a in json.loads(r["aliases"] or "[]")]
            names += [str(a) for a in overlays.get(uid) or []]
            for name in dict.fromkeys(n for n in names if n and n.strip()):
                if len(name) < min_len or name in GENERIC_ALIASES:
                    continue
                index.setdefault(name, (uid, str(r["primary_name"])))
        return index

    def find_profile(self, identifier, group_id=None):
        """Finds a member profile by user_id, primary_name, or alias.

        When ``group_id`` is given, the matching group overlay (if any) is
        attached as ``_group`` (a dict row or None) without mutating the
        global profile. Callers that need group-scoped memories should read
        ``profile["_group"]``.
        """
        exact, loose = self.match_profiles(identifier)
        prof = (exact or loose or [None])[0]
        if prof is None:
            return None
        prof = dict(prof)
        if group_id:
            gid = str(group_id).strip()
            if gid:
                with self.lock, self._connect() as conn:
                    row = conn.execute(
                        "SELECT * FROM member_group_memories WHERE user_id=? AND group_id=?",
                        (str(prof["user_id"]), gid),
                    ).fetchone()
                prof["_group"] = dict(row) if row else None
            else:
                prof["_group"] = None
        else:
            prof["_group"] = None
        return prof

    def _merge_memories(self, current, incoming):
        """Merge profile memories under the shared cap and refresh restatements.

        The list is kept least-recently-restated first, so overflowing the cap
        drops the fact that has gone longest without being mentioned instead of
        the one that has merely existed longest.
        """
        return merge_text_list(
            current,
            incoming,
            limit=MEMORY_SLOTS,
            refresh_restated=True,
        )

    def _memory_view(self, profile):
        """Returns the group-scoped memory view as ``(memories, scope)``.

        A group overlay is one group's *snapshot*, not a replacement for the
        cross-group profile. Reading it alone is what made issue #5 destructive:
        rows written before the fix hold only the group-specific slice, so the
        read returned a subset of what an ``overwrite`` write then replaced --
        the global-only facts vanished without ever being shown. The view is the
        deduplicated union, capped like every other write, so "what the caller
        read" is exactly "what the caller overwrites".
        """
        if not isinstance(profile, dict):
            return [], "global (legacy)"
        global_memories = json.loads(profile.get("key_memories") or "[]")
        group_row = profile.get("_group")
        group_memories = json.loads((group_row or {}).get("key_memories") or "[]") if group_row else []
        if not group_memories:
            return global_memories, "global (legacy)"
        return self._merge_memories(global_memories, group_memories), "group"

    def _upsert_group_memory(self, conn, user_id, group_id, key_memories=None, tone_style=None, interests=None, aliases=None, now=None, merge=True):
        """Upsert a group-scoped memory overlay row. No-op when group_id is empty."""
        uid = str(user_id or "").strip()
        gid = str(group_id or "").strip()
        if not uid or not gid:
            return
        now = now if now is not None else time.time()
        row = conn.execute(
            "SELECT * FROM member_group_memories WHERE user_id=? AND group_id=?", (uid, gid)
        ).fetchone()
        inc_mem = key_memories  # None = leave; list = incoming
        inc_interests = interests if interests is not None else None
        inc_aliases = aliases if aliases is not None else None

        if row:
            cur_mem = json.loads(row["key_memories"] or "[]")
            cur_tone = row["tone_style"] or ""
            cur_int = json.loads(row["interests"] or "[]")
            cur_alias = json.loads(row["aliases"] or "[]")
            if inc_mem is None:
                new_mem = cur_mem
            elif merge:
                new_mem = self._merge_memories(cur_mem, inc_mem)
            else:
                new_mem = list(inc_mem)
            new_tone = str(tone_style).strip() if (tone_style is not None and str(tone_style).strip()) else cur_tone
            if inc_interests is None:
                new_int = cur_int
            else:
                new_int = list(dict.fromkeys(cur_int + (inc_interests or []))) if merge else list(dict.fromkeys(inc_interests or []))
            if inc_aliases is None:
                new_alias = cur_alias
            else:
                new_alias = list(dict.fromkeys(cur_alias + (inc_aliases or []))) if merge else list(dict.fromkeys(inc_aliases or []))
            conn.execute(
                """UPDATE member_group_memories
                   SET key_memories=?, tone_style=?, interests=?, aliases=?, updated_at=?
                   WHERE user_id=? AND group_id=?""",
                (json.dumps(new_mem, ensure_ascii=False), new_tone,
                 json.dumps(new_int, ensure_ascii=False), json.dumps(new_alias, ensure_ascii=False),
                 now, uid, gid),
            )
        else:
            # Create only when there is something to store (avoid empty rows).
            new_mem = self._merge_memories([], inc_mem) if (inc_mem not in (None, [])) else []
            has_content = (inc_mem not in (None, [])) or (tone_style is not None and str(tone_style).strip()) or inc_interests or inc_aliases
            if not has_content:
                return
            conn.execute(
                """INSERT INTO member_group_memories
                   (user_id, group_id, key_memories, tone_style, interests, aliases, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (uid, gid,
                 json.dumps(new_mem, ensure_ascii=False),
                 str(tone_style or "").strip(),
                 json.dumps(list(dict.fromkeys(inc_interests or [])), ensure_ascii=False),
                 json.dumps(list(dict.fromkeys(inc_aliases or [])), ensure_ascii=False),
                 now, now),
            )

    def save_profile(self, user_id, primary_name, aliases=None, tone_style="", interests=None, traits=None, key_memories=None, group_id=None):
        """Creates or updates a member profile (cross-group shared).

        `key_memories` follows the same contract as `update_member_dossier`:
        ``None`` means "not supplied" and leaves the column untouched, while an
        explicit list is merged under `MEMORY_SLOTS`. Both halves matter. The
        idle digester calls this method with only interests/tone for every
        message it sees, so an unconditional truncation here silently undid
        whatever a dossier write had just stored (issue #43).
        """
        now = time.time()
        user_id = str(user_id).strip()
        primary_name = str(primary_name).strip() or user_id

        aliases_list = list(dict.fromkeys(aliases or []))
        if primary_name not in aliases_list:
            aliases_list.append(primary_name)
        interests_list = list(dict.fromkeys(interests or []))
        traits_dict = traits or {}
        # None stays None: that is how the merge below tells "leave it alone"
        # apart from "the caller sent an empty list".
        memories_list = normalize_list(key_memories)

        with self.lock, self._connect() as conn:
            existing = conn.execute("SELECT * FROM member_profiles WHERE user_id=?", (user_id,)).fetchone()
            if existing:
                cur_aliases = json.loads(existing["aliases"] or "[]")
                cur_interests = json.loads(existing["interests"] or "[]")
                cur_traits = json.loads(existing["traits"] or "{}")
                cur_memories = json.loads(existing["key_memories"] or "[]")

                merged_aliases = list(dict.fromkeys(cur_aliases + aliases_list))
                merged_interests = list(dict.fromkeys(cur_interests + interests_list))
                merged_traits = {**cur_traits, **traits_dict}
                if memories_list is None:
                    merged_memories = cur_memories
                else:
                    merged_memories = self._merge_memories(cur_memories, memories_list)
                tone = tone_style or existing["tone_style"]

                conn.execute(
                    """
                    UPDATE member_profiles
                    SET primary_name=?, aliases=?, tone_style=?, interests=?, traits=?, key_memories=?, updated_at=?
                    WHERE user_id=?
                    """,
                    (
                        primary_name or existing["primary_name"],
                        json.dumps(merged_aliases, ensure_ascii=False),
                        tone,
                        json.dumps(merged_interests, ensure_ascii=False),
                        json.dumps(merged_traits, ensure_ascii=False),
                        json.dumps(merged_memories, ensure_ascii=False),
                        now,
                        user_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO member_profiles (user_id, primary_name, aliases, tone_style, interests, traits, key_memories, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        primary_name,
                        json.dumps(aliases_list, ensure_ascii=False),
                        tone_style,
                        json.dumps(interests_list, ensure_ascii=False),
                        json.dumps(traits_dict, ensure_ascii=False),
                        json.dumps(
                            self._merge_memories([], memories_list),
                            ensure_ascii=False,
                        ),
                        now,
                        now,
                    ),
                )

        if group_id:
            gid = str(group_id).strip()
            if gid:
                with self.lock, self._connect() as conn:
                    self._upsert_group_memory(
                        conn, user_id, gid,
                        key_memories=memories_list,
                        tone_style=tone_style,
                        interests=interests_list,
                        aliases=aliases_list,
                        now=now,
                    )

        return self.find_profile(user_id)

    def sync_member_mappings(self, mappings, group_id=None):
        """Binds explicit member mappings (QQ -> identity/aliases/traits/seeds).
        mappings: dict of { "qq_or_id": { "name": ..., "aliases": [...], "role": ..., "tone_style": ..., "interests": [...], "desc": ..., "seed_key": ... } }
        """
        if not mappings or not isinstance(mappings, dict):
            return 0

        synced = 0
        now = time.time()
        with self.lock, self._connect() as conn:
            for target_id, info in mappings.items():
                if not isinstance(info, dict):
                    continue
                target_id = str(target_id).strip()
                if not target_id:
                    continue
                name = str(info.get("name") or target_id).strip()
                aliases = [str(a).strip() for a in (info.get("aliases") or []) if str(a).strip()]
                role = str(info.get("role") or "").strip()
                tone_style = str(info.get("tone_style") or "").strip()
                interests = [str(i).strip() for i in (info.get("interests") or []) if str(i).strip()]
                desc = str(info.get("desc") or "").strip()
                seed_key = str(info.get("seed_key") or "").strip()
                card = str(info.get("card") or "").strip()

                # Check if seed_key or candidate seed row exists
                seed_row = None
                if seed_key:
                    seed_row = conn.execute("SELECT * FROM member_profiles WHERE user_id=?", (seed_key,)).fetchone()
                if not seed_row:
                    seed_row = conn.execute(
                        "SELECT * FROM member_profiles WHERE LOWER(primary_name)=LOWER(?) AND user_id!=? AND user_id LIKE 'seed:%'",
                        (name, target_id),
                    ).fetchone()

                seed_aliases = json.loads(seed_row["aliases"] or "[]") if seed_row else []
                seed_interests = json.loads(seed_row["interests"] or "[]") if seed_row else []
                seed_traits = json.loads(seed_row["traits"] or "{}") if seed_row else {}
                seed_memories = json.loads(seed_row["key_memories"] or "[]") if seed_row else []
                seed_tone = seed_row["tone_style"] if seed_row else ""
                seed_id = seed_row["user_id"] if seed_row else None

                existing_target = conn.execute("SELECT * FROM member_profiles WHERE user_id=?", (target_id,)).fetchone()
                cur_aliases = json.loads(existing_target["aliases"] or "[]") if existing_target else []
                cur_interests = json.loads(existing_target["interests"] or "[]") if existing_target else []
                cur_traits = json.loads(existing_target["traits"] or "{}") if existing_target else {}
                cur_memories = json.loads(existing_target["key_memories"] or "[]") if existing_target else []

                merged_aliases = list(dict.fromkeys([name, target_id] + ([card] if card else []) + aliases + seed_aliases + cur_aliases))
                merged_interests = list(dict.fromkeys(interests + seed_interests + cur_interests))
                merged_traits = {**seed_traits, **cur_traits}
                if role:
                    merged_traits["role"] = role
                if card:
                    merged_traits["card"] = card
                merged_tone = tone_style or seed_tone or (existing_target["tone_style"] if existing_target and existing_target["tone_style"] else "普通群友")

                desc_list = [desc] if desc and desc not in seed_memories and desc not in cur_memories else []
                # desc is this pass's fresh material, so it goes in as the incoming
                # side: the helper keeps the tail when the cap bites.
                merged_memories = self._merge_memories(
                    seed_memories + cur_memories, desc_list
                )

                if existing_target:
                    conn.execute(
                        """
                        UPDATE member_profiles
                        SET primary_name=?, aliases=?, tone_style=?, interests=?, traits=?, key_memories=?, updated_at=?
                        WHERE user_id=?
                        """,
                        (
                            name,
                            json.dumps(merged_aliases, ensure_ascii=False),
                            merged_tone,
                            json.dumps(merged_interests, ensure_ascii=False),
                            json.dumps(merged_traits, ensure_ascii=False),
                            json.dumps(merged_memories, ensure_ascii=False),
                            now,
                            target_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO member_profiles (user_id, primary_name, aliases, tone_style, interests, traits, key_memories, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_id,
                            name,
                            json.dumps(merged_aliases, ensure_ascii=False),
                            merged_tone,
                            json.dumps(merged_interests, ensure_ascii=False),
                            json.dumps(merged_traits, ensure_ascii=False),
                            json.dumps(merged_memories, ensure_ascii=False),
                            now,
                            now,
                        ),
                    )

                # Migrate events from seed to target QQ and remove retired seed row
                if seed_id and seed_id != target_id:
                    conn.execute("UPDATE member_events SET user_id=? WHERE user_id=?", (target_id, seed_id))
                    conn.execute("DELETE FROM member_profiles WHERE user_id=?", (seed_id,))

                synced += 1

        return synced

    def sync_onebot_members(self, members, group_id=None):
        """Syncs member list from OneBot, recording/updating cards and nicknames into profiles."""
        if not members:
            return 0
        updated = 0
        now = time.time()
        with self.lock, self._connect() as conn:
            for m in members:
                if not isinstance(m, dict):
                    continue
                uid = str(m.get("user_id") or "").strip()
                if not uid or not uid.isdigit():
                    continue
                nickname = str(m.get("nickname") or "").strip()
                card = str(m.get("card") or "").strip()
                role = str(m.get("role") or "").strip()

                prof = conn.execute("SELECT * FROM member_profiles WHERE user_id=?", (uid,)).fetchone()
                if prof:
                    aliases = json.loads(prof["aliases"] or "[]")
                    traits = json.loads(prof["traits"] or "{}")
                    changed = False
                    for item in (card, nickname):
                        if item and item not in aliases:
                            aliases.append(item)
                            changed = True
                    if card and traits.get("card") != card:
                        traits["card"] = card
                        changed = True
                    if nickname and traits.get("nickname") != nickname:
                        traits["nickname"] = nickname
                        changed = True
                    if role in ("owner", "admin") and traits.get("group_role") != role:
                        traits["group_role"] = role
                        changed = True
                    if changed:
                        conn.execute(
                            "UPDATE member_profiles SET aliases=?, traits=?, updated_at=? WHERE user_id=?",
                            (json.dumps(aliases, ensure_ascii=False), json.dumps(traits, ensure_ascii=False), now, uid),
                        )
                        updated += 1
                else:
                    # Create lightweight profile for newly observed group member.
                    # Nickname wins over card: the card is the most volatile column
                    # (meme names, joke sentences) and freezing it as primary_name
                    # poisons the L1 card for good, because the update branch never
                    # rewrites primary_name. The card still lands in aliases and
                    # traits.card, which is what AGENTS.md §3.4 asks for.
                    p_name = nickname or card or uid
                    aliases = [a for a in [p_name, uid, card, nickname] if a]
                    aliases = list(dict.fromkeys(aliases))
                    traits = {"card": card, "nickname": nickname}
                    if role in ("owner", "admin"):
                        traits["group_role"] = role
                    conn.execute(
                        """
                        INSERT INTO member_profiles (user_id, primary_name, aliases, tone_style, interests, traits, key_memories, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (uid, p_name, json.dumps(aliases, ensure_ascii=False), "普通群友", "[]", json.dumps(traits, ensure_ascii=False), "[]", now, now),
                    )
                    updated += 1
        return updated
