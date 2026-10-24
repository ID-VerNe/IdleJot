"""Static app-plugin surface: names only, no imports from the rest of chatlocal.

Kept separate from the plugin modules so two low-level consumers can read it
without importing the features themselves:

* ``mcp_access`` needs the scope flag names to validate grants.
* ``mcp_chat`` needs the tool names at import time for ``CHAT_TOOLS``.

One plugin adds one block here plus one module next to this package.

``scope`` is the plugin's own grant flag, or ``None`` when the feature has no
flag of its own: those tools inherit the plain ``chat`` authorization and
authorize each call themselves (the language library binding re-checks
account/group/grant per call). ``expression`` is the bubble kind this plugin
feeds the reply plan with, or ``None`` when its tools dispatch directly before
``permission()`` -- a plan-independent tool authorizes itself, the same way a
scopeless one does.
"""
REACTIONS = dict(
    name='reactions',
    tools=('react_to_chat_message',),
    scope='chat_reactions',
    expression='reaction',
    open_world=True,
    # 本地开关文件名，与 Access.path（data/mcp-access.sqlite3）同目录。
    # 放在这里是为了让只读状态（up.py --status）不必 import 功能模块。
    policy='mcp-reactions.json',
)

LANGUAGE = dict(
    name='language',
    tools=('get_chat_learning', 'claim_chat_learning',
           'submit_chat_learning', 'select_chat_expressions'),
    # 无独立授权位：沿用 chat 授权，库绑定（binding/authorized_library）按
    # 账号/群/连接逐次自校验。唤醒事件名由插件类声明，不进静态面。
    scope=None,
    expression=None,
    # 开关是按账号+群的数据态（language_libraries.enabled），不是 policy 文件。
    policy=None,
)

PLUGINS = (REACTIONS, LANGUAGE)

TOOL_NAMES = frozenset(tool for item in PLUGINS for tool in item['tools'])
SCOPE_FLAGS = tuple(item['scope'] for item in PLUGINS if item['scope'])
SCOPE_BY_TOOL = {tool: item['scope'] for item in PLUGINS if item['scope']
                 for tool in item['tools']}
EXPRESSIONS = frozenset(item['expression'] for item in PLUGINS if item['expression'])
OPEN_WORLD = frozenset(tool for item in PLUGINS if item.get('open_world')
                       for tool in item['tools'])
