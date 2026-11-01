"""停止 / 撤权打断正在睡的发送尾巴（issue #320 §B）。

从入口 `main()` 原样搬出的一族：队列里第二条还在等气泡间隔时 `stop_chat_session`
或 `access.revoke` 必须把它判成 `CANCELLED`，而不是照发。`revoke` 那一轮另开会话
（旧 `sid` 刚被停掉），所以 `start()` 由调用点注入：它新建会话并写回 `main` 的
`sid`，`sid` 也作为参数传进来供 `stop` 那一轮复用。除这两处取名外，断言与注释
一字未改。
"""
from concurrent.futures import ThreadPoolExecutor
import time

from check_mcp_reactions import ReactionBot, eventually


def stop_and_revoke_interrupt_sleeping_tail(call, plan, send, start, access, g, sid):
    """`start()` 新建会话、写回调用方的 `sid` 并返回它的 id。

    参数 `sid` 是调用点当前的会话：`stop` 那一轮用它，`revoke` 那一轮另开新的
    （旧会话刚被停掉），与搬迁前「同一行里重新赋值」等价。
    """
    # Stop/revoke can run while queue sleeps; they cancel remaining work.
    for mode in ('stop','revoke'):
        if mode=='revoke':
            sid=start()
        p=plan();prior=len(ReactionBot.sent)
        with ThreadPoolExecutor() as pool:
            future=pool.submit(send,p,[dict(kind='text',text='已发'),dict(kind='text',text='未发')])
            eventually(lambda:len(ReactionBot.sent)==prior+1)
            if mode=='stop':call('stop_chat_session',session_id=sid)
            else:access.revoke(g['id'])
            out=future.result(15)
        assert len(ReactionBot.sent)==prior+1 and out['bubbles'][1]['state']=='CANCELLED',out
