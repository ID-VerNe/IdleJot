"""Pure rendering helpers for the group-memory engine.

Kept free of database access so the compact profile card can be unit tested
and reused without opening a connection.
"""
import json


def load_json(value, default):
    """Decode a stored JSON column, falling back to ``default``."""
    if not value:
        return default
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return default
    return decoded if isinstance(decoded, type(default)) else default


def normalize_list(val):
    """Normalise a string or iterable into a deduplicated list of strings."""
    if val is None:
        return None
    if isinstance(val, str):
        val = [val]
    elif not isinstance(val, (list, tuple, set)):
        val = [str(val)]
    res = []
    for item in val:
        s = str(item).strip()
        if s and s not in res:
            res.append(s)
    return res


def merge_text_list(current, incoming, limit=None, refresh_restated=False):
    """Merge two fact lists, collapsing restatements of the same fact.

    Exact-match dedup is not enough for a dossier that gets appended to on every
    consolidation pass. The same trait comes back reworded -- "听八卦" this week,
    "听八卦（单发一个👂就算表态）" the next -- and both survive, so the memory
    cap fills up with restatements and genuinely new material gets
    squeezed out. A shorter entry already contained in a longer one adds no
    information, so the longer wording wins.

    Substring comparison only kicks in at three characters: below that it starts
    matching unrelated fragments ("+1", "120") and eats real facts.

    With ``refresh_restated`` the list doubles as a recency queue: a fact that
    comes back (exact match, or contained in the new wording) moves to the tail.
    Callers can then evict the head without storing timestamps or access
    counters -- the least recently restated fact is the one that goes. The
    default keeps the historical insertion-order behaviour for lists such as
    interests, where position carries no retention meaning.

    When ``limit`` is exceeded the newest entries are the ones kept. An old fact
    has usually been restated since; a fresh one has not.
    """
    merged = []
    for raw in list(current or []) + list(incoming or []):
        item = str(raw).strip()
        if not item:
            continue
        restated = next(
            (kept for kept in merged if item == kept or (len(item) >= 3 and item in kept)),
            None,
        )
        if restated is not None:
            if refresh_restated:
                merged = [kept for kept in merged if kept != restated]
                merged.append(restated)
            continue
        if refresh_restated:
            # Absorb every earlier entry the new wording covers, then append the
            # survivor so a restatement also refreshes its position.
            merged = [
                kept for kept in merged if not (len(kept) >= 3 and kept in item)
            ]
            merged.append(item)
            continue
        # Absorb every earlier entry this one already covers, not just the first:
        # otherwise a single longer wording leaves the shorter restatements behind.
        seat = None
        kept_next = []
        for kept in merged:
            if len(kept) >= 3 and kept in item:
                if seat is None:
                    seat = len(kept_next)
                continue
            kept_next.append(kept)
        if seat is None:
            kept_next.append(item)
        else:
            kept_next.insert(seat, item)
        merged = kept_next
    if limit and len(merged) > limit:
        merged = merged[-limit:]
    return merged


def render_l1_card(profile, group_event_note=""):
    """Render one ~80 token Tier 1 profile card.

    ``profile`` is a row mapping with user_id, primary_name, aliases, traits,
    tone_style, interests and key_memories. ``group_event_note`` is an already
    resolved, group-scoped snippet (or empty).
    """
    uid = profile["user_id"]
    p_name = profile["primary_name"]
    aliases = load_json(profile.get("aliases"), [])
    traits = load_json(profile.get("traits"), {})
    card_name = traits.get("card") or ""
    nickname = traits.get("nickname") or ""

    id_parts = []
    if uid and not uid.startswith("seed:") and not uid.startswith("member:"):
        id_parts.append(f"QQ: {uid}")
    if card_name and card_name != p_name:
        id_parts.append(f"名片: {card_name}")
    elif nickname and nickname != p_name:
        id_parts.append(f"昵称: {nickname}")
    id_str = f" ({' / '.join(id_parts)})" if id_parts else ""

    filtered_aliases = [a for a in aliases if a not in (p_name, uid, card_name, nickname)][:3]
    alias_str = "/".join(filtered_aliases) or "无"

    tone = profile.get("tone_style") or "普通群友"
    interests = load_json(profile.get("interests"), [])
    interest_str = "、".join(interests[:4]) or "暂无明确"
    memories = load_json(profile.get("key_memories"), [])
    # Memory order is least-recently-restated first (see merge_text_list), so
    # the tail holds the freshest material. Still two entries: the storage cap
    # is not a prompt budget.
    mem_str = "；".join(memories[-2:]) or "暂无专属记录"
    location = str(traits.get("location") or traits.get("地域") or "").strip()

    note = f" | 本群八卦: {group_event_note[:45]}" if group_event_note else ""
    loc_str = f" | 地域: {location}" if location else ""
    return (
        f"【群友档案：{p_name}{id_str}】外号: {alias_str} | 风格: {tone} "
        f"| XP喜好: {interest_str} | 核心记忆: {mem_str}{loc_str}{note}"
    )
