"""The one place that decides how much of a long text leaves a data layer.

issue #90: the dossier reader cut every message at 400 characters and said
nothing, so a 1524-character message reached the consolidation model as a
half-sentence and no field in the payload hinted that anything was missing. The
same defect existed twice more -- a 500-character cut on the summary written back
to memory, and a silent 1000-character cap inside ``record_event``. Three copies
of "shrink the text", two different limits, and only the chat reader told the
caller.

So the cut lives here, once:

* :func:`clip` returns what survived *and* whether anything was lost, so a
  caller can either compensate (re-read the full message) or at least know its
  conclusion rests on a prefix.
* Limits are named for their consumer instead of being sprinkled at call sites.
  ``READ_PREVIEW_CHARS`` is what a chat reader hands an agent;
  ``EVENT_TEXT_CHARS`` is one memory-event field, aligned with the 12000-char
  body ceiling ``chatlocal/mcp_chat.py`` already applies to a single message.

Clipping prefers a sentence boundary. A hard cut at an arbitrary offset is what
split a word in half in the report; backing up to the last full sentence keeps
the prefix readable even though it is shorter.
"""
from typing import NamedTuple

# What a chat reader hands an agent for one message.
READ_PREVIEW_CHARS = 2000

# One text field of a memory event (quote / context). 12000 is not a new number:
# it is the body ceiling chatlocal/mcp_chat.py already uses for a single message,
# so a payload that was accepted there is not silently shortened on the way into
# the timeline.
EVENT_TEXT_CHARS = 12000

# Sentence ends worth cutting after. Newline counts too: long material is pasted
# as paragraphs, and a paragraph edge reads better than a mid-sentence chop.
_BOUNDARIES = "。！？；…\n!?;"

# How far back to look for one of the above: 10% of the limit, never less than
# this many characters. Below that the search starts matching the previous
# sentence and throws away more of the budget than the readability is worth.
_MIN_WINDOW = 40


class Clipped(NamedTuple):
    """Result of :func:`clip` -- the surviving text plus what it cost."""

    text: str
    truncated: bool
    original_length: int


def clip(text, limit):
    """Shorten ``text`` to ``limit`` characters, reporting the loss.

    ``truncated`` is the contract every caller shares: a payload is either whole
    or it says it is not, and ``original_length`` says how much there was. The
    text itself is never annotated with an ellipsis -- callers quote it back to
    users and compare it for deduplication, so the marker lives in the field, not
    in the prose.
    """
    if limit <= 0:
        raise ValueError("clip() needs a positive limit")
    value = "" if text is None else str(text)
    total = len(value)
    if total <= limit:
        return Clipped(value, False, total)

    cut = limit
    window = max(_MIN_WINDOW, limit // 10)
    for index in range(limit - 1, max(limit - window, 0) - 1, -1):
        if value[index] in _BOUNDARIES:
            cut = index + 1
            break
    return Clipped(value[:cut].rstrip(), True, total)
