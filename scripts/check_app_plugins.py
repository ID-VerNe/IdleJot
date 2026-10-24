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
        self.path = str(path)

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
    assert set(app_plugins.SCOPE_BY_TOOL) == app_plugins.TOOL_NAMES, 'SCOPE_BY_TOOL must cover every tool'
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
    allowed = {'mcp_chat.py'}
    offenders = []
    for path in sorted((ROOT / 'chatlocal').glob('*.py')):
        text = path.read_text('utf-8')
        for literal in ("'chat_reactions'", '"chat_reactions"', "'react_to_chat_message'", '"react_to_chat_message"'):
            if literal in text:
                offenders.append(f'{path.name}: {literal}')
    assert not offenders, ('插件名不该出现在宿主里（只允许 app_plugins/ 与 mcp_chat 的兼容属性）：\n  '
                           + '\n  '.join(offenders))
    compat = (ROOT / 'chatlocal' / 'mcp_chat.py').read_text('utf-8')
    assert "def reactions(self)" in compat, 'the compatibility property disappeared without notice'
    assert allowed == {'mcp_chat.py'}
    print('  ok  宿主无插件名分支：chatlocal/*.py 里没有硬编码工具名或授权位')


def check_removed_module():
    assert not (ROOT / 'chatlocal' / 'mcp_reactions.py').exists(), 'mcp_reactions.py should have moved'
    assert (ROOT / 'chatlocal' / 'app_plugins' / 'reactions.py').exists()
    print('  ok  归属：reactions 在 app_plugins/ 下，旧路径已删除')


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
