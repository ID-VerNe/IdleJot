"""Explicit banter-command grammar for the group-memory engine.

The patterns are compiled once at import time; the engine calls
``parse_command`` with two callbacks so this module stays free of database
access.
"""
import json
import re

# @bot, @123456, bot-style nicknames, punctuation and conversational prefixes.
# Bot nicknames must be configured in config.json `self_names`; the grammar
# only hardcodes the generic `bot`/`机器人` forms (issue #76).
BOT_PREFIX = r"(?:(?:@\d+|@?(?:机器人|bot|AI))\s*[，,：:!！\s]*)?"

ANNOTATE_PREFIX = r"(?:(?:请|帮我|麻烦|快)?(?:标注|标记|绑定|记住|认认|认一下|叫他|认准))\s*"
ANNOTATE = re.compile(
    r"^" + BOT_PREFIX + ANNOTATE_PREFIX
    + r"(?P<target>\[CQ:at,qq=\d+\]|@\d+|\d{5,12}|[\w\u4e00-\u9fa5]{2,16})\s*(?:[是为：:]\s*|\s+)(?P<info>.+)$",
    re.IGNORECASE,
)

ACTION_PREFIX = (
    r"(?:(?:请|帮我|麻烦|快)?(?:在)?小本本(?:上)?(?:记(?:录|下|上一笔|一笔|一下)?|写上)"
    r"|(?:请|帮我|麻烦|快)?(?:记在|记入|写在|写进|记)小本本(?:上)?(?:记|录|写)?"
    r"|(?:拿|用)小本本(?:记(?:录|下|上一笔|一笔|一下)?|写上))"
)
COMMAND = re.compile(
    r"^" + BOT_PREFIX + ACTION_PREFIX + r"\s*[：:，,\s]\s*(?P<content>.+)$"
    r"|^" + BOT_PREFIX + ACTION_PREFIX + r"\s*(?P<content2>\S.+)$",
    re.IGNORECASE,
)

LEADING_AT = re.compile(
    r"^(?:给\s*)?(?P<target>\[CQ:at,qq=\d+\]|@\d+|\d{5,12})\s*"
    r"(?:记上|记一笔|记上一笔)?\s*(?:[：:，,\s]\s*)?(?P<fact>.+)$"
)
COLON = re.compile(
    r"^(?:给\s*)?(?P<target>[\w\u4e00-\u9fa5]{2,16}?)(?:\s*(?:记上|记一笔|记上一笔))?\s*[：:]\s*(?P<fact>.+)$"
)
TRANSITION = re.compile(
    r"^(?:给\s*)?(?P<target>[\w\u4e00-\u9fa5]{2,12}?)\s*(?:记上|记一笔|记上一笔)?\s*"
    r"(?P<fact>(?:其实|原来|特别|非常|最|平时|经常|天天|整天|又|一直|每天|是个|是|爱|喜欢|讨厌|沉迷).+)$"
)
QQ_IN_TEXT = re.compile(r"\d{5,12}")

NOT_COMMAND = {"is_command": False}


def _target(raw):
    """Collapse an @-mention or CQ code to a bare QQ number when present."""
    text = raw.strip()
    match = QQ_IN_TEXT.search(text)
    return match.group(0) if match else text


def known_names(rows):
    """Collect primary names and aliases worth matching, longest first."""
    names = set()
    for row in rows:
        names.add(row["primary_name"])
        for alias in json.loads(row["aliases"] or "[]"):
            alias = str(alias)
            if len(alias) >= 2 and alias not in ("领导", "群友"):
                names.add(alias)
    return sorted(names, key=len, reverse=True)


def parse_command(text, names_provider, resolve_name):
    """Recognise an explicit "write it in the notebook" command.

    ``names_provider`` returns known names ordered longest first and is only
    called when the cheaper patterns have already failed. ``resolve_name`` maps
    an alias to its primary profile name (or None).
    """
    if not text or not isinstance(text, str):
        return dict(NOT_COMMAND)

    raw = text.strip()

    annotated = ANNOTATE.match(raw)
    if annotated:
        return {
            "is_command": True,
            "is_annotation": True,
            "target": _target(annotated.group("target")),
            "fact": annotated.group("info").strip(),
            "content": raw,
        }

    matched = COMMAND.match(raw)
    if not matched:
        return dict(NOT_COMMAND)

    content = (matched.group("content") or matched.group("content2") or "").strip()
    if not content:
        return dict(NOT_COMMAND)

    leading = LEADING_AT.match(content)
    if leading:
        return {
            "is_command": True,
            "target": _target(leading.group("target")),
            "fact": leading.group("fact").strip(),
            "content": content,
        }

    colon = COLON.match(content)
    if colon:
        return {
            "is_command": True,
            "target": colon.group("target").strip(),
            "fact": colon.group("fact").strip(),
            "content": content,
        }

    for candidate in names_provider():
        if content.startswith(candidate):
            fact = content[len(candidate):].strip(" ：:，,")
            if fact:
                return {
                    "is_command": True,
                    "target": resolve_name(candidate) or candidate,
                    "fact": fact,
                    "content": content,
                }

    transition = TRANSITION.match(content)
    if transition:
        return {
            "is_command": True,
            "target": transition.group("target").strip(),
            "fact": transition.group("fact").strip(),
            "content": content,
        }

    parts = content.split(maxsplit=1)
    if len(parts) == 2 and 2 <= len(parts[0]) <= 12:
        return {"is_command": True, "target": parts[0].strip(), "fact": parts[1].strip(), "content": content}

    return {"is_command": True, "target": None, "fact": content, "content": content}
