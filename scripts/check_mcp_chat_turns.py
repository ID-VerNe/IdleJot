"""Planner/Replyer public MCP integration. Isolated fixtures, no real QQ/LLM.

Unlike adapter regression suites this exercises the mandatory plan gate, native
adapters, durable queue, stop/revoke and service restart without a protocol seam.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parent))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_fixture_support import isolated_env, network_guard, temp_dir
from fixture_server import fixture_server
from check_mcp_reactions import ReactionBot, Events, eventually, ROOT
from chatlocal.store import Store
from chatlocal.mcp_chat import MCPChat
from chatlocal.mcp_routes import install_mcp_routes
from chatlocal.mcp_chat_turns import ChatTurns
import chatlocal.mcp_chat_turns as turns
from chatlocal.onebot import invalidate_availability
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import httpx


# The receiver answers source_unavailable while it re-establishes its WebSocket
# (90 s packet watchdog, or the 15 s identity re-check). That transient is
# orthogonal to the ACK/deadline behaviour asserted here, so idle samples retry
# that one event for a bounded window instead of sampling a single instant.
# Any other answer still fails immediately, and the failure keeps the whole
# result plus the receiver state so the next red run can be attributed.
IDLE_RETRY_SECONDS=6


def idle_call(call,status,name,**args):
    deadline=time.monotonic()+IDLE_RETRY_SECONDS
    while True:
        result=call(name,**args)
        if result.get('event')!='source_unavailable' or time.monotonic()>=deadline:break
        time.sleep(.05)
    assert result.get('event')=='idle',(result,status())
    return result


async def idle_follow(follow,status,value,**extra):
    deadline=time.monotonic()+IDLE_RETRY_SECONDS
    while True:
        result=await follow(value,**extra)
        if result.get('event')!='source_unavailable' or time.monotonic()>=deadline:break
        await asyncio.sleep(.05)
    assert result.get('event')=='idle',(result,status())
    return result


async def wire(service,token,sid):
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},trust_env=False,timeout=20) as http:
        async with streamable_http_client(service.status()['url'],http_client=http) as (r,w,_):
            async with ClientSession(r,w) as client:
                await client.initialize()
                names={t.name:t for t in (await client.list_tools()).tools}
                assert names['send_chat_reply'].annotations.openWorldHint
                async def call(name,**args):
                    return (await client.call_tool(name,args)).structuredContent
                read=await call('get_chat_session',session_id=sid)
                plan=await call('plan_chat_reply',session_id=sid,batch_id=read['input_batch']['id'],
                    idempotency_key='wire-plan-001',action='reply',expressions=['text'],intent='short fixture reply')
                assert plan['state']=='READY',plan
                long_text='完整解释可以保持一个气泡，不按句号机械切分。'*35
                sent=await call('send_chat_reply',session_id=sid,plan_id=plan['plan_id'],
                    idempotency_key='wire-reply-001',bubbles=[dict(kind='text',text=long_text)])
                assert sent['state']=='SUCCEEDED',sent
                assert len(sent['bubbles'])==1 and ReactionBot.sent[-1]['message']==[dict(type='text',data={'text':long_text})]
                return dict(discovered=True,state=sent['state'],bubble_count=len(sent['bubbles']))


async def wire_waiting(service,ui,events):
    """Follow actual server call templates, including a stale client's ACK."""
    access=service.access;chat=service.tools.chat
    def receiver_status():return chat.receiver.status()
    def beat():
        # This phase injects messages straight into chat.receive(), so the fake
        # WebSocket would otherwise go silent and trip the receiver's 90 s
        # packet watchdog mid-assertion. A meta heartbeat is ignored by
        # chat.receive() and keeps the socket looking alive, like NapCat's.
        events.send(dict(post_type='meta_event',meta_event_type='heartbeat',self_id=111,
            status=dict(online=True,good=True)))
    cfg=dict(name='deferred fixture',platforms=['qq'],conversations=[['qq','111:group:223']],send=True,chat=True)
    g=access.create(cfg);serial=0
    beat()
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+g['token']},trust_env=False,timeout=20) as http:
        async with streamable_http_client(service.status()['url'],http_client=http) as (r,w,_):
            async with ClientSession(r,w) as client:
                await client.initialize()
                async def call(name,**args):
                    result=await client.call_tool(name,args)
                    assert not result.isError,result
                    return result.structuredContent
                async def follow(value,**extra):
                    return await call(value['tool'],**dict(value['arguments'],**extra))
                async def start(participation='natural'):
                    nonlocal serial
                    serial+=1
                    sid=(await call('start_chat_session',conversation_id='111:group:223',persona='fixture',
                        participation=participation,idempotency_key=f'wait-start-{serial}'))['session']['id']
                    # This suite checks the paused-learning chat mode, like the
                    # main session above. With learning on, a wait that should be
                    # idle can legitimately answer learning_ready instead; that
                    # wire contract has its own suite.
                    paused=ui.put(f'/api/mcp/chats/{sid}/learning',json=dict(enabled=False),headers={'X-ChatWeave-UI':'1'})
                    assert paused.status_code==200 and not paused.json()['enabled']
                    return sid
                sid=await start()
                def add(text='deferred fixture'):
                    nonlocal serial
                    serial+=1;mid=880000+serial
                    beat()
                    chat.receive(dict(post_type='message',message_type='group',self_id=111,group_id=223,message_id=mid,
                        user_id=444,time=time.time(),message=[dict(type='text',data=dict(text=text))]))
                    with access.connect() as db:return db.execute('SELECT id FROM chat_inbox WHERE session_id=? AND message_id=?',(sid,str(mid))).fetchone()[0],mid
                async def defer(seconds=20):
                    packet=await call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0)
                    assert packet['next_call']==packet['input_batch']['plan_call'] and 'action' in packet['next_call']['requires']
                    assert 'acknowledge_through_id' not in packet['next_call']['arguments']
                    p=await follow(packet['next_call'],action='wait',intent='暂缓决定',wait_seconds=seconds)
                    assert p['state']=='WAITING' and not p['turn_complete']
                    assert 'acknowledge_through_id' not in p['next_call']['arguments']
                    return packet,p
                def due(p,expire=False):
                    with access.connect() as db:
                        data=json.loads(db.execute('SELECT payload FROM chat_turn_plans WHERE id=?',(p['plan_id'],)).fetchone()[0])
                        data['not_before']=time.time()-1
                        db.execute('UPDATE chat_turn_plans SET payload=?,created=created-? WHERE id=?',(json.dumps(data),1000 if expire else 0,p['plan_id']))
                        if expire:db.execute('UPDATE chat_input_batches SET created=created-1000 WHERE id=?',(p['decision']['batch_id'],))
                eid,mid=add();packet,p=await defer();cursor=chat.row(sid)['cursor']
                began=time.monotonic()
                early=await idle_follow(follow,receiver_status,p['next_call'],timeout_seconds=1,acknowledge_through_id=packet['read_through_id'])
                assert time.monotonic()-began>=.9 and not early['decision_required'] and not early['messages'],early
                assert chat.row(sid)['cursor']==cursor and early['deferred_until']>time.time()
                due(p,expire=True)
                resumed=await follow(early['next_call'],timeout_seconds=1)
                assert resumed['decision_required'] and [m['id'] for m in resumed['messages']]==[eid]
                assert resumed['input_batch']['id']!=packet['input_batch']['id']
                assert resumed['next_call']==resumed['input_batch']['plan_call']
                ready=await follow(resumed['next_call'],action='reply',intent='到期回复',target_event_id=eid,expressions=['text'])
                assert ready['state']=='READY',ready
                sent=await follow(ready['next_call'],expression_ids=[],bubbles=[dict(kind='text',text='deferred fixture reply')])
                assert sent['state']=='SUCCEEDED',sent
                idle=await follow(sent['next_call'],timeout_seconds=1)
                assert not idle['decision_required'] and chat.row(sid)['cursor']>=eid
                eid,_=add();_,p=await defer(seconds=1)
                until=chat.turns.waiting(sid)['not_before']
                resumed=await follow(p['next_call'],timeout_seconds=2)
                assert time.time()>=until and resumed['decision_required'] and resumed['messages'][0]['id']==eid
                silent=await follow(resumed['input_batch']['plan_call'],action='silence',intent='到期后不参与')
                await idle_follow(follow,receiver_status,silent['next_call'],timeout_seconds=1)
                # A fresh input can interrupt the deliberate wait without
                # losing the original input; silence then ACK finishes both.
                old,_=add();packet,p=await defer();new,_=add()
                resumed=await follow(p['next_call'],timeout_seconds=1)
                assert {old,new}<={m['id'] for m in resumed['messages']} and resumed['decision_required']
                silent=await follow(resumed['input_batch']['plan_call'],action='silence',intent='本轮不参与')
                idle=await idle_follow(follow,receiver_status,silent['next_call'],timeout_seconds=1)
                assert not chat.turns.waiting(sid),idle
                # A new-input page supersedes the timer; its continuation must
                # not remain stuck behind the original deferred ACK guard.
                old,_=add();_,p=await defer();new=[add()[0] for _ in range(33)]
                resumed=await follow(p['next_call'],timeout_seconds=1)
                assert resumed['has_more'] and not chat.turns.waiting(sid)
                tail=await follow(resumed['next_call'],timeout_seconds=1)
                assert set([old]+new)<={m['id'] for m in resumed['messages']+tail['messages']} and not tail['has_more']
                silent=await follow(tail['input_batch']['plan_call'],action='silence',intent='分页处理完毕')
                await idle_follow(follow,receiver_status,silent['next_call'],timeout_seconds=1)
                # Without new input, expiry must also release the page ACK:
                # the original batch can be larger than the next wait's page.
                original=[add()[0] for _ in range(35)]
                packet=await call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0,limit=50)
                assert [m['id'] for m in packet['messages']]==original and not packet['has_more']
                p=await follow(packet['input_batch']['plan_call'],action='wait',intent='分页前暂缓',wait_seconds=1)
                cursor=chat.row(sid)['cursor'];until=chat.turns.waiting(sid)['not_before']
                resumed=await follow(p['next_call'],timeout_seconds=2,quiet_seconds=0)
                assert time.time()>=until and [m['id'] for m in resumed['messages']]==original[:30]
                assert resumed['has_more'] and not chat.turns.waiting(sid)
                assert chat.row(sid)['cursor']==cursor,'expiry acknowledged input before page delivery'
                assert resumed['next_call']['arguments']['acknowledge_through_id']==original[29]
                tail=await follow(resumed['next_call'],timeout_seconds=1,quiet_seconds=0)
                assert [m['id'] for m in tail['messages']]==original[30:] and not tail['has_more']
                assert chat.row(sid)['cursor']==original[29]
                ready=await follow(tail['input_batch']['plan_call'],action='reply',intent='分页后回复',
                    target_event_id=original[-1],expressions=['text'])
                assert ready['state']=='READY',ready
                sent=await follow(ready['next_call'],expression_ids=[],bubbles=[dict(kind='text',text='paged deferred reply')])
                assert sent['state']=='SUCCEEDED',sent
                idle=await follow(sent['next_call'],timeout_seconds=1)
                assert not idle['decision_required'] and not idle['messages'] and chat.row(sid)['cursor']>=original[-1],idle
                # Recall removes the deferred decision, not a new live event.
                eid,mid=add();_,p=await defer()
                chat.recall(dict(self_id=111,group_id=223,message_id=mid));due(p)
                recalled=await follow(p['next_call'],timeout_seconds=1)
                assert not recalled['decision_required'] and not chat.turns.waiting(sid)
                await idle_follow(follow,receiver_status,recalled['next_call'],timeout_seconds=1)
                # Cancellation cannot return a late decision or dispatch; a
                # later explicit read may still recover unprocessed input.
                add();_,p=await defer();cancel=threading.Event()
                task=asyncio.create_task(asyncio.to_thread(service.tools.call,g['token'],'wait_chat_messages',dict(p['next_call']['arguments'],timeout_seconds=10),cancel))
                while sid not in chat.waiters:await asyncio.sleep(.01)
                before=len(ReactionBot.sent);cancel.set()
                assert (await task).structuredContent['event']=='cancelled' and len(ReactionBot.sent)==before
                await call('stop_chat_session',session_id=sid)
                assert (await follow(p['next_call'],timeout_seconds=1))['event']=='stopped'
                assert not chat.turns.waiting(sid)
                # `ask` (issue #223): the planner needs a way to say "I just
                # asked something and am waiting for the answer". It shares the
                # WAITING state with `wait`, so the read/wait/recovery flow is
                # unchanged, but it must refill the mention_only dialogue
                # window — otherwise the answer, which rarely carries another
                # @, stays invisible and the turn reads as nothing happened.
                def add_to(target,text,mention):
                    # A real @ 111 segment is what the mention_only path keys on
                    # (mcp_chat_wait.py:132), not the text. Each scenario gets a
                    # fresh session: a `wait` plan keeps collecting for its full
                    # wait_seconds, so reusing one session would feed the next
                    # scenario's lines into the previous batch.
                    nonlocal serial
                    serial+=1;mid=880000+serial
                    beat()
                    seg=[dict(type='at',data=dict(qq='111'))] if mention else []
                    seg.append(dict(type='text',data=dict(text=text)))
                    chat.receive(dict(post_type='message',message_type='group',self_id=111,group_id=223,message_id=mid,
                        user_id=444,time=time.time(),message=seg))
                    with access.connect() as db:return db.execute('SELECT id FROM chat_inbox WHERE session_id=? AND message_id=?',(target,str(mid))).fetchone()[0]
                async def decision_window(action):
                    # A mention_only session: an @ opens the dialogue window, and
                    # the ask action decides what a later bare answer does. Each
                    # scenario starts its own session (`ask` and the `wait`
                    # control must not share a dialogue window) and each read is
                    # a plain idle one after the plan — no wait plan is left open
                    # to defer anything, so only the action can hide the answer.
                    #
                    # The answer is held until the plan lands: a bare answer that
                    # arrives while the @ is still unplanned is older input, and
                    # the `@` is still at `id > cursor` because only `wait` keeps
                    # a plan open. Spacing them out is what real use looks like
                    # — the agent plans, then the peer replies.
                    target=await start(participation='mention_only')
                    asked=add_to(target,'你刚才说的那个是什么',True)
                    packet=await call('wait_chat_messages',session_id=target,timeout_seconds=1,quiet_seconds=0)
                    assert [m['id'] for m in packet['messages']]==[asked],packet
                    planned=await follow(packet['input_batch']['plan_call'],action=action,intent='fixture intent')
                    answer_id=add_to(target,'就是那个每日任务',False)
                    seen=await call('wait_chat_messages',session_id=target,timeout_seconds=1,quiet_seconds=0)
                    return target,asked,planned,answer_id,seen
                # `ask` (issue #223): "I just asked something and am waiting for
                # the answer". It shares WAITING with `wait`, so read/wait/
                # recovery are unchanged, but it refills the dialogue window —
                # otherwise the answer, which rarely carries another @, stays
                # invisible and the turn reads as nothing happened. The marker is
                # what tells the reader which of the two it is.
                target,asked,queued,answer,seen=await decision_window('ask')
                assert queued['state']=='WAITING' and not queued['turn_complete'],queued
                assert queued['decision']['awaiting'] is True and queued['decision']['action']=='ask'
                assert 'acknowledge_through_id' not in queued['next_call']['arguments']
                # The answer carries no @ and comes second in the batch (the @ is
                # still owed — only `wait` keeps a plan open), yet the `ask`
                # plan's window makes the batch a decision of its own instead of
                # letting the answer be swallowed behind the mention.
                assert [m['id'] for m in seen['messages']]==[asked,answer],seen
                assert seen['messages'][-1]['content']=='就是那个每日任务',seen
                assert seen['decision_required'],seen
                # `silence` still closes the window for real: the bare message
                # after it stays invisible even though it arrived right after.
                stopped=await follow(seen['input_batch']['plan_call'],action='silence',intent='本轮收场')
                assert stopped['state']=='SILENT' and not chat.turns.waiting(target),stopped
                add_to(target,'那算了',False)
                silent_tail=await idle_follow(follow,receiver_status,stopped['next_call'],timeout_seconds=1)
                assert not silent_tail['decision_required'] and not silent_tail['messages'],silent_tail
                # Control: `wait` on the same wire carries no `awaiting` marker
                # and, unlike `ask`, never opens the window — the bare answer
                # stays behind the held @ and is not what the round turns on.
                target,asked,held,answer,seen=await decision_window('wait')
                assert held['state']=='WAITING' and not held['decision'].get('awaiting'),held
                assert seen['decision_required'] and [m['id'] for m in seen['messages']]==[asked,answer],seen
                assert not seen.get('decision',{}).get('awaiting'),seen
                # `silence` on top of the held `wait` is still what ends the round.
                await follow(seen['input_batch']['plan_call'],action='silence',intent='本轮收场')
                assert not chat.turns.waiting(target)
                await call('stop_chat_session',session_id=target)
                sid=target
                sid=await start();add();_,p=await defer()
                assert ui.post('/api/mcp/connections/'+g['id']+'/revoke',json={},headers={'Origin':'http://testserver','X-ChatWeave-UI':'1'}).status_code==200
                denied=await http.post(service.status()['url'],json=dict(jsonrpc='2.0',id=999,method='tools/call',
                    params=dict(name='wait_chat_messages',arguments=dict(p['next_call']['arguments'],timeout_seconds=1))))
                assert denied.status_code==401 and not chat.row(sid)['active'] and not chat.turns.waiting(sid)
    return 'real MCP deferred ACK, deadline, expired batch/plan, new input, silence/success, recall/cancel/stop/revoke'


