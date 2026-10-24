"""Mandatory expression choice through real MCP HTTP; fake QQ, no LLM calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parent))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_fixture_support import isolated_env, network_guard
from check_mcp_reactions import ReactionBot, Events, eventually, ROOT
from check_mcp_language_learning import ground_fixture_rows
from chatlocal.store import Store
from chatlocal.mcp_routes import install_mcp_routes
from chatlocal.mcp_chat_turns import ChatTurns
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import httpx


async def wire(service,connection,sid,ui):
    tools=service.tools;learning=tools.chat.learning;scope='qq:111:group:222';serial=0
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+connection['token']},trust_env=False,timeout=20) as http:
        async with streamable_http_client(service.status()['url'],http_client=http) as (read,write,_):
            async with ClientSession(read,write) as client:
                await client.initialize()
                names={t.name for t in (await client.list_tools()).tools}
                assert {'plan_chat_reply','select_chat_expressions','send_chat_reply'}<=names
                async def call(name,**args):return (await client.call_tool(name,args)).structuredContent
                def seed(n):
                    with learning.store.db() as db:
                        db.execute('DELETE FROM expressions WHERE scope=?',(scope,))
                        db.execute('DELETE FROM expression_grounding WHERE scope=?',(scope,))
                        db.execute('DELETE FROM selections WHERE scope=?',(scope,))
                        for i in range(n):
                            db.execute('INSERT INTO expressions(id,scope,situation,style,count,updated,independence) VALUES(?,?,?,?,2,?,?)',
                                (f'{i:032x}',scope,f'合成情境{i}',f'合成群友形式{i}',time.time(),'degraded'))
                    ground_fixture_rows(learning.store,scope)
                async def plan(expressions=('text',),action='reply'):
                    nonlocal serial
                    serial+=1
                    packet=await call('get_chat_session',session_id=sid)
                    target=next(m for m in reversed(packet['context']['messages']) if not m['is_self'])
                    return await call('plan_chat_reply',session_id=sid,batch_id=packet['input_batch']['id'],
                        action=action,expressions=list(expressions),target_event_id=target['id'],
                        intent='合成测试意图',idempotency_key=f'usage-plan-{serial}')
                async def send(p,bubbles=None,**extra):
                    return await call('send_chat_reply',session_id=sid,plan_id=p['plan_id'],
                        bubbles=bubbles or [dict(kind='text',text='仅发送给本机模拟 OneBot')],**extra)
                async def select(p,ids):
                    return await call('select_chat_expressions',session_id=sid,plan_id=p['plan_id'],expression_ids=ids)
                def pause(value):
                    result=ui.put(f'/api/mcp/chats/{sid}/learning',json=dict(enabled=not value),headers={'X-ChatWeave-UI':'1'})
                    assert result.status_code==200
                seed(9);p=await plan()
                assert p['next_call']['tool']=='send_chat_reply' and p['expression_selection']['reason']=='insufficient_expressions'
                assert (await send(p))['state']=='SUCCEEDED'
                seed(10);p=await plan();before=len(ReactionBot.sent)
                assert p['phase']=='SELECTING_EXPRESSION' and p['next_call']['tool']=='send_chat_reply'
                assert p['next_call']['requires']==['expression_ids','bubbles']
                assert p['learned_language']['send_call']['tool']=='send_chat_reply'
                assert p['expression_selection']['state']=='pending'
                blocked=await send(p)
                assert blocked['error_code']=='expression_selection_required' and len(ReactionBot.sent)==before,blocked
                assert blocked['recovery']['next_call']['tool']=='send_chat_reply'
                # The legacy single-send path uses the same gate.
                legacy=await call('send_chat_message',session_id=sid,plan_id=p['plan_id'],text='cannot bypass',idempotency_key='usage-legacy')
                assert legacy['error_code']=='expression_selection_required' and len(ReactionBot.sent)==before
                foreign=await select(p,['f'*32]);assert foreign['error_code']=='invalid_selection'
                chosen=await select(p,[])
                assert chosen['expression_decision']==dict(state='none',reason='explicit_none') and chosen['selected']==[]
                assert 'candidates' not in chosen and chosen['next_call']['tool']=='send_chat_reply'
                assert (await select(p,[]))['expression_selection']==chosen['expression_selection']
                completed=await send(p)
                assert completed['state']=='SUCCEEDED' and completed['expression_selection']['state']=='none'
                assert (await send(p))['state']=='SUCCEEDED' and len(ReactionBot.sent)==before+1
                print('PASS 9/10 threshold, wire discovery, explicit empty choice, legacy gate, unknown IDs and replay')

                # The next model pass chooses references and writes the reply in
                # one public HTTP call; no extra selector call is needed.
                p=await plan();before=len(ReactionBot.sent)
                invalid=await send(p,expression_ids=['f'*32])
                assert invalid['error_code']=='invalid_selection' and len(ReactionBot.sent)==before
                completed=await send(p,expression_ids=[])
                assert completed['state']=='SUCCEEDED' and completed['expression_selection']['state']=='none'
                assert (await send(p,expression_ids=[]))['state']=='SUCCEEDED' and len(ReactionBot.sent)==before+1
                assert (await send(p,expression_ids=[p['learned_language']['candidates'][0]['id']]))['error_code']=='idempotency_conflict'
                p=await plan();ids=[x['id'] for x in p['learned_language']['candidates'][:2]]
                completed=await send(p,expression_ids=ids)
                assert completed['state']=='SUCCEEDED' and completed['expression_selection']['selected_ids']==ids
                assert completed['expression_selection']['candidate_context']['sha256']
                assert completed['expression_selection']['selected_context']['sha256']
                p=await plan()
                legacy=await call('send_chat_message',session_id=sid,plan_id=p['plan_id'],
                    text='兼容单条入口也可以随正文选择',expression_ids=[],idempotency_key='inline-legacy')
                assert legacy['state']=='SUCCEEDED'
                from chatlocal.message_sender import SendUncertain,QQSender
                p=await plan();before=len(ReactionBot.sent)
                with patch.object(QQSender,'send',side_effect=SendUncertain('fixture unknown')) as dispatch:
                    unknown=await send(p,expression_ids=[])
                    assert unknown['state']=='UNKNOWN' and dispatch.call_count==1
                    assert (await send(p,expression_ids=[]))['state']=='UNKNOWN' and dispatch.call_count==1
                assert len(ReactionBot.sent)==before
                print('PASS inline empty/selected choices, foreign rejection, immutable retry, legacy compatibility and UNKNOWN no redispatch')

                # Normal wire path after a wait result needs exactly two
                # decision tool calls, including an actual candidate choice.
                seed(10)
                with service.access.connect() as db:
                    offered=db.execute('SELECT offered FROM chat_sessions WHERE id=?',(sid,)).fetchone()[0]
                fixture=dict(post_type='message',message_type='group',self_id=111,group_id=222,user_id=333,
                    message_id=-987654,message_seq=987654,sender={'user_id':333},time=int(time.time()),
                    message=[dict(type='text',data={'text':'two pass fixture'})])
                ReactionBot.history['-987654']=deepcopy(fixture);tools.chat.receive(fixture)
                packet=await call('wait_chat_messages',session_id=sid,acknowledge_through_id=offered,timeout_seconds=1)
                assert packet['messages'][-1]['content']=='two pass fixture'
                assert 'reactions' not in packet['chat_guidance'] and 'chat_prompt' not in packet
                # Message bodies/history are intentionally not capped to meet
                # a host-specific file threshold; only guidance is compacted.
                assert len(json.dumps(packet['chat_guidance'],ensure_ascii=False,separators=(',',':')))<2500
                with service.access.connect() as db:last_call=db.execute('SELECT max(id) FROM calls').fetchone()[0]
                p=await call('plan_chat_reply',session_id=sid,batch_id=packet['input_batch']['id'],action='reply',
                    target_event_id=packet['messages'][-1]['id'],expressions=['text'],intent='two pass fixture')
                assert p['quote_guidance']['threshold']==3 and p['quote_guidance']['messages_after_target']==0
                assert not p['quote_guidance']['quote_recommended']
                ids=[p['learned_language']['candidates'][0]['id']]
                out=await send(p,expression_ids=ids,bubbles=[dict(kind='text',text='fixture quoted reply',quote=True)])
                assert out['state']=='SUCCEEDED' and ReactionBot.sent[-1]['message'][0]==dict(type='reply',data=dict(id='-987654'))
                with service.access.connect() as db:
                    called=[r[0] for r in db.execute("SELECT tool FROM calls WHERE id>? AND tool!='chat_expression' ORDER BY id",(last_call,))]
                assert called==['plan_chat_reply','send_chat_reply'],called
                full=await call('wait_chat_messages',session_id=sid,timeout_seconds=1,response_format='full')
                assert 'reactions' in full['chat_guidance']
                print('PASS compact wait -> plan -> inline select/send: exactly two decision calls; full diagnostics available')

                p=await plan();ids=[x['id'] for x in p['learned_language']['candidates'][:2]]
                args=dict(session_id=sid,plan_id=p['plan_id'],expression_ids=ids)
                # Same choice can arrive twice concurrently without changing it.
                def choose():return tools.call(connection['token'],'select_chat_expressions',args,threading.Event()).structuredContent
                with ThreadPoolExecutor(2) as pool:
                    first=pool.submit(choose);second=pool.submit(choose);a=first.result(10);b=second.result(10)
                assert a['selected']==b['selected'] and a['expression_selection']==b['expression_selection']
                audit=a['expression_selection'];assert audit['selected_ids']==ids
                assert audit['delivery']=='tool_result_prepared' and audit['host_injection']==audit['model_use']=='unverified'
                assert audit['candidate_context']['sha256'] and audit['selected_context']['sha256']
                assert not any(k in json.dumps(audit,ensure_ascii=False) for k in ('合成群友形式','合成情境','合成测试意图'))
                assert (await select(p,[]))['error_code']=='idempotency_conflict'
                completed=await send(p);assert completed['state']=='SUCCEEDED'
                assert completed['expression_selection']==audit
                with service.access.connect() as db:
                    stored=json.loads(db.execute('SELECT payload FROM chat_turn_plans WHERE id=?',(p['plan_id'],)).fetchone()[0])
                    assert stored['expression_selection']==audit
                packet=await call('get_chat_session',session_id=sid)
                assert next(r for r in packet['recent_turns'] if r['plan_id']==p['plan_id'])['expression_selection']==audit
                print('PASS selected material audit, no private text in audit, atomic repeat, durable linked turn receipt')

                # Disabling a selected record invalidates the snapshot and requires a new choice.
                p=await plan();ids=[x['id'] for x in p['learned_language']['candidates'][:1]];await select(p,ids)
                learning.store.manage(learning.binding(sid,connection['id']),'expressions',ids[0],enabled=False)
                # Nine checked entries now means automatic not-applicable, not fabricated model refusal.
                out=await send(p);assert out['state']=='SUCCEEDED' and out['expression_selection']['state']=='not_applicable'
                seed(12);p=await plan();ids=[x['id'] for x in p['learned_language']['candidates'][:1]];await select(p,ids)
                learning.store.manage(learning.binding(sid,connection['id']),'expressions',ids[0],enabled=False)
                before=len(ReactionBot.sent);out=await send(p)
                assert out['error_code']=='expression_selection_required' and len(ReactionBot.sent)==before
                assert ids[0] not in [x['id'] for x in out['learned_language']['candidates']]
                await select(p,[]);assert (await send(p))['state']=='SUCCEEDED'
                p=await plan();pause(True)
                out=await send(p,expression_ids=[]);assert out['state']=='SUCCEEDED' and out['expression_selection']['reason']=='learning_disabled'
                pause(False)
                # Learning-storage failure cannot silently masquerade as a model
                # choice or stop otherwise authorized ordinary conversation.
                with patch.object(learning.store,'context',side_effect=OSError('fixture storage failure')):
                    p=await plan();out=await send(p,expression_ids=[])
                    assert out['state']=='SUCCEEDED' and out['expression_selection']['state']=='unavailable'
                    assert out['expression_selection']['reason']=='learning_unavailable'
                silent=await plan(expressions=(),action='silence')
                assert silent['state']=='SILENT' and silent['expression_selection']['reason']=='no_reply'
                # A reaction-only round requires no expression selector or text filler.
                p=await plan(expressions=('reaction',))
                assert p['next_call']['tool']=='send_chat_reply' and p['expression_selection']['reason']=='non_text_plan'
                before=len(ReactionBot.sent)
                out=await send(p,[dict(kind='reaction',event_id=p['decision']['target_event_id'],reaction_id='qq_like',operation='add')])
                assert out['state']=='SUCCEEDED' and len(ReactionBot.sent)==before and len(ReactionBot.writes)==1,out
                p=await plan(expressions=('text','reaction'))
                assert p['expression_selection']['state']=='pending'
                out=await send(p,[dict(kind='reaction',event_id=p['decision']['target_event_id'],reaction_id='qq_like',operation='remove')])
                assert out['state']=='SUCCEEDED' and out['expression_selection']['reason']=='non_text_output',out
                assert len(ReactionBot.sent)==before and len(ReactionBot.writes)==2
                # Selection is never an exemption from stale-message/stop/revoke checks.
                p=await plan();await select(p,[])
                event=dict(post_type='message',message_type='group',self_id=111,group_id=222,user_id=333,
                    message_id=-9002,time=int(time.time()),sender={'user_id':333},message=[dict(type='text',data={'text':'合成补充更正'})])
                tools.chat.receive(event)
                out=await send(p);assert out['error_code']=='stale' and len(ReactionBot.sent)==before,out
                await asyncio.sleep(.15)
                p=await plan();await select(p,[])
                await call('stop_chat_session',session_id=sid)
                assert (await send(p))['error_code']=='chat_rejected'
                assert len(ReactionBot.sent)==before
                # Turn audit survives restart, but must never revive the cancelled queue.
                turns=ChatTurns(tools.chat)
                row=turns.load(p['plan_id'],sid)
                assert row['state']=='CANCELLED' and json.loads(row['payload'])['expression_selection']['state']=='none'
                print('PASS record changes, pause, silence/reaction-only, stale/stop, restart retains audit but no dispatch')
                return dict(status='passed',transport='real MCP HTTP + loopback OneBot',real_qq_writes=0,
                    model_calls=0,mocked_text_writes=len(ReactionBot.sent),mocked_reactions=len(ReactionBot.writes))


def main():
    assert Path(__import__('chatlocal.mcp_chat_turns',fromlist=['x']).__file__).resolve()==ROOT/'chatlocal/mcp_chat_turns.py'
    events=Events();ReactionBot.sent=[];ReactionBot.writes=[];ReactionBot.history={};ReactionBot.intercept=None
    server=ThreadingHTTPServer(('127.0.0.1',0),ReactionBot)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    ports={server.server_port,events.port};restrict,_=network_guard(ports)
    try:
        with tempfile.TemporaryDirectory(prefix='tulpa-language-usage-') as tmp,patch.dict('os.environ',isolated_env(),clear=True),patch('socket.create_connection',restrict):
            root=Path(tmp);store=Store(root/'chats.sqlite3')
            (root/'.env').write_text(f'REPLY_ONEBOT_URL=http://127.0.0.1:{server.server_port}\nREPLY_ONEBOT_WS_URL=ws://127.0.0.1:{events.port}\nREPLY_ONEBOT_WS_TOKEN=event-fixture\n','utf-8')
            with patch('chatlocal.onebot.ROOT',root),patch('chatlocal.message_sender.ROOT',root):
                service=install_mcp_routes(app:=FastAPI(),store);access=service.access
                with TestClient(app) as ui:
                    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                    ports.add(port);access.configure(True,port);service.start()
                    connection=access.create(dict(name='usage-fixture',platforms=['qq'],conversations=[['qq','111:group:222']],chat=True,send=True,chat_reactions=True))
                    (access.path.parent/'mcp-chat-policy.json').write_text('{"quiet_seconds":0.1,"reply_cooldown_seconds":0}','utf-8')
                    service.tools.chat.reactions.policy_path.write_text('{"cooldown_seconds":0}','utf-8')
                    sid=service.tools.call(connection['token'],'start_chat_session',dict(conversation_id='111:group:222',persona_preset='little_whale_v2',idempotency_key='usage-start'),threading.Event()).structuredContent['session']['id']
                    event=dict(post_type='message',message_type='group',self_id=111,group_id=222,user_id=333,
                        message_id=-9001,message_seq=9001,time=int(time.time()),sender={'user_id':333},
                        message=[dict(type='text',data={'text':'合成测试消息，不来自真实群'})])
                    ReactionBot.history['-9001']=deepcopy(event);events.send(event)
                    def received():
                        with access.connect() as db:return db.execute('SELECT 1 FROM chat_inbox WHERE session_id=? AND message_id=?',(sid,'-9001')).fetchone()
                    eventually(received)
                    time.sleep(.15)
                    print(json.dumps(asyncio.run(wire(service,connection,sid,ui)),ensure_ascii=False))
    finally:server.shutdown();events.close()


if __name__=='__main__':main()
