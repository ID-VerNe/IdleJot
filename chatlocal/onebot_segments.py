"""OneBot 11 message segments shared by ingest and detail rendering.

Both the live ingest path and the quoted-message renderer rebuild segments
from raw OneBot payloads. Keeping the field names and truncation limits here
stops the two paths from drifting apart.
"""

ID_LIMIT = 100
QQ_LIMIT = 30


def message_id(raw):
    """Normalise an OneBot message id for use inside a segment."""
    return str(raw or '')[:ID_LIMIT]


def reply(raw_id):
    return dict(type='reply', onebot_message_id=message_id(raw_id))


def qq(raw_qq):
    """Normalise an @-target QQ number."""
    return str(raw_qq or '')[:QQ_LIMIT]


def at(raw_qq):
    return dict(type='at', qq=qq(raw_qq))
