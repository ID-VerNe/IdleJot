"""Observation layer: events, banter commands, and idle digestion (PA).

Turns group messages into timeline rows and profile updates. The explicit
command grammar itself lives in ``memory_commands``; this layer only supplies
the profile lookups it needs and executes the writes.
"""
import json
import re
import time

from .memory_commands import known_names, parse_command

# Text ceilings live in one module (issue #90). Reaching it by package name is
# safe here: importing `water_group.memory_engine` already requires the repo root
# on sys.path, which is what makes `chatlocal` importable in the first place.
from chatlocal.text_limits import EVENT_TEXT_CHARS, clip
from chatlocal.name_guard import clean_sender_name


class MemoryObserveMixin:

    def record_event(self, user_id, group_id, quote, context, source="interaction", tags=None, timestamp=None):
        """Records a timeline event scoped to a specific group, with recent deduplication.

        Both text fields go through ``clip()`` (issue #90): the row reports
        whether it holds a prefix and, if so, how long the original was. The
        previous hard slice at 1000 looked exactly like a complete quote, so a
        1600-character summary was consolidated from a fragment the caller could
        not detect -- and could not fix by re-reading anything.
        """
        now = time.time()
        ts = timestamp if timestamp is not None else now
        user_id = str(user_id).strip()
        group_id = str(group_id).strip()
        tags_list = list(dict.fromkeys(tags or []))
        q_clip = clip(quote, EVENT_TEXT_CHARS)
        c_clip = clip(context, EVENT_TEXT_CHARS)
        q_str, c_str = q_clip.text, c_clip.text
        s_str = str(source)[:50]
        # One flag for the row: truncated=1 means at least one field is a prefix.
        # original_length keeps the loss measurable; when both fields were cut it
        # holds the longer original, which is the one worth re-reading.
        was_cut = q_clip.truncated or c_clip.truncated
        original_length = max(q_clip.original_length, c_clip.original_length) if was_cut else 0

        with self.lock, self._connect() as conn:
            # Deduplicate identical recent events within 300 seconds (e.g. idle digest loops)
            dup = conn.execute(
                """
                SELECT id FROM member_events
                WHERE user_id=? AND group_id=? AND quote=? AND source=? AND timestamp > ?
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, group_id, q_str, s_str, ts - 300),
            ).fetchone()
            if dup:
                return dup[0]

            cursor = conn.execute(
                """
                INSERT INTO member_events
                    (user_id, group_id, timestamp, quote, context, source, tags,
                     truncated, original_length, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    group_id,
                    ts,
                    q_str,
                    c_str,
                    s_str,
                    json.dumps(tags_list, ensure_ascii=False),
                    int(was_cut),
                    original_length,
                    now,
                ),
            )
            event_id = cursor.lastrowid
            conn.execute("UPDATE member_profiles SET updated_at=? WHERE user_id=?", (now, user_id))
            return event_id

    def parse_explicit_command(self, text):
        """Recognises user banter commands like:

        - @机器人 记在小本本上 [某人] 其实[喜好/事实]
        - @bot 记在小本本上 [某人] 喜欢[某事物]
        - 小本本记录 [内容]
        - 记在小本本上 [某人] [喜好/事实]
        - 快记在小本本上 [某人] [喜好/事实]

        The grammar lives in memory_commands; this method only supplies the
        profile lookups it needs.
        """
        return parse_command(text, self._known_command_names, self._primary_name)

    def _known_command_names(self):
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT primary_name, aliases FROM member_profiles").fetchall()
        return known_names(rows)

    def _primary_name(self, candidate):
        prof = self.find_profile(candidate)
        return prof["primary_name"] if prof else None

    def handle_explicit_command(self, sender_id, sender_name, group_id, text):
        """Processes an explicit banter command, updating DB and compact profiles."""
        # issue #39: guard sender_name (see extract_interaction) before it can
        # reach a profile primary_name/aliases write.
        sender_name = clean_sender_name(sender_name, sender_id)
        cmd = self.parse_explicit_command(text)
        if not cmd.get("is_command"):
            return None

        target = cmd.get("target")
        fact = cmd.get("fact")
        content = cmd.get("content")

        # If target is a QQ number, check if this is an identity annotation (e.g. "是 小明", "其实是小明", "小明 (管理员)")
        if target and str(target).isdigit():
            ident_m = re.match(
                r"^(?:其实(?:是|叫)|原来(?:是|叫)|是|叫|为)?\s*(?P<name>[\w\u4e00-\u9fa5]{2,12})(?:\s*[（\(](?P<role>[^）\)]+)[）\)])?(?:\s*(?:其实|平时|特征|特点|喜欢|爱)?\s*(?P<desc>.+))?$",
                fact,
            )
            if ident_m and ident_m.group("name") not in ("喜欢", "讨厌", "经常", "天天", "特别"):
                p_name = ident_m.group("name").strip()
                p_role = (ident_m.group("role") or "").strip()
                p_desc = (ident_m.group("desc") or "").strip()

                mapping_entry = {
                    "name": p_name,
                    "aliases": [p_name],
                    "role": p_role,
                    "desc": p_desc or fact,
                }
                seed_cand = self.find_profile(p_name)
                if seed_cand and seed_cand["user_id"].startswith("seed:"):
                    mapping_entry["seed_key"] = seed_cand["user_id"]

                self.sync_member_mappings({target: mapping_entry}, group_id=group_id)
                ack_hint = f"认准了！已把 QQ {target} 标注为【{p_name}】{f'（{p_role}）' if p_role else ''}，小本本已锁定，以后发言精准怼他！"

                ev_id = self.record_event(
                    user_id=target,
                    group_id=group_id,
                    quote=text,
                    context=f"标注身份: {p_name} {p_role} {p_desc}".strip(),
                    source="explicit_annotation",
                    tags=["标注", "explicit_annotation"],
                )
                return {
                    "event_id": ev_id,
                    "target": p_name,
                    "fact": fact or content,
                    "user_id": target,
                    "ack_hint": ack_hint,
                }

        # Resolve target profile
        profile = None
        if target:
            profile = self.find_profile(target, group_id=group_id)
            if not profile:
                # Create profile with primary_name=target
                profile = self.save_profile(
                    user_id=f"member:{target}",
                    primary_name=target,
                    aliases=[target],
                    group_id=group_id,
                )
        else:
            # General note or about sender
            profile = self.find_profile(sender_id, group_id=group_id) or self.save_profile(
                user_id=sender_id,
                primary_name=sender_name or sender_id,
                group_id=group_id,
            )
            target = profile["primary_name"]

        user_id = profile["user_id"]
        # Record timeline event
        ev_id = self.record_event(
            user_id=user_id,
            group_id=group_id,
            quote=text,
            context=fact or content,
            source="explicit_command",
            tags=["小本本", "explicit_command"],
        )

        # Update profile key_memories (group-scoped when group_id is present).
        # Dedup against the merged view (issue #5): the overlay alone misses
        # facts that live in the cross-group profile, so the same note would be
        # appended twice. The overlay write carries the whole view as a
        # snapshot, which keeps "read == written" true for the next caller.
        existing_memories, _ = self._memory_view(profile)
        new_memory = f"小本本记录: {fact or content}"
        if new_memory not in existing_memories:
            updated_memories = self._merge_memories(existing_memories, [new_memory])
            if group_id and str(group_id).strip():
                with self.lock, self._connect() as conn:
                    self._upsert_group_memory(
                        conn, user_id, str(group_id).strip(),
                        key_memories=updated_memories, now=time.time(), merge=False,
                    )
            else:
                with self.lock, self._connect() as conn:
                    conn.execute(
                        "UPDATE member_profiles SET key_memories=?, updated_at=? WHERE user_id=?",
                        (json.dumps(updated_memories, ensure_ascii=False), time.time(), user_id),
                    )

        # Formulate funny banter acknowledgment hint for bot
        ack_hint = f"记在小本本上了！这就给 {target} 记上一笔：{fact or content}"

        return {
            "event_id": ev_id,
            "target": target,
            "fact": fact or content,
            "user_id": user_id,
            "ack_hint": ack_hint,
        }

    def extract_interaction(self, sender_id, sender_name, group_id, text, is_direct=False):
        """Piggyback extraction on direct interactions (@bot or reply threads)."""
        if not text or not sender_id:
            return None

        # issue #39: sender_name may be QQ quote-bubble UI text (``<X>回应了你的
        # 消息：<Y>``). Never write that string into primary_name/aliases --
        # fall back to the QQ number so the member stays recognisable.
        sender_name = clean_sender_name(sender_name, sender_id)

        # Update / bind profile with sender info
        profile = self.find_profile(sender_id)
        if not profile:
            # Check if sender_name matches an existing seed profile
            # SAFETY GUARD: Only bind if sender_name is an EXACT match to primary_name or distinct aliases
            # (never match on partial names or generic words like "领导")
            named_profile = None
            if sender_name and len(sender_name.strip()) >= 2 and sender_name.strip() not in ("领导", "群友"):
                candidate_prof = self.find_profile(sender_name.strip())
                if candidate_prof and candidate_prof["user_id"].startswith("seed:"):
                    # Verify exact match on primary_name or distinct aliases
                    p_name = candidate_prof["primary_name"].casefold()
                    c_aliases = [str(a).casefold() for a in json.loads(candidate_prof["aliases"] or "[]") if str(a) != "领导"]
                    if sender_name.strip().casefold() == p_name or sender_name.strip().casefold() in c_aliases:
                        named_profile = candidate_prof

            if named_profile:
                # Bind actual QQ number to this seed profile, and migrate member_events
                old_seed_id = named_profile["user_id"]
                with self.lock, self._connect() as conn:
                    cur_aliases = json.loads(named_profile["aliases"] or "[]")
                    if str(sender_id) not in cur_aliases:
                        cur_aliases.append(str(sender_id))
                    if sender_name and sender_name not in cur_aliases:
                        cur_aliases.append(sender_name)
                    if old_seed_id not in cur_aliases:
                        cur_aliases.append(old_seed_id)
                    conn.execute(
                        "UPDATE member_profiles SET user_id=?, aliases=?, updated_at=? WHERE user_id=?",
                        (str(sender_id), json.dumps(cur_aliases, ensure_ascii=False), time.time(), old_seed_id),
                    )
                    conn.execute(
                        "UPDATE member_events SET user_id=? WHERE user_id=?",
                        (str(sender_id), old_seed_id),
                    )
                profile = self.find_profile(sender_id)
            else:
                profile = self.save_profile(
                    user_id=sender_id,
                    primary_name=sender_name or sender_id,
                    aliases=[sender_name] if sender_name else [],
                )
        else:
            # Ensure sender_name is in aliases
            if sender_name:
                cur_aliases = json.loads(profile["aliases"] or "[]")
                if sender_name not in cur_aliases:
                    self.save_profile(user_id=profile["user_id"], primary_name=profile["primary_name"], aliases=[sender_name], group_id=group_id)

        # Piggyback preference extraction:
        # Check for first-person declarations: "我喜欢...", "我平时爱玩...", "我其实最爱..."
        extracted_interests = []
        interest_match = re.search(r"我(?:其实|平时|最|特别|经常|超)*(?:喜欢玩|爱玩|喜欢|爱|超爱|沉迷|在玩)\s*([A-Za-z0-9\u4e00-\u9fa5]{2,16}?)(?:了|呢|啊|哈|[，。！？\s]|$)", text)
        if interest_match:
            item = interest_match.group(1).strip()
            if item not in ("你", "大家", "什么", "这个", "那个", "哈哈", "呵呵", "嘿嘿", "嘻嘻", "草", "乐"):
                extracted_interests.append(item)

        # First-person trait declarations: "我才不是...", "我是..."
        extracted_traits = {}
        trait_match = re.search(r"我(?:才不是|不是|其实是|是个)\s*([A-Za-z0-9\u4e00-\u9fa5]{2,16}?)(?:了|呢|啊|哈|[，。！？\s]|$)", text)
        if trait_match:
            claim = trait_match.group(1).strip()
            if "不是" in text or "才不是" in text:
                extracted_traits["self_claim"] = f"否认{claim}/嘴硬"
            else:
                extracted_traits["self_claim"] = claim

        # Update profile if any info was extracted
        if extracted_interests or extracted_traits:
            profile = self.save_profile(
                user_id=profile["user_id"],
                primary_name=profile["primary_name"],
                interests=extracted_interests,
                traits=extracted_traits,
                group_id=group_id,
            )

        # Record interaction event
        if is_direct or extracted_interests or extracted_traits:
            self.record_event(
                user_id=profile["user_id"],
                group_id=group_id,
                quote=text[:200],
                context=f"直接互动: {'提取喜好 ' + ','.join(extracted_interests) if extracted_interests else '日常交流'}",
                source="direct_interaction" if is_direct else "interaction",
                tags=extracted_interests or ["互动"],
            )

        return self.find_profile(profile["user_id"])

    def digest_idle_batch(self, group_id, messages):
        """Light-weight batch digestion of recent group chat to extract member habits, tone, and preferences.

        Called during idle windows or periodically.
        """
        if not messages or not isinstance(messages, list):
            return {"digested_count": 0, "updated_members": []}

        updated = []
        for m in messages:
            if not isinstance(m, dict) or m.get("is_self") or m.get("synthetic"):
                continue
            sender_id = str(m.get("sender_id") or m.get("user_id") or "").strip()
            if not sender_id or sender_id == "0":
                continue

            sender_name = str(
                m.get("sender_name")
                or m.get("nickname")
                or (m.get("sender", {}).get("nickname") if isinstance(m.get("sender"), dict) else m.get("sender"))
                or ""
            ).strip()
            # issue #39: guard the upstream name before the direct save_profile
            # below can write bubble UI text into primary_name.
            sender_name = clean_sender_name(sender_name, sender_id)
            text = str(m.get("content") or m.get("text") or m.get("message") or "").strip()
            if not text:
                continue

            # Check if this is an explicit command
            cmd = self.parse_explicit_command(text)
            if cmd.get("is_command"):
                # Handle command with deduplication protection
                res = self.handle_explicit_command(sender_id, sender_name, group_id, text)
                if res and res["target"] not in updated:
                    updated.append(res["target"])
                continue

            # Extract any preference declarations in background messages
            self.extract_interaction(sender_id, sender_name, group_id, text, is_direct=False)

            # Tone & jargon observation: check for signature expressions
            prof = self.find_profile(sender_id, group_id=group_id)
            if not prof:
                prof = self.save_profile(user_id=sender_id, primary_name=sender_name or sender_id, group_id=group_id)

            tone_clues = []
            if any(w in text for w in ("草", "乐", "难绷", "蚌埠住了", "6", "？", "何意味", "幽默")):
                tone_clues.append("沙雕梗多")
            if any(w in text for w in ("健身", "运动", "肌肉", "跑步", "游泳")):
                tone_clues.append("健身/运动爱好者")
                self.save_profile(user_id=prof["user_id"], primary_name=prof["primary_name"], interests=["健身/运动"], group_id=group_id)
            if any(w in text for w in ("原神", "启动", "三国杀", "蒸", "崩铁", "鸣潮", "绝区零", "黑神话")):
                tone_clues.append("二次元/游戏党")
                self.save_profile(user_id=prof["user_id"], primary_name=prof["primary_name"], interests=["游戏"], group_id=group_id)

            if tone_clues and not prof.get("tone_style"):
                self.save_profile(
                    user_id=prof["user_id"],
                    primary_name=prof["primary_name"],
                    tone_style="、".join(tone_clues),
                    group_id=group_id,
                )
                if prof["primary_name"] not in updated:
                    updated.append(prof["primary_name"])

        return {"digested_count": len(messages), "updated_members": updated}

    def observe_message(self, group_id, payload):
        """Unified entry point for incoming messages from MCPChat.receive."""
        if not payload or payload.get("is_self") or payload.get("synthetic"):
            return None

        sender_id = str(payload.get("sender_id", "")).strip()
        sender_name = str(payload.get("sender", "")).strip()
        text = str(payload.get("content", "")).strip()

        if not text or not sender_id:
            return None

        # Check explicit command first
        cmd = self.parse_explicit_command(text)
        if cmd.get("is_command"):
            # issue #220: "记在小本本上 摸鱼才是正经事" 是群里随口一句，前缀撞上
            # ACTION_PREFIX 就被当指令。解析层只认语法（命中前缀即 True），意图门加
            # 在这层：命中后还要"是不是在叫机器人"。判据（与 followup.mention_info、
            # mcp_chat_prompts.social_state 同口径）——segments 里有 at 段 qq == 本机，
            # 或文本里显式带 @机器人/@bot/@AI/@<account>（segments 被超 20KB 清空时的
            # 兜底，mcp_chat.py:482）。判据不满足就降级走 extract_interaction：不写
            # 小本本记录、不凭空建 member:<句首两三字>。reply 指向本机的判据这轮不做，
            # 那要查 chat_inbox 的 own 回执，与本库（group_memories.sqlite3）跨库。
            account = self._self_account(payload)
            if self._addressed_to_bot(payload, account):
                return self.handle_explicit_command(sender_id, sender_name, group_id, text)

        # Only @-this-bot counts as direct. "@别人" / "回复别人" used to set is_direct
        # (any at/reply segment), which polluted the interaction path the same way.
        addressed = self._addressed_to_bot(payload, self._self_account(payload))
        return self.extract_interaction(sender_id, sender_name, group_id, text, is_direct=addressed)

    def _self_account(self, payload):
        """The bot's own account for this message, from conversation_id.

        ``conversation_id`` is ``{account}:{kind}:{group}`` (mcp_chat.receive).
        Missing / malformed -> empty string, which makes ``_addressed_to_bot``
        return False: a message we cannot attribute degrades to a plain text
        observation, never to a fabricated notebook entry.
        """
        cid = str(payload.get("conversation_id", "") or "")
        return cid.split(":", 1)[0].strip() if ":" in cid else ""

    def _addressed_to_bot(self, payload, account):
        """True when the message's ``at`` segment points at our own account.

        ``content`` carries ``@<display name>`` after mcp_chat.receive replaced
        the bare QQ, so only ``segments`` carries the original ``qq``. The
        text-level fallback (``@机器人`` / ``@<account>``) covers the rare row
        whose ``segments`` were dropped for the 20KB inbox ceiling.
        """
        if not account:
            return False
        segs = payload.get("segments", [])
        if isinstance(segs, list):
            for seg in segs:
                if isinstance(seg, dict) and seg.get("type") == "at" and str(seg.get("qq", "")) == account:
                    return True
        return self._text_addresses_bot(str(payload.get("content", "") or ""), account)

    def _text_addresses_bot(self, text, account):
        """Text fallback: did the sender write ``@机器人`` / ``@bot`` / ``@<qq>``?

        Fixed addresses only, mirroring ``memory_commands.BOT_PREFIX`` but
        requiring them to actually appear (the prefix in the grammar is an
        optional group). ``self_names`` from config is the wake layer's concern;
        memory observation does not reverse-depend on it.
        """
        if not text:
            return False
        addresses = ("@机器人", "@bot", "@ai", f"@{account}")
        lowered = text.lower()
        return any(addr.lower() in lowered for addr in addresses)
