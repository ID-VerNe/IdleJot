"""Group-member dossier plugin: the L1 cards and the four dossier tools.

Business logic stays in ``mcp_chat_dossier.ChatDossiers`` and in the memory
engine; this module only forwards. The three prompt-side hooks moved here from
the host in issue #4 PD2c:

* ``enrich`` writes ``packet['member_dossiers']`` and the reminder line from a
  message row the host stored earlier.
* ``idle`` runs the digest of unattended messages while a wait call is idle.
* ``on_stored`` feeds the freshly stored message to the memory engine.

The tool schemas moved here too, so the host no longer lists this plugin's
tool names in its own wire contract (``mcp_tools`` still fans them out through
the registry, under the same ``chat``+``send`` gate).

No switch file and no data state: ``enabled()`` is the plugin's own answer.
The feature is always on in this release; the hook exists so the digest below
and the tool surface can be turned off in one place later.
"""
from .base import AppPlugin
from .spec import DOSSIER

_QUERY_DOSSIER = dict(
    identifier=dict(type='string', description='群友 QQ 号、群名片、昵称或外号（如“群友A”、“群友B”）'),
    group_id=dict(type='string', description='当前群号（可选，用于过滤群内专属八卦事件）'),
    session_id=dict(type='string', description='当前持续聊天会话编号（可选，自动推导群号）'))

_UPDATE_DOSSIER = dict(
    identifier=dict(type='string', description='群友 QQ 号、群名片、昵称或既有外号'),
    group_id=dict(type='string', description='当前群号（可选，用于写入群专属记忆；缺省时尝试从 session_id 推导）'),
    session_id=dict(type='string', description='当前持续聊天会话编号（可选，用于推导群号）'),
    key_memories=dict(type='array', items=dict(type='string'), description='需要记录或追加的核心记忆/黑料/事件（如“重度二次元”、“沉迷三国杀”等）'),
    interests=dict(type='array', items=dict(type='string'), description='群友喜好/游戏/XP领域列表'),
    traits=dict(type='object', description='结构化特征槽位，如 {"role": "群友角色", "trope": "反差"}'),
    aliases=dict(type='array', items=dict(type='string'), description='新增外号/别名列表'),
    tone_style=dict(type='string', description='说话口吻与风格偏好描述'),
    role=dict(type='string', description='在群内的角色身份（如“群主”、“管理员”、“活跃群友”）'),
    primary_name=dict(type='string', description='规范化主名称（可选）'),
    overwrite=dict(type='boolean', default=False, description='是否覆盖既有字段，默认 false 为智能追加合并。带 group_id 时只覆盖本群快照，跨群主档记忆不受影响；覆盖导致条目减少时，上一份记忆写入 key_memories_backup'))

_CONSOLIDATE = dict(
    group_id=dict(type='string', description='目标群号（可选，若提供 session_id 可自动推导）'),
    session_id=dict(type='string', description='当前持续聊天会话编号（可选，用于推导群号）'),
    records=dict(type='array', items=dict(type='object'), description='批量群友画像提炼列表，每项包含 identifier, key_memories, interests, traits, tone_style, event(quote, context, tags) 等'),
    events=dict(type='array', items=dict(type='object'), description='独立群事件与八卦流水列表，每项包含 identifier, quote, context, tags 等'),
    summary=dict(type='string', description='本次对话整体话题总结或阶段性沉淀摘要'),
    notes=dict(type='array', items=dict(type='object'), description='群级结构化术语（黑话、店名、场地、群内约定），每项包含 term（术语/简称）, value（指向的含义）, kind（可选，术语类别，默认 glossary）'))

_QUERY_NOTES = dict(
    group_id=dict(type='string', description='目标群号（可选，若提供 session_id 可自动推导）'),
    session_id=dict(type='string', description='当前持续聊天会话编号（可选，用于推导群号）'),
    query=dict(type='string', description='要检索的术语或含义关键词（可选）；不填返回本群最近更新的术语'),
    limit=dict(type='integer', minimum=1, maximum=50, default=20, description='返回条数上限'))

