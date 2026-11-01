"""mention_only 对话窗口跨 wait 与进程重启（issue #217）。

从入口 `main()` 原样搬出的一族（issue #320 §B）：窗口只在 MCPChat 的内存字典里
时，换实例（服务重启 / collector 重连）就全丢，单次 wait 的用例永远看不出这件事。
本族自己开 `111:group:223` 的会话，不读 `main` 的游标、计划队列与 `sid`。

夹具对象由调用点传入：`tools` / `access` 就是 `install_mcp_routes` 产出的那套
（`ChatTurns` 在 `chatlocal/mcp_chat_turns.py:107` 用它推 `self.chat` 与
`self.access`），`events.send` 喂假 WebSocket。换实例时写的是 `tools.chat` 这个
模块级属性，所以本函数改的与 `main` 看到的是同一份状态。

`sid3` 的原地定义（`call('start_chat_session', ...)`）提成 `ensure()` 回调，由调用点
提供——这是本族唯一被改写过的一行，断言与注释一字未改。
"""
from copy import deepcopy
import time

from check_mcp_reactions import ReactionBot, eventually
from chatlocal.mcp_chat import MCPChat


def dialogue_window_survives_restart(call, tools, access, events, ensure):
    """issue #217 现场：落库窗口跨 wait 与实例重建，跳批只推游标、回复重开窗口。

    `ensure()` 返回本族要用的 `111:group:223` 会话 id（已存在就直接复用）；
    `call(name, **args)` 是 `tools.call` 的薄包装；`tools.chat` 可被本函数替换；
    `results.append` 的那行由调用点负责，本函数只做断言。
    """
    # issue #217：mention_only 的对话窗口要落库，跨 wait 调用与
    # 进程重启都存活；窗口关掉后的跳批只推游标，不许把没交付
    # 的行记成 delivered=1。以前窗口只在 MCPChat 的内存字典里，
    # 换一个实例（服务重启 / collector 重连）就全丢——单次 wait
    # 的用例永远看不出这件事。
    sid3=ensure()
    def stored3(mid):
        with access.connect() as db:
            r=db.execute('SELECT id,delivered FROM chat_inbox WHERE session_id=? AND message_id=?',
                         (sid3,str(mid))).fetchone()
        return (r[0],r[1]) if r else None
    def say3(text,mid,at_me=False,self_sent=False):
        uid=111 if self_sent else 444
        segs=([dict(type='at',data={'qq':'111'})] if at_me else [])+[dict(type='text',data={'text':text})]
        event=dict(post_type='message',message_type='group',self_id=111,group_id=223,message_id=mid,
                   message_seq=abs(mid),user_id=uid,sender={'user_id':uid},time=int(time.time()),message=segs)
        ReactionBot.history[str(mid)]=deepcopy(event);events.send(event)
        eventually(lambda:stored3(mid))
        return stored3(mid)[0]
    at_id=say3('有人吗',-901,at_me=True)
    first=call('wait_chat_messages',session_id=sid3,timeout_seconds=1,quiet_seconds=0,mention_only=True)
    assert [m['id'] for m in first['messages']]==[at_id],first
    # 服务重启：换一个 MCPChat 实例，内存态到这里全丢
    old_chat=tools.chat;old_chat.receiver.stop()
    tools.chat=MCPChat(access,tools.actions)
    tools.chat.receiver.start()
    eventually(lambda:tools.chat.receiver.status()['state']=='connected')
    chatter=[say3('哈哈哈哈',-902),say3('确实',-903)]
    second=call('wait_chat_messages',session_id=sid3,timeout_seconds=1,quiet_seconds=0,mention_only=True,
                acknowledge_through_id=first['read_through_id'])
    assert [m['id'] for m in second['messages']]==chatter,(
        'issue #217：服务重启后，回复之后的续接闲聊没有投递',second)
    # 窗口关掉：整批跳过，游标推进但 delivered 保持 0
    with access.connect() as db:
        db.execute('UPDATE chat_sessions SET dialogue_until=0,dialogue_turns=0 WHERE id=?',(sid3,))
    skipped=say3('后来的闲聊',-904)
    idle=call('wait_chat_messages',session_id=sid3,timeout_seconds=1,quiet_seconds=0,mention_only=True,
              acknowledge_through_id=second['read_through_id'])
    assert not idle['messages'],idle
    assert tools.chat.row(sid3)['cursor']>=skipped,'跳批没有推进游标，下一轮会重复看见'
    assert stored3(-904)[1]==0,'issue #217：跳批把没交付的行记成了 delivered=1'
    # 本机发出的回复要重新起算窗口：Agent 想得比 followup_seconds 久
    # 时，回复之后那几句仍然接得上
    with access.connect() as db:
        db.execute('UPDATE chat_sessions SET dialogue_until=0,dialogue_turns=0 WHERE id=?',(sid3,))
    say3('我在',-905,self_sent=True)
    call('wait_chat_messages',session_id=sid3,timeout_seconds=1,quiet_seconds=0,mention_only=True)
    assert tools.chat.row(sid3)['dialogue_until']>time.time(),'issue #217：回复没有重开对话窗口'
