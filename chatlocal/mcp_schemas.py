"""Shared MCP wire-contract fragments.

Every helper returns a fresh dict: callers merge these into their own
property dicts, and mcp_tools rewrites descriptions in place, so a
shared mutable dict would leak edits between tools.
"""

SESSION_ID_DESCRIPTION = '原样复制工具返回的字符串；JSON 中必须带双引号。'
TURN_KEY_DESCRIPTION = '可省略，由程序按当前批次/计划生成稳定编号；重试不能换参数。'


def session_id(description=''):
    """The 32-hex session id every chat tool takes."""
    field = dict(type='string', minLength=32, maxLength=32)
    if description:
        field['description'] = description
    return field


def idempotency_key(description=''):
    """The 8-80 char retry-stable operation key."""
    field = dict(type='string', minLength=8, maxLength=80)
    if description:
        field['description'] = description
    return field


def event_id(description=''):
    """A chat message event id, bounded to SQLite-safe integers."""
    field = dict(type='integer', minimum=1, maximum=2**63-1)
    if description:
        field['description'] = description
    return field
