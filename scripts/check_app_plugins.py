"""App-side plugin protocol gate (issue #4 PD).

No MCP transport, no OneBot socket, no real database: one plugin is built
against a throwaway SQLite file and the protocol surface the host relies on is
checked directly. The end-to-end behaviour stays covered by
``check_mcp_reactions.py``; this gate covers the *socket*.

Run with the repo venv:

    .venv/Scripts/python.exe scripts/check_app_plugins.py
"""
from __future__ import annotations

import atexit
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chatlocal import app_plugins                                  # noqa: E402
from chatlocal.agent_tools import tool                             # noqa: E402
from chatlocal.app_plugins import spec                             # noqa: E402
from chatlocal.app_plugins.loader import build                     # noqa: E402
from chatlocal.app_plugins.reactions import ChatReactions          # noqa: E402
from chatlocal.mcp_access import CHAT_EXTRA_FLAGS, PERMISSION_FLAGS  # noqa: E402
from chatlocal.mcp_chat import CHAT_TOOLS                          # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix='wg_app_plugins_'))
atexit.register(shutil.rmtree, TMP, True)

# Expressions the host dispatches itself; a plugin may not claim one of these.
BUILTIN_EXPRESSIONS = frozenset(('text', 'sticker', 'voice', 'ai_voice', 'video', 'file', 'qq_action'))