# (name, description, properties, required) -- the wire rows, unchanged from
# the host-side table they came from.
TOOLS = (
    ('query_member_dossier',
     '查询指定群友的小本本记忆与专属档案（包括外号、XP喜好、说话习惯与群内历史八卦）。返回的核心记忆是跨群主档与本群快照的并集；若上一份记忆被覆盖，会一并返回 key_memories_backup。在群友发问“你还记得我吗”、“我喜欢什么”、“你小本本记了啥”或需要精确调侃时调用。',
     _QUERY_DOSSIER, ['identifier']),
    ('update_member_dossier',
     '更新或补充指定群友的小本本记忆与画像档案（包括核心记忆、性格特征、外号、XP喜好、角色身份等）。用于在聊天中捕捉到群友新狠料、明确偏好或被纠正称谓时，原生写入持久化记忆，无需直接操作底层数据库。',
     _UPDATE_DOSSIER, ['identifier']),
    ('consolidate_chat_memory',
     '批量整合与沉淀群聊记忆。将近期对话中提取的多位群友事件、性格变化、最新梗与话题摘要一次性结构化提炼归档至持久化记忆库，免去零散调用与数据库脚本操作。',
     _CONSOLIDATE, []),
    ('query_group_notes',
     '查询本群的结构化群级术语（黑话、店名简称、活动场地、群内约定等）。当群友提到某个黑话/简称、或你记得“记一下”过但想不起指向，用此工具按术语查回。group_id 可用 session_id 自动推导。',
     _QUERY_NOTES, []),
)


def schemas(tool):
    """The four wire rows, built through the host's row factory."""
    return [tool(name, description, dict(properties), required)
            for name, description, properties, required in TOOLS]


class DossierPlugin(AppPlugin):
    name = DOSSIER['name']
    tool_names = frozenset(DOSSIER['tools'])
    scope_flag = DOSSIER['scope']
    expression = DOSSIER['expression']

    def __init__(self, chat):
        super().__init__(chat)
        from ..mcp_chat_dossier import ChatDossiers
        self.feature = ChatDossiers(chat)

    def enabled(self):
        # 没有开关文件也没有数据态：这一版常开，单独的钩子是为了让
        # 「工具面 + 空闲消化」以后能一处关掉。
        return True

    # -- tools ----------------------------------------------------------
    def schemas(self, tool, scope):
        # 无授权位：chat+send 连接即可见，与插件化前一致。
        return schemas(tool)

    def dispatch(self, grant, name, args, cancel):
        return getattr(self.feature, name)(grant, args)

    # -- prompt and batch data ------------------------------------------
    def enrich(self, packet, grant, row, items):
        # 与插件化前逐字等价：任何一个环节出问题都只丢画像，不拖垮唤醒。
        try:
            speaker_ids = [m.get('sender_id') for m in items if not m.get('is_self') and m.get('sender_id')]
            speaker_names = [m.get('sender') for m in items if not m.get('is_self') and m.get('sender')]
            if speaker_ids or speaker_names:
                from water_group.memory_engine import get_memory_engine
                engine = get_memory_engine()
                gid = row['conversation_id'].split(':')[-1]
                l1_cards = engine.build_l1_context(gid, speaker_ids, speaker_names)
                if l1_cards:
                    packet['member_dossiers'] = l1_cards
                    if 'reminders' in packet:
                        packet['reminders'].append(_reminder())
        except Exception:
            pass

    def idle(self, after):
        if not self.enabled():
            return 0
        gid = after['conversation_id'].split(':')[-1]
        try:
            return _digest(gid, _recent_payloads(self.chat, after))
        except Exception:
            # 与插件化前一致：空闲消化出问题不拖垮这次 wait。
            return 0

    # -- lifecycle and stored messages ----------------------------------
    def on_stored(self, row, payload):
        try:
            from water_group.memory_engine import get_memory_engine
            get_memory_engine().observe_message(row['conversation_id'].split(':')[-1], payload)
        except Exception:
            pass


def _reminder():
    from ..mcp_chat_hints import DOSSIER_REMINDER
    return DOSSIER_REMINDER


def _recent_payloads(chat, row):
    from ..mcp_chat_wait import recent_payloads
    return recent_payloads(chat, row)


def _digest(gid, payloads):
    from water_group.memory_engine import get_memory_engine
    return get_memory_engine().digest_idle_batch(gid, payloads)