def main():
    assert Path(__import__('chatlocal.mcp_chat_turns',fromlist=['ChatTurns']).__file__).resolve()==ROOT/'chatlocal/mcp_chat_turns.py'
    whale=ROOT/'water-group/personas/little_whale.md';before=hashlib.sha256(whale.read_bytes()).hexdigest()
    events=Events();ReactionBot.sent=[];ReactionBot.writes=[];ReactionBot.history={};ReactionBot.intercept=None
    results=[];counter=0
    allowed_ports={events.port}
    with fixture_server(ReactionBot, ports=allowed_ports) as upstream:
        restrict,network=network_guard(allowed_ports)
        try:
            with temp_dir(prefix='tulpa-turns-') as root,patch.dict('os.environ',isolated_env(root),clear=True),patch('socket.create_connection',restrict):
                store=Store(root/'chats.sqlite3')
                (root/'.env').write_text(f'REPLY_ONEBOT_URL=http://127.0.0.1:{upstream.server_port}\nREPLY_ONEBOT_WS_URL=ws://127.0.0.1:{events.port}\nREPLY_ONEBOT_WS_TOKEN=event-fixture\n','utf-8')
                with patch('chatlocal.onebot.ROOT',root),patch('chatlocal.message_sender.ROOT',root):
                    app=FastAPI();service=install_mcp_routes(app,store);access=service.access;tools=service.tools
                    with TestClient(app) as ui:
                        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                        allowed_ports.add(port);access.configure(True,port);service.start();assert service.status()['running']
                        cfg=dict(name='turn-fixture',platforms=['qq'],conversations=[['qq','111:group:222'],['qq','111:group:223']],
                            send=True,chat=True,chat_reactions=True,chat_images=True,chat_sticker_send=True)
                        g=access.create(cfg);grant=access.authorize(g['token'],source=False)
                        policy=access.path.parent/'mcp-chat-policy.json'
                        policy.write_text(json.dumps(dict(quiet_seconds=.15,reply_cooldown_seconds=0,bubble_min_seconds=.25,bubble_max_seconds=.35)),'utf-8')
                        tools.chat.reactions.policy_path.write_text('{"cooldown_seconds":0}','utf-8')
                        def call(name,**args):return tools.call(g['token'],name,args,threading.Event()).structuredContent
                        def receiver_status():return tools.chat.receiver.status()
                        def key():
                            nonlocal counter
                            counter+=1;return f'fixture-key-{counter:04}'
                        sid=call('start_chat_session',conversation_id='111:group:222',persona_preset='little_whale_v2',idempotency_key=key())['session']['id']
                        assert next(s for s in call('list_chat_sessions')['sessions'] if s['id']==sid)['persona_preset']=='little_whale_v2'
                        assert call('start_chat_session',conversation_id='111:group:222',persona_preset='little_whale_v2',idempotency_key=key())['session']['id']==sid
                        assert call('start_chat_session',conversation_id='111:group:222',persona_preset='little_whale',idempotency_key=key())['error_code']=='chat_rejected'
                        # This suite checks the supported paused-learning chat mode.
                        # Automatic learning/idle wakes have a dedicated wire suite.
                        paused=ui.put(f'/api/mcp/chats/{sid}/learning',json=dict(enabled=False),headers={'X-ChatWeave-UI':'1'})
                        assert paused.status_code==200 and not paused.json()['enabled']
                        def stored(mid):
                            with access.connect() as db:
                                r=db.execute('SELECT id FROM chat_inbox WHERE session_id=? AND message_id=?',(sid,str(mid))).fetchone()
                                return r[0] if r else None
                        def add(text='fixture',uid=333,segments=None,group=222,**extra):
                            mid=-int(key().split('-')[-1])-80000
                            event=dict(post_type='message',message_type='group',self_id=111,group_id=group,message_id=mid,message_seq=abs(mid),
                                user_id=uid,sender={'user_id':uid},time=int(time.time()),message=segments or [dict(type='text',data={'text':text})])|extra
                            ReactionBot.history[str(mid)]=deepcopy(event);events.send(event)
                            if group==222:eventually(lambda:stored(mid));return stored(mid)
                        def read():return call('get_chat_session',session_id=sid)
                        def plan(target=None,expression=None,action='reply',batch=None,**extra):
                            args=dict(session_id=sid,batch_id=(batch or read())['input_batch']['id'],action=action,intent='fixture communication',
                                expressions=expression or (['text'] if action=='reply' else []),idempotency_key=key(),**extra)
                            if target:args['target_event_id']=target
                            return call('plan_chat_reply',**args)
                        def send(p,bubbles=None,**extra):
                            return call('send_chat_reply',session_id=sid,plan_id=p['plan_id'],idempotency_key=key(),
                                bubbles=bubbles or [dict(kind='text',text='fixture reply')],**extra)
                        def clear_budget():
                            with access.connect() as db:db.execute('DELETE FROM calls')
                        def expire(p):
                            """End a deliberate wait without sleeping out its window."""
                            with access.connect() as db:
                                data=json.loads(db.execute('SELECT payload FROM chat_turn_plans WHERE id=?',(p['plan_id'],)).fetchone()[0])
                                data['not_before']=time.time()-1
                                db.execute('UPDATE chat_turn_plans SET payload=? WHERE id=?',(json.dumps(data),p['plan_id']))

                        bad=call('wait_chat_messages',session_id=1234)
                        assert bad['error_code']=='invalid_arguments' and bad['argument_errors'][0]==dict(path=['session_id'],rule='type',expected='string'),bad
                        bad=call('plan_chat_reply',session_id=sid,batch_id='a'*32,action='private-sentinel',intent='private-sentinel')
                        assert 'private-sentinel' not in json.dumps(bad)
                        assert bad['recovery']['executed'] is False and not ReactionBot.sent
                        from chatlocal.mcp_tools import argument_problems,argument_validator
                        import jsonschema
                        schema=next(s['parameters'] for s in tools.schemas(grant) if s['name']=='wait_chat_messages')
                        assert schema['properties']['timeout_seconds']['default']==150
                        assert tools.chat.continuation(sid)['arguments']['timeout_seconds']==150
                        params=dict(session_id=sid,timeout_seconds=1)
                        argument_problems(params,schema);cache_before=argument_validator.cache_info()
                        began=time.perf_counter()
                        for _ in range(100):assert not argument_problems(params,schema)
                        cached_ms=(time.perf_counter()-began)*1000
                        assert argument_validator.cache_info().hits-cache_before.hits==100
                        began=time.perf_counter()
                        for _ in range(100):jsonschema.validate(params,schema)
                        uncached_ms=(time.perf_counter()-began)*1000
                        validation_cost=dict(sample_calls=100,cached_ms=round(cached_ms,2),previous_ms=round(uncached_ms,2))
                        results.append('field-specific argument errors, no private value echo, schema cache retains validation')

                        # Correct ordinary Planner arguments within the delivered
                        # batch, without a forced context/material re-read.
                        eid=add('契约测试');time.sleep(.18)
                        known=read();bid=known['input_batch']['id']
                        with patch.object(tools.chat,'read_context',side_effect=AssertionError('argument correction reread context')):
                            wrong=call('plan_chat_reply',session_id=sid,batch_id=bid,action='reply',expressions=['text'],
                                topic_event_ids=[999999],intent='synthetic correction')
                            assert wrong['error_code']=='target_not_in_batch' and wrong['recovery']['action']=='correct_request'
                            assert eid in wrong['valid_event_ids'] and wrong['recovery']['next_call']['arguments']['batch_id']==bid
                            for action in ('wait','silence','reply'):
                                bad=call('plan_chat_reply',session_id=sid,batch_id=bid,action=action,intent='synthetic',
                                    expressions=[] if action=='reply' else ['text'])
                                assert bad['error_code']=='expression_mismatch' and bad['recovery']['action']=='correct_request'
                        latest=add('纠错期间新补充');time.sleep(.18)
                        correction=wrong['recovery']['next_call']
                        blocked=call(correction['tool'],**correction['arguments'],action='reply',intent='corrected',expressions=['text'],target_event_id=eid)
                        assert blocked['error_code']=='stale' and any(m['id']==latest for m in blocked['updated_chat']['context']['messages'])
                        fix=blocked['recovery']['next_call']
                        resolved=call(fix['tool'],**fix['arguments'],action='silence',intent='本批已判断')
                        assert resolved['state']=='SILENT'
                        with access.connect() as db:db.execute('DELETE FROM calls')

                        # MCP ordinary get/legacy sends are not a quiet-window bypass.
                        fast_policy=policy.read_text('utf-8');policy.write_text('{"quiet_seconds":2,"reply_cooldown_seconds":0}','utf-8')
                        eid=add('第一段')
                        denied=call('send_chat_message',session_id=sid,text='too early',idempotency_key=key())
                        assert denied['error_code']=='plan_required' and not ReactionBot.sent,denied
                        early=plan(eid);assert early.get('error_code')=='needs_wait',early
                        policy.write_text(fast_policy,'utf-8')
                        drained=call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0)
                        # Real Windows SQLite commits + synthetic WS delivery can
                        # exceed 150 ms under CI load; use an explicit bounded
                        # collection window rather than race machine throughput.
                        with ThreadPoolExecutor() as pool:
                            waiting=pool.submit(call,'wait_chat_messages',session_id=sid,timeout_seconds=3,quiet_seconds=.6,acknowledge_through_id=drained['read_through_id'])
                            time.sleep(.04);add('第一段')
                            time.sleep(.06);add('第二段');time.sleep(.06);last=add('第三段')
                            batch=waiting.result(5)
                        assert [m['content'] for m in batch['messages']]==['第一段','第二段','第三段']
                        assert batch['input_batch']['collection']['reason']=='quiet'
                        assert len(batch['input_batch']['bursts'][0]['event_ids'])==3
                        p=plan(last,batch=batch);assert p['state']=='READY',p
                        # New same-speaker correction while model was composing.
                        correction=add('更正，改成明天')
                        original_context=tools.chat.context
                        def unlocked_context(*args,**kwargs):
                            assert not tools.chat.turns.lock(sid).locked() and not tools.chat.lock._is_owned()
                            return original_context(*args,**kwargs)
                        with patch.object(tools.chat,'context',side_effect=unlocked_context),patch.object(tools.chat,'get',side_effect=AssertionError('recursive recovery get')):out=send(p)
                        assert out['error_code']=='stale' and not ReactionBot.sent,out
                        recovery=out['recovery']['next_call']
                        updated=out['updated_chat'];assert any(m['id']==correction for m in updated['context']['messages'])
                        assert recovery==updated['input_batch']['plan_call'] and recovery['arguments']['session_id']==sid
                        assert out['recovery']['automatic_write_retry'] is False
                        # Another correction still rejects the newly delivered batch, without another explicit get.
                        latest=add('再补充，明天下午')
                        again=plan(correction,batch=updated)
                        assert again['error_code']=='stale' and any(m['id']==latest for m in again['updated_chat']['context']['messages']) and not ReactionBot.sent,again
                        time.sleep(.18)
                        assert plan(latest,batch=again['updated_chat'])['state']=='READY'
                        assert send(p)['error_code']=='plan_not_ready'
                        results.append('three-part collection; unlocked stale result delivers fresh usable batch; another correction still blocks old answer')

                        # Unrelated busy traffic does not reset selected topic.
                        time.sleep(.18);p=plan(last);assert p['state']=='READY',p
                        halt=threading.Event()
                        def hot():
                            while not halt.is_set():add('无关游戏话题',uid=444);halt.wait(.04)
                        worker=threading.Thread(target=hot);worker.start()
                        try:
                            started=time.monotonic();out=send(p);elapsed=time.monotonic()-started
                            assert out['state']=='SUCCEEDED' and elapsed<3,out
                        finally:halt.set();worker.join()
                        results.append('unrelated traffic does not starve a selected response')

                        # A third-party @-ing the bot during composition does not invalidate existing target's reply
                        clear_budget()
                        first_target=add('UserA的问题',uid=101)
                        time.sleep(.18);p=plan(first_target);assert p['state']=='READY',p
                        add(uid=202,segments=[dict(type='at',data={'qq':'111'}),dict(type='text',data={'text':'UserB问另一个问题'})])
                        before_send=len(ReactionBot.sent);out=send(p)
                        assert out['state']=='SUCCEEDED' and len(ReactionBot.sent)==before_send+1,out
                        results.append('third-party bot mention does not starve or invalidate existing target reply')

                        # A selected speaker can move to another conversation while
                        # the Agent composes. Native addressing, not speaker identity,
                        # distinguishes that conversation and its unmarked followups.
                        clear_budget()
                        other=add('另一段对话',uid=444)
                        with access.connect() as db:
                            other_mid=db.execute('SELECT message_id FROM chat_inbox WHERE id=?',(other,)).fetchone()[0]
                        last=add('请回答这条问题');time.sleep(.18);batch=read()
                        add(uid=333,segments=[dict(type='at',data={'qq':'444'}),dict(type='text',data={'text':'先聊你那件事'})])
                        add('接着补充旁边的话题')
                        p=plan(last,batch=batch);assert p.get('state')=='READY',p
                        add(uid=333,segments=[dict(type='reply',data={'id':other_mid}),dict(type='text',data={'text':'这条也在说另一件事'})])
                        add('还是刚才那件旁边的事')
                        before_send=len(ReactionBot.sent);out=send(p)
                        assert out['state']=='SUCCEEDED' and len(ReactionBot.sent)==before_send+1,out
                        results.append('same-speaker side conversations and their followups do not invalidate plan or send')

                        # The collection gate must use the same routing as dispatch.
                        # Copy only fixture times: an old question is quiet while a
                        # side conversation is arbitrarily recent, without a race.
                        with access.connect() as db:
                            pending=[dict(r) for r in db.execute('SELECT * FROM chat_inbox WHERE session_id=? AND id>=? ORDER BY id',(sid,last))]
                        for item in pending:
                            payload=json.loads(item['payload'])
                            payload['received_at']=0 if item['id']==last else time.time()+60
                            item['payload']=json.dumps(payload)
                        collected=tools.chat.turns.collection(tools.chat.row(sid),pending,2,time.monotonic())
                        assert collected['reason']=='quiet' and collected['ready_to_send'],collected

                        # Returning to the selected topic still invalidates the
                        # answer, including native mixed addressing and late replies.
                        for mode in ('quote','mention','term','reply_chain','continuation'):
                            last=add('请回答原问题');time.sleep(.18)
                            read()
                            target_mid=tools.chat.response_target(tools.chat.row(sid),last)['onebot_message_id']
                            if mode in ('reply_chain','continuation'):
                                supplement=add(uid=555,segments=[dict(type='reply',data={'id':target_mid}),dict(type='text',data={'text':'补充条件'})])
                                read()
                                supplement_mid=tools.chat.response_target(tools.chat.row(sid),supplement)['onebot_message_id']
                            else:
                                add(uid=333,segments=[dict(type='at',data={'qq':'444'}),dict(type='text',data={'text':'旁边的话题'})])
                            time.sleep(.18)
                            p=plan(last,**({'topic_terms':['原题条件']} if mode=='term' else {}));assert p.get('state')=='READY',p
                            if mode=='quote':
                                segments=[dict(type='reply',data={'id':target_mid}),dict(type='at',data={'qq':'444'}),dict(type='text',data={'text':'原题更正'})]
                            elif mode=='mention':
                                segments=[dict(type='at',data={'qq':'444'}),dict(type='at',data={'qq':'111'}),dict(type='text',data={'text':'更正一下'})]
                            elif mode=='term':
                                segments=[dict(type='at',data={'qq':'444'}),dict(type='text',data={'text':'原题条件有变化'})]
                            elif mode=='reply_chain':
                                segments=[dict(type='reply',data={'id':supplement_mid}),dict(type='text',data={'text':'再改一个条件'})]
                            else:segments=[dict(type='text',data={'text':'还要补充一个条件'})]
                            correction=add(uid=555 if mode=='continuation' else 666 if mode=='reply_chain' else 333,segments=segments)
                            prior=len(ReactionBot.sent);out=send(p)
                            assert out.get('error_code')=='stale' and correction in out['refresh_event_ids'] and len(ReactionBot.sent)==prior,(mode,out)
                        results.append('quiet collection ignores side traffic; quotes, mentions, topic terms, reply chains and corrections still block stale sends')

                        # Refresh delivery can be acknowledged without fetching it
                        # again, and recovery must start at the oldest unread input.
                        clear_budget()
                        page=call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0,limit=50)
                        while page['has_more']:
                            page=call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0,limit=50,
                                      acknowledge_through_id=page['read_through_id'])
                        call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0,acknowledge_through_id=page['read_through_id'])
                        cursor=tools.chat.row(sid)['cursor'];fresh=add('补读后不应再次交付')
                        refreshed=read()
                        assert refreshed['session']['cursor']==cursor and refreshed['session']['offered']==fresh
                        next_call=refreshed['chat_prompt']['continue_with']
                        assert next_call['arguments']['acknowledge_through_id']==fresh
                        idle=idle_call(call,receiver_status,next_call['tool'],**dict(next_call['arguments'],timeout_seconds=1,quiet_seconds=0))
                        assert not idle['messages'],idle
                        unread=[add('未读分页 '+str(i),uid=444) for i in range(55)]
                        refreshed=read()
                        assert [m['id'] for m in refreshed['context']['messages'] if m['id']>fresh]==unread[:50]
                        assert refreshed['context']['has_more'] and refreshed['session']['cursor']==fresh
                        assert refreshed['session']['offered']==unread[49]
                        rejected=call('wait_chat_messages',session_id=sid,timeout_seconds=1,acknowledge_through_id=unread[-1])
                        assert rejected.get('error_code') and tools.chat.row(sid)['cursor']==fresh
                        page=call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0,limit=3)
                        assert [m['id'] for m in page['messages']]==unread[:3] and page['has_more']
                        refreshed=read();next_call=refreshed['chat_prompt']['continue_with']
                        assert refreshed['session']['cursor']==fresh and next_call['arguments']['acknowledge_through_id']==unread[49]
                        remaining=call(next_call['tool'],**dict(next_call['arguments'],timeout_seconds=1,quiet_seconds=0,limit=50))
                        assert [m['id'] for m in remaining['messages']]==unread[50:]
                        call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0,acknowledge_through_id=remaining['read_through_id'])
                        results.append('refresh pages oldest unread input; explicit ACK cannot skip undelivered messages')

                        # Real spacing, one immutable queue and no duplicate dispatch.
                        clear_budget();last=add('引用和点名的目标');time.sleep(.18);p=plan(last)
                        assert p.get('state')=='READY',p
                        bubbles=[dict(kind='text',text='第一句',quote=True,mention_user_ids=['333']),dict(kind='text',text='补充原因')]
                        stamps=[];ReactionBot.intercept=lambda action,body:stamps.append(time.monotonic()) if action=='send_group_msg' else None
                        prior=len(ReactionBot.sent)
                        with ThreadPoolExecutor() as pool:
                            first=pool.submit(send,p,bubbles)
                            eventually(lambda:len(ReactionBot.sent)==prior+1)
                            duplicate=send(p,bubbles);out=first.result(15)
                        assert out['state']=='SUCCEEDED',out
                        assert len(ReactionBot.sent)==prior+2 and stamps[-1]-stamps[-2]>=.25
                        assert send(p,bubbles)['state']=='SUCCEEDED' and len(ReactionBot.sent)==prior+2
                        assert duplicate.get('error_code')=='queue_busy' or duplicate['state']=='SUCCEEDED'
                        assert [s['type'] for s in ReactionBot.sent[-2]['message']][:2]==['reply','at']
                        assert all(s['type']=='text' for s in ReactionBot.sent[-1]['message'])
                        ReactionBot.intercept=None
                        results.append('semantic bubbles spaced, first-only native quote/at, concurrent/rekey replay no duplicate')

                        # 条数上限真的被执行，而不是「源码里写着常量」（issue #136）。
                        # 两个边界分开断言：经 MCP 工具进来先被 schema 的 maxItems 拦成
                        # invalid_arguments；绕过 schema 直接问 submit() 才是 invalid_bubbles
                        # ——只测前者会把「校验根本没执行」误判成通过。
                        clear_budget();last=add('条数上限的目标');time.sleep(.18)
                        p=plan(last);assert p.get('state')=='READY',p
                        too_many=[dict(kind='text',text='第%d句'%i) for i in range(1,turns.TURN_MAX_BUBBLES+2)]
                        prior=len(ReactionBot.sent)
                        refused=send(p,too_many)
                        assert refused.get('error_code')=='invalid_arguments' and len(ReactionBot.sent)==prior,refused
                        direct=tools.chat.turns.submit(grant,dict(session_id=sid,plan_id=p['plan_id'],
                            idempotency_key=key(),bubbles=too_many),threading.Event())
                        assert direct.get('error_code')=='invalid_bubbles' and len(ReactionBot.sent)==prior,direct
                        assert 'bubbles' not in direct or all(b['state']=='PENDING' for b in direct.get('bubbles',[]))
                        # 同一计划再用刚好装满的 12 条：不因条数被拒，且 12 条全发出去。
                        exact=too_many[:-1]
                        out=send(p,exact)
                        assert out.get('state')=='SUCCEEDED' and len(out['bubbles'])==len(exact),out
                        assert all(b['state']=='SUCCEEDED' for b in out['bubbles'])
                        assert len(ReactionBot.sent)==prior+len(exact)
                        results.append('bubble cap enforced at submit(): 13 rejected with nothing sent, 12 dispatched')

                        # A new speaker quoting the first bubble must interrupt tail.
                        last=add('新的可见目标');time.sleep(.18)
                        p=plan(last);assert p.get('state')=='READY',p
                        prior=len(ReactionBot.sent)
                        def interrupt(action,body):
                            if action=='send_group_msg':
                                mid=70000+len(ReactionBot.sent)+1
                                def later():
                                    time.sleep(.08);add(uid=555,segments=[dict(type='reply',data={'id':str(mid)}),dict(type='text',data={'text':'等下，有个更正'})])
                                threading.Thread(target=later).start()
                        ReactionBot.intercept=interrupt
                        out=send(p,[dict(kind='text',text='先说一点'),dict(kind='text',text='旧尾部不应该发')])
                        ReactionBot.intercept=None
                        assert out['state']=='INTERRUPTED' and out['reason']=='stale',out
                        assert 'updated_chat' not in out and out['recovery']['automatic_write_retry'] is False
                        assert [b['state'] for b in out['bubbles']]==['SUCCEEDED','CANCELLED'] and len(ReactionBot.sent)==prior+1
                        assert send(p,[dict(kind='text',text='先说一点'),dict(kind='text',text='旧尾部不应该发')])['state']=='INTERRUPTED'
                        results.append('related reply to first bubble cancels tail; old queue cannot resume')

                        clear_budget();time.sleep(.18);prior=len(ReactionBot.sent)
                        assert plan(action='silence')['state']=='SILENT'
                        wait_batch=read()
                        # The probe below must land inside the wait window; 1 second
                        # was shorter than one MCP round trip on a loaded runner.
                        wait_plan=plan(action='wait',wait_seconds=10,batch=wait_batch)
                        assert wait_plan['state']=='WAITING',wait_plan
                        blocked=plan(batch=wait_batch)
                        assert blocked.get('error_code')=='needs_wait',blocked
                        expire(wait_plan)
                        call('wait_chat_messages',session_id=sid,timeout_seconds=2)
                        assert len(ReactionBot.sent)==prior
                        p=plan(last,expression=['reaction']);out=send(p,[dict(kind='reaction',event_id=last,reaction_id='unicode_whale',operation='add')])
                        assert out['state']=='SUCCEEDED' and len(ReactionBot.sent)==prior,out
                        assert ReactionBot.writes[-1]['emoji_id']=='128051'
                        p=plan(last,expression=['reaction']);assert send(p,[dict(kind='reaction',event_id=last,reaction_id='unicode_whale',operation='remove')])['state']=='SUCCEEDED'
                        assert ReactionBot.writes[-1]['set'] is False
                        results.append('silence/wait and reaction-only complete without added text')
                        p=plan(last,expression=['text','reaction'])
                        out=send(p,[dict(kind='text',text='一句话配一个小回应'),dict(kind='reaction',event_id=last,reaction_id='unicode_whale',operation='add')])
                        assert out['state']=='SUCCEEDED' and len(out['bubbles'])==2,out
                        results.append('mixed text and reaction share a single checked queue')

                        # issue #221：三个「等待秒数」上限（plan_chat_reply.wait_seconds
                        # 的 schema maximum、recovery 夹 needs_wait/reply_cooldown 指导值
                        # 的 min(N,…)、wait_chat_messages.minimum_wait_seconds 的 schema
                        # maximum）过去各写一个 30，而 reply_cooldown 抛出的 wait_seconds
                        # 按 reply_cooldown_seconds-(now-prev) 算、最大可达 RANGES 上限 60。
                        # 冷却配成 45 时指导值被静默压成 30、schema 也只接 30，变成「等 30、
                        # 被拒、再等」两步空转。本节钉两件可观察事实，都用既有符号（不引新
                        # 常量），未打补丁的 base 上红在 issue 症状本身，不是 import 裸奔：
                        #   ① 结构：两个 schema maximum == 冷却上限（base 上是 30≠60）；
                        #   ② 行为：冷却>30 时 recovery.retry_after_seconds ==
                        #      next_call.minimum_wait_seconds == 真实 wait_seconds（base 上
                        #      被 min(30,…) 压成 30，≠ 真实 43.x）。
                        cool_ceiling=turns.RANGES['reply_cooldown_seconds'][1]
                        wait_schema=next(s['parameters'] for s in tools.schemas(grant) if s['name']=='wait_chat_messages')
                        plan_schema=next(s['parameters'] for s in tools.schemas(grant) if s['name']=='plan_chat_reply')
                        assert plan_schema['properties']['wait_seconds']['maximum']==cool_ceiling,(plan_schema['properties']['wait_seconds'],cool_ceiling)
                        assert wait_schema['properties']['minimum_wait_seconds']['maximum']==cool_ceiling,(wait_schema['properties']['minimum_wait_seconds'],cool_ceiling)
                        clear_budget()
                        # 冷却配成 45（>30）：把此前用例的发送回执（queue!='[]'）老化到冷却
                        # 窗外，否则一设 cooldown=45，紧接着的 plan(reply) 自己就被前面的
                        # reaction/mixed 发送挡成 reply_cooldown，拿不到一个 READY 来占 prev。
                        cool_policy=policy.read_text('utf-8')
                        policy.write_text(json.dumps(dict(quiet_seconds=.15,reply_cooldown_seconds=45,bubble_min_seconds=.25,bubble_max_seconds=.35)),'utf-8')
                        with access.connect() as db:
                            db.execute("UPDATE chat_turn_plans SET updated=updated-1000 WHERE session_id=? AND queue!='[]'",(sid,))
                        cool_target=add('冷却上限的目标');time.sleep(.18);p=plan(cool_target);assert p['state']=='READY',p
                        cool_prior=len(ReactionBot.sent)
                        sent_ok=send(p,[dict(kind='text',text='先发一条占住 prev')]);assert sent_ok['state']=='SUCCEEDED',sent_ok
                        assert len(ReactionBot.sent)==cool_prior+1,sent_ok
                        # 用新目标再 plan(reply)：上一轮刚发完，仍在 45s 冷却内。
                        hot=add('还在冷却里');time.sleep(.18);p=plan(hot)
                        assert p.get('error_code')=='reply_cooldown',p
                        ws=p['wait_seconds'];rec=p['recovery']
                        assert ws>30,(ws,p)  # 真实剩余冷却 >30，不是被压过的 30
                        assert rec['retry_after_seconds']==ws,(rec,ws)  # 不被 min(30,…) 静默压小
                        assert rec['next_call']['arguments']['minimum_wait_seconds']==ws,(rec,ws)
                        assert ws<=cool_ceiling,(ws,cool_ceiling)  # 指导值天然夹进上限，不需要二次压
                        assert len(ReactionBot.sent)==cool_prior+1,p  # 冷却拒绝没有再发任何消息
                        policy.write_text(cool_policy,'utf-8')
                        results.append('issue #221: wait ceilings track cooldown ceiling; cooldown>30 keeps full wait_seconds, not clamped to 30')

                        # Seen-pixel and existing scope rules remain in force in queue.
                        media=tools.chat.media;aid=media.upsert(sid,'fixture:blue',dict(url='https://gchat.qpic.cn/fixture.png'))
                        buf=io.BytesIO();Image.new('RGB',(20,20),'blue').save(buf,'PNG')
                        p=plan(last,expression=['sticker']);out=send(p,[dict(kind='sticker',sticker_id=aid)])
                        assert out['state']!='SUCCEEDED'
                        with patch('chatlocal.mcp_chat_media.download',return_value=buf.getvalue()):
                            assert not tools.call(g['token'],'read_chat_sticker',dict(session_id=sid,sticker_id=aid),threading.Event()).isError
                            p=plan(last,expression=['sticker']);out=send(p,[dict(kind='sticker',sticker_id=aid)])
                        assert out['state']=='SUCCEEDED' and ReactionBot.sent[-1]['message'][0]['type']=='image',out
                        results.append('unknown image denied, seen image-only queue uses existing native adapter')

                        # UNKNOWN is durable, not restarted with a new caller key.
                        p=plan(last,expression=['reaction']);ReactionBot.mode='503';prior=len(ReactionBot.writes)
                        b=[dict(kind='reaction',event_id=last,reaction_id='unicode_fist',operation='add')]
                        out=send(p,b);ReactionBot.mode='ok';assert out['state']=='UNKNOWN',out
                        assert 'updated_chat' not in out
                        unknown_plan=p['plan_id']
                        assert send(p,b)['state']=='UNKNOWN' and len(ReactionBot.writes)==prior+1
                        old=tools.chat.turns;tools.chat.turns=ChatTurns(tools.chat)
                        assert send(p,b)['state']=='UNKNOWN' and len(ReactionBot.writes)==prior+1
                        expired=plan(last,batch=batch)
                        assert expired['error_code']=='batch_expired' and expired['updated_chat']['input_batch']
                        results.append('unknown no automatic retry, process epoch invalidates old input')

                        clear_budget();time.sleep(.18)
                        report=asyncio.run(wire(service,g['token'],sid));results.append('public MCP discovery -> plan -> queue -> OneBot fixture passed')
                        results.append(asyncio.run(wire_waiting(service,ui,events)))
                        # Bounded collection forces reassessment, never permission to
                        # answer a still-active speaker. Ordinary get is still gated.
                        policy.write_text('{"quiet_seconds":2,"reply_cooldown_seconds":0}','utf-8')
                        current=add('还有一段')
                        with access.connect() as db:pending=db.execute('SELECT * FROM chat_inbox WHERE session_id=? AND id=?',(sid,current)).fetchall()
                        capped=tools.chat.turns.collection(tools.chat.row(sid),pending,2,time.monotonic()-9)
                        assert capped['reason']=='collection_limit' and not capped['ready_to_send']
                        # Start the quiet-window fixture after potentially expensive
                        # context assembly; this checks the gate, not machine speed.
                        fresh_batch=read()
                        with access.connect() as db:
                            fresh=json.loads(db.execute('SELECT payload FROM chat_inbox WHERE id=?',(current,)).fetchone()[0])
                            fresh['received_at']=time.time()
                            db.execute('UPDATE chat_inbox SET payload=? WHERE id=?',(json.dumps(fresh),current))
                        assert plan(current,batch=fresh_batch).get('error_code')=='needs_wait'
                        policy.write_text(fast_policy,'utf-8');time.sleep(.18)
                        p=plan(current)
                        sid2=call('start_chat_session',conversation_id='111:group:223',persona_preset='little_whale',idempotency_key=key())['session']['id']
                        foreign=call('send_chat_reply',session_id=sid2,plan_id=p['plan_id'],idempotency_key=key(),bubbles=[dict(kind='text',text='wrong group')])
                        assert foreign['error_code']=='plan_missing'
                        prior=len(ReactionBot.sent)
                        bypass=call('send_qq_message',conversation_id='111:group:222',text='bypass forbidden',idempotency_key=key())
                        assert bypass.get('error_code') and len(ReactionBot.sent)==prior
                        with patch.object(tools.chat.receiver,'status',return_value={'state':'disconnected','account':'111'}):
                            assert send(p)['error_code']=='source_unavailable'
                        assert len(ReactionBot.sent)==prior
                        with access.connect() as db:db.execute('UPDATE chat_turn_plans SET created=created-100 WHERE id=?',(p['plan_id'],))
                        expired=send(p)
                        assert expired['error_code']=='plan_expired' and expired['updated_chat']['input_batch']
                        synthetic=add('not real',synthetic=True);time.sleep(.18)
                        assert plan(synthetic)['error_code']=='target_expired'
                        call('stop_chat_session',session_id=sid2)
                        results.append('collection cap only reassesses; cross-group, expired, synthetic and ordinary-send bypass refused')

                        # Use the returned call templates without caller-generated
                        # keys. An identical retry yields the same plan and receipt.
                        clear_budget();current=add('新的可靠调用样本');time.sleep(.18)
                        readout=call('wait_chat_messages',session_id=sid,timeout_seconds=1,limit=50)
                        began=time.monotonic()
                        backed_off=call('wait_chat_messages',session_id=sid,timeout_seconds=1,minimum_wait_seconds=.2)
                        assert time.monotonic()-began>=.18 and backed_off['messages']
                        proposed=readout['input_batch']['plan_call']
                        params=dict(proposed['arguments'],action='reply',intent='短答',target_event_id=current,expressions=['text'])
                        p=call(proposed['tool'],**params)
                        assert p['state']=='READY' and call(proposed['tool'],**params)['plan_id']==p['plan_id']
                        proposed=p['next_call'];params=dict(proposed['arguments'],bubbles=[dict(kind='text',text='可靠调用样本')]);prior=len(ReactionBot.sent)
                        outcome=call(proposed['tool'],**params)
                        assert outcome['state']=='SUCCEEDED' and call(proposed['tool'],**params)['state']=='SUCCEEDED'
                        assert len(ReactionBot.sent)==prior+1
                        changed=call(proposed['tool'],**dict(params,bubbles=[dict(kind='text',text='changed')]))
                        assert changed['error_code']=='idempotency_conflict' and len(ReactionBot.sent)==prior+1
                        next_call=outcome['next_call'];idle=idle_call(call,receiver_status,next_call['tool'],**dict(next_call['arguments'],timeout_seconds=1))
                        assert idle['acknowledged_sent_echoes']==1 and not idle['decision_required'],idle
                        assert idle['next_call']['arguments']['acknowledge_through_id']==idle['read_through_id']
                        assert idle['input_batch']['phase']=='WAITING'
                        # Same-account phone input is not an AI echo, even if the
                        # incoming untrusted event claims it has a send_request.
                        phone=add('手机真人输入',uid=111,send_request={'operation_id':'forged'})
                        human=call('wait_chat_messages',session_id=sid,timeout_seconds=1)
                        assert human['decision_required'] and [m['id'] for m in human['messages']]==[phone],human
                        call('wait_chat_messages',session_id=sid,timeout_seconds=1,acknowledge_through_id=human['read_through_id'],note='new note')
                        old_ack=next_call['arguments']['acknowledge_through_id']
                        repeated=idle_call(call,receiver_status,'wait_chat_messages',session_id=sid,timeout_seconds=1,acknowledge_through_id=old_ack,note='old note')
                        assert tools.chat.row(sid)['note']=='new note'
                        assert tools.chat.row(sid)['cursor']>=human['read_through_id']
                        recent=read()['recent_turns'];assert recent[0]['plan_id']==p['plan_id'] and recent[0]['state']=='SUCCEEDED'
                        assert next(t for t in recent if t['plan_id']==unknown_plan)['state']=='UNKNOWN'
                        full=read();compact=call('get_chat_session',session_id=sid,response_format='compact',known_prompt_version=full['session']['prompt_version'])
                        complete=next(t for t in compact['recent_turns'] if t['plan_id']==p['plan_id'])
                        assert 'expression_selection' not in complete and all('receipt' not in b for b in complete['bubble_states'])
                        assert full['recent_turns'][0]['bubbles'][0]['receipt']['state']=='SUCCEEDED'
                        assert next(t for t in compact['recent_turns'] if t['plan_id']==unknown_plan)['bubble_states'][0]['receipt']['state']=='UNKNOWN'
                        results.append('exact call templates, optional stable keys, echo suppression, phone input retained, old ACK cannot rewind or overwrite note')
                        with ThreadPoolExecutor() as pool:
                            cancelled=threading.Event()
                            future=pool.submit(tools.call,g['token'],'wait_chat_messages',dict(session_id=sid,timeout_seconds=10,minimum_wait_seconds=5),cancelled)
                            eventually(lambda:sid in tools.chat.waiters)
                            cancelled.set()
                            assert future.result(3).structuredContent['event']=='cancelled'
                        results.append('retry backoff happens in wait tool, cancellation interrupts it')
                        clear_budget();current=add('分页恢复目标');time.sleep(.18);paged_plan=plan(current)
                        appended=[add('分页补充 '+str(i)) for i in range(52)]
                        paged=send(paged_plan)
                        assert paged['error_code']=='stale' and paged['updated_chat']['context']['has_more'],paged
                        continuation=paged['recovery']['next_call']
                        assert continuation['tool']=='wait_chat_messages' and continuation['arguments']['timeout_seconds']==150
                        more=call(continuation['tool'],**dict(continuation['arguments'],timeout_seconds=1))
                        delivered={m['id'] for m in paged['updated_chat']['context']['messages']+more['messages']}
                        assert set(appended)<=delivered and not more['has_more']
                        # Revoked/withdrawn targets remain refusals, not automatic resend material.
                        time.sleep(.18);withdrawn=plan(appended[-1])
                        with access.connect() as db:
                            message_id=db.execute('SELECT message_id FROM chat_inbox WHERE id=?',(appended[-1],)).fetchone()[0]
                        tools.chat.recall(dict(self_id=111,group_id=222,message_id=message_id))
                        refused=send(withdrawn)
                        assert refused['error_code']=='target_expired' and 'updated_chat' not in refused,refused
                        # issue #217：mention_only 的对话窗口要落库，跨 wait 调用与
                        # 进程重启都存活；窗口关掉后的跳批只推游标，不许把没交付
                        # 的行记成 delivered=1。以前窗口只在 MCPChat 的内存字典里，
                        # 换一个实例（服务重启 / collector 重连）就全丢——单次 wait
                        # 的用例永远看不出这件事。
                        sid3=call('start_chat_session',conversation_id='111:group:223',persona='fixture',
                                  idempotency_key=key())['session']['id']
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
                        results.append('mention_only dialogue window survives a process restart, skip keeps delivered=0, own reply re-anchors it')
                        # Stop/revoke can run while queue sleeps; they cancel remaining work.
                        for mode in ('stop','revoke'):
                            if mode=='revoke':
                                sid=call('start_chat_session',conversation_id='111:group:222',persona_preset='little_whale',idempotency_key=key())['session']['id']
                            p=plan();prior=len(ReactionBot.sent)
                            with ThreadPoolExecutor() as pool:
                                future=pool.submit(send,p,[dict(kind='text',text='已发'),dict(kind='text',text='未发')])
                                eventually(lambda:len(ReactionBot.sent)==prior+1)
                                if mode=='stop':call('stop_chat_session',session_id=sid)
                                else:access.revoke(g['id'])
                                out=future.result(15)
                            assert len(ReactionBot.sent)==prior+1 and out['bubbles'][1]['state']=='CANCELLED',out
                        results.append('stop and revoke interrupt sleeping tail; old and v2 persona snapshots selectable')
                        assert hashlib.sha256(whale.read_bytes()).hexdigest()==before
                        # 接收线程要在 patch.dict 退出前停掉：它跑在
                        # onebot_events.py:169 的后台线程里，补丁退出后仍在处理事件，
                        # 等于拿夹具去喂一个环境已被拆掉的后端（issue #89 时它正是
                        # 这样把夹具写进实机记忆库的）。放到 finally 里已经晚了。
                        # 数据根本身另由 check_fixture_support 在模块顶层冻结兜底。
                        tools.chat.receiver.stop()
        finally:
            ReactionBot.intercept=None;ReactionBot.gate.set();events.close();invalidate_availability()
        result=dict(status='passed',checks=results,original_persona_sha256=before,wire=report,validation_cost=validation_cost,
                    mock_text_or_image_sends=len(ReactionBot.sent),mock_reactions=len(ReactionBot.writes),external_connections=0)
        if '--report' in sys.argv:Path(sys.argv[sys.argv.index('--report')+1]).write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