class FakeAccess:
    """Only what the plugin protocol touches: one path and one connection."""

    def __init__(self, path):
        # Path, not str: the language feature derives sibling files from it.
        self.path = Path(path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()


class FakeChat:
    def __init__(self, path):
        self.access = FakeAccess(path)
        self.actions = None


def new_plugin():
    # A fresh file per call: `with sqlite3.connect(...)` commits but does not
    # close, and Windows will not let us reuse a still-open database.
    path = TMP / f'chats-{len(list(TMP.glob("chats-*.sqlite3")))}.sqlite3'
    db = sqlite3.connect(path)
    try:
        with db:
            db.execute('CREATE TABLE chat_sessions(id TEXT PRIMARY KEY)')
    finally:
        db.close()
    chat = FakeChat(path)
    return ChatReactions(chat), chat


def check_static_surface():
    assert app_plugins.TOOL_NAMES, 'no app plugin tools registered'
    missing = app_plugins.TOOL_NAMES - CHAT_TOOLS
    assert not missing, f'plugin tools missing from CHAT_TOOLS: {sorted(missing)}'
    # 每块必须显式声明 scope/expression：缺键会在 spec 推导时 KeyError，但隐式
    # 省略比显式 None 难 review——None 是「没有」的声明，省略是「忘了」。
    for item in spec.PLUGINS:
        assert 'scope' in item, f"{item['name']} must declare scope explicitly (None = no flag)"
        assert 'expression' in item, f"{item['name']} must declare expression explicitly (None = direct dispatch)"
        assert item['tools'], f"{item['name']} registers no tools"
    scoped = {tool for item in spec.PLUGINS if item['scope'] for tool in item['tools']}
    assert set(app_plugins.SCOPE_BY_TOOL) == scoped, 'SCOPE_BY_TOOL must cover exactly the scoped tools'
    assert set(app_plugins.SCOPE_FLAGS) == set(CHAT_EXTRA_FLAGS), (app_plugins.SCOPE_FLAGS, CHAT_EXTRA_FLAGS)
    for flag in app_plugins.SCOPE_FLAGS:
        assert flag in PERMISSION_FLAGS, f'{flag} not granted by PERMISSION_FLAGS'
    overlap = app_plugins.EXPRESSIONS & BUILTIN_EXPRESSIONS
    assert not overlap, f'plugin claims a built-in expression: {sorted(overlap)}'
    assert app_plugins.OPEN_WORLD <= app_plugins.TOOL_NAMES
    print(f'  ok  静态面：{len(app_plugins.TOOL_NAMES)} 个工具、{len(app_plugins.SCOPE_FLAGS)} 个授权位、'
          f'{len(app_plugins.EXPRESSIONS)} 个表达')


def check_registry():
    _, chat = new_plugin()
    apps = build(chat)
    assert isinstance(apps.get('reactions'), ChatReactions), 'build() must instantiate the registered class'
    owner = apps.tool_owner('react_to_chat_message')
    assert owner is not None and owner.name == 'reactions'
    assert apps.tool_owner('send_chat_message') is None
    assert apps.expression_owner('reaction') is owner
    assert apps.expression_owner('text') is None
    assert 'reactions' in apps.stamps()
    assert re.fullmatch(r'[0-9a-f]{32}', apps.stamps()['reactions'])
    assert apps.brief('reactions')
    assert apps.brief('messages') is None
    # A recall notice must reach the host's own handler, not be eaten here.
    assert apps.receive({'post_type': 'notice', 'notice_type': 'group_recall'}) is False
    assert apps.receive({'post_type': 'message', 'message_type': 'group'}) is False
    assert apps.receive({'post_type': 'request'}) is False
    # language：四个工具同一 owner；无授权位、无表达（调用前自授权）；
    # 唤醒事件名是线报名而不是插件名，注册表按键仍只有插件名。
    language = apps.get('language')
    assert language is not None, 'build() must instantiate the language plugin'
    for tool_name in spec.LANGUAGE['tools']:
        assert apps.tool_owner(tool_name) is language, f'{tool_name} must belong to language'
    assert language.expression is None and language.scope_flag is None
    assert language.wake_event == 'learning_ready'
    assert language.wake_event != language.name, 'a differing wire event must be declared, not defaulted'
    assert apps.expression_owner('reaction') is owner, 'language must not claim the reaction expression'
    assert apps.brief('learning_ready') is None, 'a wake event is not a registry key'
    print('  ok  注册表：工具/表达/戳/提示路由，且不吃别人的事件')


def check_scope_and_disable():
    plugin, _ = new_plugin()
    scope = {'chat': True, 'chat_reactions': True}
    group = {'conversation_id': '111:group:222'}
    direct = {'conversation_id': '111:direct:333'}
    assert plugin.scope(scope, group)['chat_reactions'] is True
    assert plugin.scope(scope, direct)['chat_reactions'] is False, 'private chat must clear the flag'
    assert plugin.schemas(tool, scope) and len(plugin.schemas(tool, scope)) == 1
    assert plugin.schemas(tool, {'chat': True}) == ()
    # The switch has to remove the tool, not just hide it from the prompt.
    plugin.policy_path.write_text(json.dumps({'enabled': False}), 'utf-8')
    assert plugin.enabled() is False
    assert plugin.schemas(tool, scope) == ()
    assert plugin.scope(scope, group)['chat_reactions'] is False, 'disabled plugin must clear its own flag'
    plugin.policy_path.write_text(json.dumps({'enabled': True}), 'utf-8')
    assert plugin.enabled() is True and len(plugin.schemas(tool, scope)) == 1
    print('  ok  开关：停用即降级（工具消失、授权位清空），私聊自动关掉自己')


def check_host_has_no_name_branches():
    """Rule 3 of the protocol: the host never names a plugin."""
    # 兼容属性所在宿主，与功能模块自身（它本来就拥有这些名字）不在扫描范围。
    allowed = {'mcp_chat.py', 'mcp_language_learning.py'}
    literals = ("'chat_reactions'", '"chat_reactions"', "'react_to_chat_message'", '"react_to_chat_message"',
                "'get_chat_learning'", '"get_chat_learning"', "'claim_chat_learning'", '"claim_chat_learning"',
                "'submit_chat_learning'", '"submit_chat_learning"', "'select_chat_expressions'", '"select_chat_expressions"')
    offenders = []
    for path in sorted((ROOT / 'chatlocal').glob('*.py')):
        if path.name in allowed:
            continue
        text = path.read_text('utf-8')
        for literal in literals:
            if literal in text:
                offenders.append(f'{path.name}: {literal}')
    assert not offenders, ('插件名不该出现在宿主里（只允许 app_plugins/、功能模块与 mcp_chat 的兼容属性）：\n  '
                           + '\n  '.join(offenders))
    compat = (ROOT / 'chatlocal' / 'mcp_chat.py').read_text('utf-8')
    assert "def reactions(self)" in compat, 'the compatibility property disappeared without notice'
    assert "def learning(self)" in compat, 'the learning compatibility property disappeared without notice'
    print('  ok  宿主无插件名分支：chatlocal/*.py 里没有硬编码工具名或授权位')


def check_removed_module():
    assert not (ROOT / 'chatlocal' / 'mcp_reactions.py').exists(), 'mcp_reactions.py should have moved'
    assert (ROOT / 'chatlocal' / 'app_plugins' / 'reactions.py').exists()
    # language 的功能模块原地不动：七个语言门禁钉死它的路径，插件化的只有适配器。
    assert (ROOT / 'chatlocal' / 'mcp_language_learning.py').exists(), 'the feature module must stay in place'
    assert (ROOT / 'chatlocal' / 'app_plugins' / 'language.py').exists(), 'the adapter must live in app_plugins/'
    print('  ok  归属：适配器在 app_plugins/ 下，功能模块路径未动')


def main():
    print('app plugin protocol (issue #4 PD)')
    check_static_surface()
    check_registry()
    check_scope_and_disable()
    check_host_has_no_name_branches()
    check_removed_module()
    print('PASS: app plugin protocol holds')


if __name__ == '__main__':
    main()
