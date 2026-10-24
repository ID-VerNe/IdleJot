"""The one place that decides whether an upstream name is a real display name.

issue #39: a group member set their QQ card to a quote-bubble string
(``<昵称>回应了你的消息：<对方昵称>``). OneBot returns that string verbatim as
``sender.card``, and ``chatlocal/mcp_chat.py`` trusted it without any shape
check, so the bubble text travelled into ``chat_inbox.payload.sender``, then
into the dossier's ``aliases`` (via ``memory_observe.extract_interaction`` →
``save_profile``) and the wake-line / ``--list`` / ``--context`` display name.
The agent could no longer recognise the member by their real card.

QQ renders that bubble as part of the *message UI*, not as a group card, so it
is never a legitimate display name. The rule lives here, once, instead of being
sprinkled at call sites, and it is deliberately enumerable -- a comment-listed
set of patterns, not a magic regex.

``clean_sender_name`` is what a caller feeds an upstream name through before it
may enter a payload or a profile:

* A name that matches a UI-bubble pattern, or is blank/whitespace, falls back to
  ``fallback`` (the caller passes the QQ number, so the member stays recognisable
  without ever leaking bubble text).
* A legitimate name (any card/nickname that does not look like a UI bubble)
  passes through untouched.
"""
from __future__ import annotations

# Patterns that are QQ message-UI chrome, never a group card or nickname.
# ``回应了你的消息`` is the quote-bubble frame ("<X> replied to your message");
# the surrounding member names vary, so the match is on the frame only.
# Keep this list flat and documented; add a new literal here, not a new branch
# at a call site. Only patterns actually observed on the wire go in -- a
# speculative English frame is skipped rather than risk a false positive on a
# real nickname.
_UI_BUBBLE_FRAMES = (
    "回应了你的消息",
)


def _is_ui_bubble(name):
    """True when ``name`` reads as QQ quote-bubble UI text rather than a card."""
    return any(frame in name for frame in _UI_BUBBLE_FRAMES)


def clean_sender_name(name, fallback):
    """Return a display-safe name, falling back to ``fallback`` when ``name``
    is bubble UI text or blank.

    ``fallback`` should be a stable identifier the caller already holds (the QQ
    number); it is what the member is shown as when their upstream name cannot
    be trusted. A caller should never pass a second untrusted name as the
    fallback -- that would just re-import the bubble.
    """
    value = "" if name is None else str(name).strip()
    if not value or _is_ui_bubble(value):
        return fallback
    return value