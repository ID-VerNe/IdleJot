"""Language learning as an app plugin (issue #4 PD2b): protocol forwards only.

The feature itself stays at ``chatlocal/mcp_language_learning.py`` (plus its
four leaf modules) -- byte-for-byte behaviour, and the gates pin that module
path. This adapter speaks the app plugin protocol and nothing else: every
method below forwards into ``ChatLearning``, and the wire text the wait loop
and the reply plan used to hardcode in the host lives here as descriptor
data, so the host never learns the feature's names.
"""
from . import spec
from .base import AppPlugin

TOOLS = spec.LANGUAGE['tools']

# 原宿主持有的线报文案，逐字搬进描述子（wait 结果与计划回执的形状不变）。
WAKE_NOTE = ('领取并提交一个学习阶段；附 updated_chat 时沿 next_call 先聊天，'
             '否则回 wait，潜水不结束循环。')
SELECT_NEXT = ('结合候选选择表达并组织文字，直接 send_chat_reply 一起提交 '
               'expression_ids 和 bubbles；[] 明确不用，不必单独调用选择工具。')


class LanguagePlugin(AppPlugin):
    name = spec.LANGUAGE['name']
    tool_names = frozenset(TOOLS)
    scope_flag = spec.LANGUAGE['scope']
    expression = spec.LANGUAGE['expression']
    wake_event = 'learning_ready'

    def __init__(self, chat):
        super().__init__(chat)
        from ..mcp_language_learning import ChatLearning
        self.feature = ChatLearning(chat)

    # -- tools ----------------------------------------------------------
    def schemas(self, tool, scope):
        # 无授权位：chat+send 连接即可见，与插件化前一致。
        # schemas 是功能模块级函数（无 self），不是 ChatLearning 方法。
        from ..mcp_language_learning import schemas
        return schemas(tool)

    def dispatch(self, grant, name, args, cancel):
        return self.feature.call(grant, name, args)

    # -- lifecycle -------------------------------------------------------
    def resume(self):
        self.feature.resume()

    def close(self):
        self.feature.close()

    def suspend(self):
        # 宿主持锁调用；学习态随服务停止整体 drop，与插件化前逐字等价。
        self.feature.invalidate()

    def reset(self):
        self.feature.invalidate()

    def stopped(self, sid):
        self.feature.invalidate(sid)

    def on_stored(self, row, payload):
        self.feature.observe(row, payload)

    # -- prompt and batch data -------------------------------------------
    def batch(self, grant, row):
        data = self.feature.hint(grant, row)
        return dict(language_learning=data) if data else {}

    def decorate(self, packet, data):
        packet['language_learning'] = data['language_learning']

    # -- wait wake -------------------------------------------------------
    def wake_signal(self, grant, row):
        ready = self.feature.wake(grant, row)
        if not ready.get('ready'):
            return None
        return dict(kind=self.wake_event, ready=True, payload=ready,
                    next_call=ready['next_call'], note=WAKE_NOTE)

    # -- plan context and inline selection --------------------------------
    def plan_context(self, p):
        context = self.feature.plan_context(p)
        if not context:
            return None
        patch = dict(field='learned_language', context=context)
        if context.get('selection_required'):
            patch['phase'] = 'SELECTING_EXPRESSION'
            patch['next'] = SELECT_NEXT
            patch['next_call'] = dict(tool='send_chat_reply',
                arguments=dict(session_id=p['session_id'], plan_id=p['id']),
                requires=['expression_ids', 'bubbles'])
        return patch

    def select(self, grant, p, expression_ids):
        choice = self.feature.call(grant, 'select_chat_expressions',
            dict(session_id=p['session_id'], plan_id=p['id'],
                 expression_ids=expression_ids))
        if choice.get('error_code'):
            return choice['error_code'], choice['note']
        return None

    # -- tool-boundary handoff -------------------------------------------
    def handoff(self, grant, data, name, args, cancel):
        if name not in ('claim_chat_learning', 'submit_chat_learning'):
            return None
        return self.feature.handoff(grant, data, args['session_id'], cancel)

    def durable(self, data, name):
        if name != 'submit_chat_learning' or data.get('state') == 'rejected':
            return None
        # 交接失败时随附的聊天调用不成立；receipt 只留可独立成立的字段。
        receipt = {k: v for k, v in data.items()
                   if k not in ('next_call', 'continue_waiting', 'note')}
        return dict(receipt=receipt,
                    cancel_note='学习提交后的交接已取消。',
                    failure_note='学习提交状态已记录；聊天材料未交付。核对会话与授权后接续，不要重做已保存阶段。')

    # -- local UI and gate handles -----------------------------------------
    # ``chat.learning`` is the write-through property on MCPChat; these
    # forwards keep the local HTTP UI and the gates on the same instance.
    @property
    def store(self):
        return self.feature.store

    def tick(self):
        return self.feature.tick()

    def status(self, sid, gid, records=False):
        return self.feature.status(sid, gid, records)

    def binding(self, sid, gid):
        return self.feature.binding(sid, gid)

    def configure(self, sid, body):
        return self.feature.configure(sid, body)

    def manage(self, sid, body):
        return self.feature.manage(sid, body)

    def overview(self):
        return self.feature.overview()

    def library(self, cid, records=False):
        return self.feature.library(cid, records)

    def library_change(self, cid, body):
        return self.feature.library_change(cid, body)

    def invalidate(self, sid=None):
        return self.feature.invalidate(sid)
