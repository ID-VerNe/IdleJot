"""OneBot 11 message segments shared by ingest and detail rendering.

Both the live ingest path and the quoted-message renderer rebuild segments
from raw OneBot payloads. Keeping the field names and truncation limits here
stops the two paths from drifting apart.
"""

ID_LIMIT = 100
QQ_LIMIT = 30
NAME_LIMIT = 100


def message_id(raw):
    """Normalise an OneBot message id for use inside a segment."""
    return str(raw or '')[:ID_LIMIT]


def reply(raw_id):
    return dict(type='reply', onebot_message_id=message_id(raw_id))


def qq(raw_qq):
    """Normalise an @-target QQ number."""
    return str(raw_qq or '')[:QQ_LIMIT]


def name(raw_name):
    """Normalise the display name carried by an @ segment (issue #52).

    OneBot's raw at segment has ``data.name`` (the @-ed member's display
    name). Dropping it here is what made every wake line render a bare
    ``@``; the ingest and detail-render paths share this normaliser so
    they cannot drift apart on the field name or the limit. Whitespace-only
    names count as absent -- ``@`` followed by blanks is the original defect
    wearing a disguise.
    """
    return str(raw_name or '').strip()[:NAME_LIMIT]


def at(raw_qq, raw_name=None):
    segment = dict(type='at', qq=qq(raw_qq))
    display = name(raw_name)
    if display:
        segment['name'] = display
    return segment
