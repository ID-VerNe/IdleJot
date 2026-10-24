"""Static app-plugin surface: names only, no imports from the rest of chatlocal.

Kept separate from the plugin modules so two low-level consumers can read it
without importing the features themselves:

* ``mcp_access`` needs the scope flag names to validate grants.
* ``mcp_chat`` needs the tool names at import time for ``CHAT_TOOLS``.

One plugin adds one block here plus one module next to this package.
"""

REACTIONS = dict(
    name='reactions',
    tool='react_to_chat_message',
    scope='chat_reactions',
    expression='reaction',
    open_world=True,
    # 本地开关文件名，与 Access.path（data/mcp-access.sqlite3）同目录。
    # 放在这里是为了让只读状态（up.py --status）不必 import 功能模块。
    policy='mcp-reactions.json',
)

PLUGINS = (REACTIONS,)

TOOL_NAMES = frozenset(item['tool'] for item in PLUGINS)
SCOPE_FLAGS = tuple(item['scope'] for item in PLUGINS if item['scope'])
SCOPE_BY_TOOL = {item['tool']: item['scope'] for item in PLUGINS if item['scope']}
EXPRESSIONS = frozenset(item['expression'] for item in PLUGINS if item['expression'])
OPEN_WORLD = frozenset(item['tool'] for item in PLUGINS if item['open_world'])
