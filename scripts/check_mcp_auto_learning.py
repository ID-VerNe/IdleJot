"""Automatic chat/learning loop, migration and scope isolation. Synthetic only.

The simulated host follows returned next_call links; it never enables learning
or manually chooses a learning stage. A fake clock drives batch/retry deadlines.
All network targets are loopback fixtures; model results are deterministic.
"""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import base64
import io
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

sys.path.insert(0,str(Path(__file__).parent))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_fixture_support import isolated_env, network_guard
from fixture_server import fixture_server
from check_mcp_reactions import ReactionBot,Events,ROOT
from check_mcp_chat_enrichment import DetailBot, text
from check_mcp_chat_media import fixture_image
from check_mcp_language_learning import Clock,expression_fixture,review_fixture,ground_fixture_rows,jargon_fixture
from chatlocal.store import Store
from chatlocal.mcp_routes import install_mcp_routes
from chatlocal.mcp_language_store import LanguageStore,LearningError
from chatlocal.mcp_chat_media import pixels as encode_pixels
from fastapi import FastAPI
from fastapi.testclient import TestClient


class LearningBot(DetailBot):
    def do_POST(self):
        if self.path not in ('/get_friend_list','/send_private_msg'):return super().do_POST()
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path=='/get_friend_list':data=[dict(user_id=333,nickname='Synthetic friend')]
        else:self.sent.append(body);data=dict(message_id=70000+len(self.sent))
        self.reply(dict(status='ok',retcode=0,data=data))


async def handoff_wire(service):
    """Real MCP delivery, with synthetic learning stages and loopback media."""
    chat=service.tools.chat;access=service.access;timer=[time.time()]
    chat.learning.store.clock=lambda:timer[0]
    chat.learning.store.cfg['hourly_calls']=100
    (access.path.parent/'mcp-chat-policy.json').write_text('{"quiet_seconds":0.1,"reply_cooldown_seconds":0}','utf-8')
    raw=fixture_image();photo=dict(type='image',data=dict(base64=base64.b64encode(raw).decode()))
    forward_photo=dict(type='image',data=dict(base64=base64.b64encode(fixture_image(animated=True)).decode()))
    LearningBot.forwards['learning-forward']=dict(messages=[dict(message=[text('synthetic forward body'),forward_photo])])
    serial=10000
    for category,peer in (('group',223),('direct',333)):
        timer[0]=time.time()
        g=access.create(dict(name='synthetic learning handoff',platforms=['qq'],chat=True,send=True,chat_images=True,
                            conversations=[['qq',f'111:{category}:{peer}']]))
        async with httpx.AsyncClient(headers={'Authorization':'Bearer '+g['token']},timeout=20,trust_env=False) as http:
            async with streamable_http_client(service.status()['url'],http_client=http) as (r,w,_):
                async with ClientSession(r,w) as client:
                    await client.initialize();sequence=[]
                    async def call(name,**args):
                        sequence.append(name);out=await client.call_tool(name,args)
                        assert not out.isError,out
                        return out
                    async def follow(link,**extra):return await call(link['tool'],**dict(link['arguments'],**extra))
                    started=await call('start_chat_session',conversation_id=f'111:{category}:{peer}',persona='Synthetic fixture',idempotency_key='synthetic-learning-start')
                    sid=started.structuredContent['session']['id']
                    def add(segments=None,own=False):
                        nonlocal serial
                        serial+=1
                        event=dict(post_type='message_sent' if own else 'message',message_type='group' if category=='group' else 'private',
                            self_id=111,user_id=111 if own else 333,message_id=-serial,time=time.time(),sender=dict(user_id=111 if own else 333),
                            message=segments or [text('synthetic learning input '+str(serial))],
                            **(dict(group_id=peer) if category=='group' else dict(target_id=peer,sub_type='friend')))
                        LearningBot.history[str(-serial)]=event;chat.receive(event)
                        with access.connect() as db:eid=db.execute('SELECT id FROM chat_inbox WHERE session_id=? AND message_id=?',(sid,str(-serial))).fetchone()[0]
                        return eid,-serial
                    async def silence(packet):
                        out=await follow(packet['next_call'],action='silence',intent='synthetic observation')
                        assert out.structuredContent['state']=='SILENT'
                        return out.structuredContent['next_call']
                    async def send(packet):
                        at=len(sequence)
                        await asyncio.sleep(.12)
                        plan=await follow(packet['next_call'],action='reply',expressions=['text'],intent='synthetic reply')
                        sent=await follow(plan.structuredContent['next_call'],expression_ids=[],bubbles=[dict(kind='text',text='synthetic answer')])
                        assert sent.structuredContent['state']=='SUCCEEDED'
                        assert sequence[at:]==['plan_chat_reply','send_chat_reply']
                        return sent.structuredContent['next_call']
                    # Seal real incoming material, without touching history/persona files.
                    for i in range(10):
                        original,_=add([text('synthetic seed '+str(i))]+([photo,dict(type='forward',data=dict(id='learning-forward'))] if i==9 else []))
                    seed_mid=-serial
                    packet=await call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0)
                    link=await silence(packet.structuredContent);timer[0]+=31;chat.learning.tick()
                    wake=await follow(link,timeout_seconds=1,quiet_seconds=0)
                    assert wake.structuredContent['event']=='learning_ready',(category,wake.structuredContent['event'],chat.learning.status(sid,g['id']))
                    before=chat.row(sid)['cursor'];eid,_=add()
                    with patch.object(chat.learning.store,'claim',side_effect=AssertionError('claimed despite chat')):
                        claimed=await follow(wake.structuredContent['next_call'])
                        duplicate=await follow(wake.structuredContent['next_call'])
                    data=claimed.structuredContent;updated=data['updated_chat']
                    assert data['state']=='no_task' and data['reason']=='chat_pending' and data['next_call']==updated['next_call']
                    assert updated['decision_required'] and [m['id'] for m in updated['messages']]==[eid]
                    assert chat.row(sid)['cursor']==before and duplicate.structuredContent['next_call']['tool']=='plan_chat_reply'
                    link=await send(updated)
                    # Only persisted successful-send echoes: consume, never plan on them.
                    with access.connect() as db:db.execute('UPDATE chat_sessions SET cursor=? WHERE id=?',(eid,sid))
                    echo=await call('claim_chat_learning',session_id=sid)
                    assert not echo.structuredContent['updated_chat']['decision_required']
                    assert echo.structuredContent['next_call']['tool']=='wait_chat_messages'
                    wake=await follow(link,timeout_seconds=1,quiet_seconds=0)
                    task=(await follow(wake.structuredContent['next_call'])).structuredContent['task']
                    assert (await call('claim_chat_learning',session_id=sid)).structuredContent['reason']=='return_to_wait'
                    eid,_=add([dict(type='reply',data=dict(id=str(seed_mid))),photo])
                    before=chat.row(sid)['cursor']
                    real_image=chat.media.event_image
                    def pixels(*args,**kw):
                        assert not chat.lock._is_owned()
                        with access.connect() as db:db.execute('BEGIN IMMEDIATE')
                        return real_image(*args,**kw)
                    with patch.object(chat.details,'enrich',wraps=chat.details.enrich) as enrich, \
                         patch.object(chat.media,'event_image',side_effect=pixels) as reads, \
                         patch('chatlocal.mcp_chat_media.pixels',wraps=encode_pixels) as encoded:
                        submitted=await follow(task['submit_call'],invocation_id='synthetic-stage-1',result=dict(expressions=[]))
                    data=submitted.structuredContent;updated=data['updated_chat'];entries=updated['materials']['images']
                    assert data['state']=='completed' and data['next_call']==updated['next_call']
                    assert enrich.call_count==1 and reads.call_count==len(entries)==3,(enrich.call_count,reads.call_count,entries)
                    assert sum(not c.kwargs.get('validate_only') for c in encoded.call_args_list)==3
                    assert chat.row(sid)['cursor']==before and [m['id'] for m in updated['messages']]==[eid]
                    quote=updated['messages'][0]['referenced_messages'][0]
                    assert quote['state']=='available' and quote['segments'][2]['nodes'][0]['content'].startswith('synthetic forward body')
                    for entry in entries:
                        assert entry['event_id']==eid and entry['state']=='pixels_delivered'
                        for index in entry['content_indexes']:
                            block=submitted.content[index];assert block.type=='image'
                            with Image.open(io.BytesIO(base64.b64decode(block.data))) as im:im.load()
                    # New input after handoff still invalidates its plan.
                    latest,_=add();stale=await follow(updated['next_call'],action='reply',expressions=['text'],intent='synthetic outdated')
                    assert stale.structuredContent['error_code']=='stale'
                    fresh=stale.structuredContent['updated_chat']
                    assert any(m['id']==latest for m in fresh['context']['messages'])
                    link=await send(fresh)
                    replay=await follow(task['submit_call'],invocation_id='synthetic-stage-1',result=dict(expressions=[]))
                    assert replay.structuredContent['state']=='already_completed'
                    with chat.learning.store.db() as db:
                        assert db.execute('SELECT count(*) FROM stages WHERE job_id=? AND lease=?',(task['job_id'],task['lease'])).fetchone()[0]==1
                    await follow(link,timeout_seconds=1,quiet_seconds=0)
                    # Manual same-account phone input is not a receipt-backed echo.
                    manual,_=add(own=True);out=await call('claim_chat_learning',session_id=sid)
                    assert [m['id'] for m in out.structuredContent['updated_chat']['messages']]==[manual]
                    link=await silence(out.structuredContent['updated_chat'])
                    await follow(link,timeout_seconds=1,quiet_seconds=0)
                    recalled,mid=add();chat.recall(dict(self_id=111,message_id=mid,**(dict(group_id=peer) if category=='group' else dict(user_id=peer))))
                    out=await call('claim_chat_learning',session_id=sid)
                    assert not out.structuredContent['updated_chat']['decision_required'] and out.structuredContent['next_call']['tool']=='wait_chat_messages'
                    await follow(out.structuredContent['next_call'],timeout_seconds=1,quiet_seconds=0)
                    # Hard page boundaries preserve all inputs; no preceding history masquerades as new.
                    before=chat.row(sid)['cursor'];ids=[add()[0] for _ in range(65)]
                    out=await call('claim_chat_learning',session_id=sid);page=out.structuredContent['updated_chat'];seen=[]
                    assert chat.row(sid)['cursor']==before
                    while True:
                        seen.extend(m['id'] for m in page['messages'])
                        if not page['has_more']:break
                        page=(await follow(page['next_call'],timeout_seconds=1,quiet_seconds=0)).structuredContent
                    assert seen==ids and len(seen)==len(set(seen))
                    link=await silence(page);await follow(link,timeout_seconds=1,quiet_seconds=0)
                    add();out=await call('claim_chat_learning',session_id=sid)
                    waiting=await follow(out.structuredContent['next_call'],action='wait',intent='synthetic defer',wait_seconds=10)
                    assert waiting.structuredContent['state']=='WAITING'
                    before=chat.row(sid)['cursor'];out=await call('claim_chat_learning',session_id=sid)
                    assert out.structuredContent['updated_chat']['deferred_until'] and not out.structuredContent['updated_chat']['decision_required']
                    assert out.structuredContent['next_call']['tool']=='wait_chat_messages' and chat.row(sid)['cursor']==before
                    add();out=await call('claim_chat_learning',session_id=sid)
                    assert out.structuredContent['next_call']['tool']=='plan_chat_reply'
                    await silence(out.structuredContent['updated_chat'])
                    await call('stop_chat_session',session_id=sid)
                    access.revoke(g['id'])
                    print('PASS MCP',category,': wake→claim(updated)→plan→send; submit(saved+pixels)→stale→plan→send; replay/echo/manual/recall/65-input pages/WAITING')


async def receipt_guards(service):
    chat=service.tools.chat;access=service.access;timer=[chat.learning.store.clock()]
    chat.learning.store.clock=lambda:timer[0]
    raw=fixture_image();photo=dict(type='image',data=dict(base64=base64.b64encode(raw).decode()))
    for n,scenario in enumerate(('details','prepare','image','cancel','stop','revoke','pages','claim_cancel','claim_stop','claim_revoke')):
        claiming=scenario.startswith('claim_');fault=scenario.removeprefix('claim_')
        g=access.create(dict(name='synthetic receipt guard',platforms=['qq'],chat=True,send=True,chat_images=True,
            conversations=[['qq','111:group:223']]))
        async with httpx.AsyncClient(headers={'Authorization':'Bearer '+g['token']},timeout=20,trust_env=False) as http:
            async with streamable_http_client(service.status()['url'],http_client=http) as (r,w,_):
                async with ClientSession(r,w) as client:
                    await client.initialize()
                    async def call(name,**args):return await client.call_tool(name,args)
                    start=await call('start_chat_session',conversation_id='111:group:223',persona='Synthetic receipt',idempotency_key='synthetic-receipt-start')
                    sid=start.structuredContent['session']['id'];b=chat.learning.binding(sid,g['id'])
                    for i in range(10):chat.learning.store.observe(b,dict(peer=True,source_id=f'guard-{n}-{i}',sent_at=timer[0],at=timer[0],text='synthetic evidence'))
                    timer[0]+=31;chat.learning.tick()
                    wake=await call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0)
                    assert wake.structuredContent['event']=='learning_ready'
                    link=wake.structuredContent['next_call'];claimed=await call(link['tool'],**link['arguments'])
                    task=claimed.structuredContent['task']
                    value=dict(jargon=[]) if task['stage']=='extract_jargon' else dict(expressions=[])
                    chat.receive(dict(post_type='message',message_type='group',self_id=111,group_id=223,user_id=333,
                        message_id=-60000-n,time=time.time(),message=[text('synthetic guard input'),photo]))
                    before=chat.row(sid)['cursor']
                    if fault=='pages':
                        for i in range(31):chat.receive(dict(post_type='message',message_type='group',self_id=111,group_id=223,user_id=333,
                            message_id=-70000-i,time=time.time(),message=[text('synthetic submission page '+str(i))]))
                    original=chat.media.event_image
                    def interrupt(*args,**kw):
                        if fault=='image':raise OSError('synthetic unreadable image')
                        result=original(*args,**kw)
                        if fault=='cancel':args[2].set()
                        elif fault=='stop':chat.stop(sid,g['id'],'synthetic stop')
                        elif fault=='revoke':access.revoke(g['id'])
                        return result
                    target=chat.details if fault=='details' else __import__('chatlocal.mcp_chat_materials',fromlist=['prepare']) if fault=='prepare' else chat.media
                    method='enrich' if fault=='details' else 'prepare' if fault=='prepare' else 'event_image'
                    effect=OSError('synthetic material failure') if fault in ('details','prepare') else interrupt
                    with patch.object(target,method,side_effect=effect):
                        out=await call('claim_chat_learning',session_id=sid) if claiming else await call('submit_chat_learning',**task['submit_call']['arguments'],invocation_id='synthetic-guard-call',result=value)
                    data=out.structuredContent
                    if claiming:
                        assert 'updated_chat' not in data and not [c for c in out.content if c.type=='image']
                        with chat.learning.store.db() as db:assert db.execute('SELECT count(*) FROM stages WHERE job_id=? AND lease=?',(task['job_id'],task['lease'])).fetchone()[0]==0
                        chat.stop(sid,g['id'],'synthetic cleanup');access.revoke(g['id'])
                        print('PASS MCP',scenario,': no learning claim/save or message/pixel disclosure')
                        continue
                    assert data['state'] in ('completed','stage_completed'),data
                    with chat.learning.store.db() as db:
                        assert db.execute('SELECT count(*) FROM stages WHERE job_id=? AND lease=?',(task['job_id'],task['lease'])).fetchone()[0]==1
                    if fault=='pages':
                        page=data['updated_chat'];seen=[]
                        assert chat.row(sid)['cursor']==before and page['has_more'] and data['next_call']==page['next_call']
                        while True:
                            seen.extend(m['id'] for m in page['messages'])
                            if not page['has_more']:break
                            link=page['next_call'];page=(await call(link['tool'],**dict(link['arguments'],timeout_seconds=1,quiet_seconds=0))).structuredContent
                        assert len(seen)==len(set(seen))==32 and page['next_call']['tool']=='plan_chat_reply'
                    elif fault=='image':
                        assert not [c for c in out.content if c.type=='image']
                        assert data['updated_chat']['messages'] and data['updated_chat']['materials']['images'][0]['state']=='unavailable'
                    else:
                        assert not [c for c in out.content if c.type=='image']
                        assert data['chat_error']=='handoff_unavailable' and 'updated_chat' not in data and 'next_call' not in data
                    if fault not in ('stop','revoke'):
                        repeated=await call('submit_chat_learning',**task['submit_call']['arguments'],invocation_id='synthetic-guard-call',result=value)
                        assert repeated.structuredContent['state']=='already_completed'
                    chat.stop(sid,g['id'],'synthetic cleanup');access.revoke(g['id'])
                    print('PASS MCP saved receipt survives',fault,': submit→one durable stage; no unintended pixels; idempotent where authorized')


def migration():
    clock=Clock();scope='qq:111:group:222'
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'mcp-language.sqlite3';store=LanguageStore(path,clock=clock)
        b=dict(scope=scope,gid='old-a',sid='old-session',epoch='old-process')
        store.attach(b)
        with store.db() as db:
            db.execute("UPDATE settings SET enabled=0,allow_degraded=0,last_learned=0")
            db.execute("INSERT INTO settings(scope,gid,enabled,allow_degraded,since,last_batch) VALUES(?,'old-b',1,1,1,1)",(scope,))
            for i in range(9):
                db.execute("INSERT INTO expressions(id,scope,situation,style,count,updated,independence) VALUES(?,?,?,?,7,1,'degraded')",(str(i),scope,'场景'+str(i),'表达'+str(i)))
            db.execute("INSERT INTO jargon(id,scope,term,folded,count,meaning,is_jargon,manual,enabled,updated) VALUES('word',?,'合成词','合成词',8,'人工释义',1,1,0,1)",(scope,))
            for table in ('language_libraries','language_runs','language_migrations','learning_failures'):db.execute('DROP TABLE '+table)
        store=LanguageStore(path,clock=clock)
        assert len(list(Path(tmp).glob('*.before-auto-*.sqlite3')))==1
        b.update(gid='new-grant',sid='new-session',epoch='new-process');store.attach(b)
        saved=store.status(b,records=True)
        assert not saved['enabled'] and saved['library']['last_completed_at'] is None
        assert len(saved['expressions'])==9 and all(e['count']==7 for e in saved['expressions'])
        assert saved['jargon'][0]['manual'] and not saved['jargon'][0]['enabled']
        store.configure(b,enabled=True)
        assert not store.context(b,'nine',[],'')['candidates']
        assert saved['library']['checked_expressions']==0 and saved['library']['pending_expressions']==9
        ground_fixture_rows(store,scope)
        assert not store.context(b,'nine-verified',[],'')['candidates']
        with store.db() as db:db.execute("INSERT INTO expressions(id,scope,situation,style,count,updated,independence) VALUES('tenth',?,'第十个','方式',2,1,'degraded')",(scope,))
        ground_fixture_rows(store,scope)
        candidates=store.context(b,'ten',[],'')['candidates'];assert 0<len(candidates)<=10
        store.select(b,'ten',[e['id'] for e in candidates[:5]])
        for i in range(10):store.observe(b,dict(source_id=i,sent_at=clock(),text='合成材料',at=clock(),peer=True))
        clock.advance(30);wake=store.readiness(b);assert wake['ready']
        with ThreadPoolExecutor(2) as pool:claims=list(pool.map(lambda _:store.claim(b,'context-a',wake_id=wake['wake_id']),range(2)))
        assert sum(t is not None for t in claims)==1
        t=next(t for t in claims if t)
        failed=store.submit(b,t['job_id'],t['lease'],'fake-failure',failure='timeout')
        assert store.submit(b,t['job_id'],t['lease'],'fake-failure',failure='timeout')['already_recorded']
        independent=store.claim(b,'context-a');assert independent['stage']=='extract_jargon'
        store.submit(b,independent['job_id'],independent['lease'],'independent-jargon',dict(jargon=[]))
        assert store.readiness(b)['reason']=='retry_cooldown'
        clock.advance(15);new_wake=store.readiness(b);assert new_wake['ready'] and new_wake['wake_id']!=wake['wake_id']
        assert not store.claim(b,'context-a',wake_id=wake['wake_id'])
        retry=store.claim(b,'context-a',wake_id=new_wake['wake_id']);assert retry
        store.cancel(dict(sid=b['sid']))
        try:store.submit(b,retry['job_id'],retry['lease'],'late-call',dict(expressions=[],jargon=[]))
        except LearningError as e:assert e.code=='task_unavailable'
        else:raise AssertionError('late result accepted')
        store=LanguageStore(path,clock=clock);b.update(gid='third-grant',sid='third-session',epoch='third-process');store.attach(b)
        state=store.status(b,records=True)
        assert state['enabled'] and state['expression_count']==10 and state['jargon']==saved['jargon']
        assert len(list(Path(tmp).glob('*.before-auto-*.sqlite3')))==1
        for changed in ('qq:999:group:222','qq:111:group:223'):
            foreign=dict(b,scope=changed,gid=changed,sid=changed);store.attach(foreign)
            assert store.status(foreign)['expression_count']==0 and store.status(foreign)['jargon_count']==0
        # Same native event+time in a new grant never increments a second time.
        for i in range(10):store.observe(b,dict(source_id=i,sent_at=1000.,text='合成材料',at=clock(),peer=True))
        assert store.status(b)['buffered_messages']==0
        print('PASS migration backup/rerun, 9/10 threshold, human changes/pause, new grant/restart, scope isolation, atomic claim, retry receipts')


def main():
    assert Path(__import__('chatlocal.mcp_language_learning',fromlist=['ChatLearning']).__file__).resolve()==ROOT/'chatlocal/mcp_language_learning.py'
    migration()
    events=Events();ReactionBot.sent=[];ReactionBot.writes=[];ReactionBot.intercept=None
    allowed_ports={events.port}
    with fixture_server(LearningBot, ports=allowed_ports) as upstream:
        restrict,_=network_guard(allowed_ports)
        try:
            with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',isolated_env(),clear=True),patch('socket.create_connection',restrict):
                root=Path(tmp);data=Store(root/'chats.sqlite3')
                (root/'.env').write_text(f'REPLY_ONEBOT_URL=http://127.0.0.1:{upstream.server_port}\nREPLY_ONEBOT_WS_URL=ws://127.0.0.1:{events.port}\nREPLY_ONEBOT_WS_TOKEN=event-fixture\n','utf-8')
                with patch('chatlocal.onebot.ROOT',root),patch('chatlocal.message_sender.ROOT',root):
                    app=FastAPI();service=install_mcp_routes(app,data);tools=service.tools;access=service.access;chat=tools.chat
                    with TestClient(app) as ui:
                        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                        allowed_ports.add(port);access.configure(True,port);service.start()
                        timer=[time.time()];chat.learning.store.clock=lambda:timer[0]
                        cfg=dict(name='auto-fixture',platforms=['qq'],chat=True,send=True,conversations=[['qq','111:group:222']])
                        g=access.create(cfg);headers={'X-ChatWeave-UI':'1'};serial=0
                        def call(name,**args):return tools.call(g['token'],name,args,threading.Event()).structuredContent
                        def key():
                            nonlocal serial
                            serial+=1;return f'fixture-{serial:04}'
                        body=dict(connection_id=g['id'],conversation_id='111:group:222',persona_preset='little_whale_v2',idempotency_key=key())
                        assert ui.post('/api/mcp/chats/start',json=body).status_code==403
                        click=ui.post('/api/mcp/chats/start',json=body,headers=headers).json();sid=click['session_id']
                        overview=ui.get('/api/mcp/chat-overview').json()['groups'][0]
                        assert overview['learning_state']=='等待 Agent',overview
                        # Usual host entrance resumes the UI session, without any learning command.
                        start=call('start_chat_session',conversation_id='111:group:222',persona_preset='little_whale_v2',idempotency_key=key())
                        assert start['session']['id']==sid and call('get_chat_learning',session_id=sid)['enabled']
                        def receive(n,base=100):
                            for i in range(n):chat.receive(dict(post_type='message',message_type='group',self_id=111,group_id=222,message_id=-base-i,user_id=333,
                                time=int(time.time()),sender={'user_id':333},message=[dict(type='text',data={'text':f'合成表达 云朵开机 {i}'})]))
                        def follow(link):
                            args=dict(link['arguments'])
                            if link['tool']=='wait_chat_messages':args.update(timeout_seconds=1,quiet_seconds=0)
                            return call(link['tool'],**args)
                        def silence(packet):
                            assert packet['event']=='messages'
                            decision=call('plan_chat_reply',session_id=sid,batch_id=packet['input_batch']['id'],idempotency_key=key(),action='silence',intent='合成场景潜水',expressions=[])
                            assert decision['state']=='SILENT' and decision['next_call']['tool']=='wait_chat_messages',decision
                            return decision['next_call']
                        receive(10)
                        packet=call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0)
                        link=silence(packet);timer[0]+=29
                        assert follow(link)['event']=='idle'
                        # Clock, not claim or an eleventh message, seals the batch.
                        timer[0]+=1;chat.learning.tick()
                        with chat.learning.store.db() as db:assert db.execute('SELECT count(*) FROM batches').fetchone()[0]==1
                        began=time.perf_counter();wake=follow(link);wake_ms=(time.perf_counter()-began)*1000
                        assert wake['event']=='learning_ready' and wake['continue_waiting']
                        # Chat arriving between wake and claim must win, without claiming or discarding a stage.
                        receive(1,490)
                        yielded=follow(wake['next_call'])
                        assert yielded['state']=='no_task' and yielded['reason']=='chat_pending' and 'task' not in yielded
                        assert yielded['next_call']==yielded['updated_chat']['next_call'] and yielded['next_call']['tool']=='plan_chat_reply'
                        link=silence(yielded['updated_chat']);wake=follow(link)
                        t=follow(wake['next_call'])['task'];assert t['stage']=='extract'
                        assert follow(wake['next_call'])['state']=='no_task'
                        began=time.perf_counter();busy=follow(link);lease_ms=(time.perf_counter()-began)*1000
                        assert busy['event']=='idle' and lease_ms>=900
                        # Model works on one stage while a fresh message arrives.
                        receive(1,500)
                        ids=[m['source_id'] for m in t['material']['messages']]
                        result=dict(expressions=[expression_fixture(t['material']['messages'][i],'情境'+str(i),'方式'+str(i)) for i in range(3)])
                        submitted=call('submit_chat_learning',**t['submit_call']['arguments'],invocation_id=key(),result=result)
                        assert call('claim_chat_learning',session_id=sid)['reason']=='chat_pending'
                        assert submitted['next_call']==submitted['updated_chat']['next_call'] and submitted['next_call']['tool']=='plan_chat_reply'
                        packet=submitted['updated_chat'];assert packet['event']=='messages'
                        link=silence(packet);wake=follow(link);assert wake['event']=='learning_ready'
                        tj=follow(wake['next_call'])['task'];assert tj['stage']=='extract_jargon'
                        sj=call('submit_chat_learning',**tj['submit_call']['arguments'],invocation_id=key(),result=jargon_fixture(tj))
                        wake=follow(sj['next_call']);assert wake['event']=='learning_ready'
                        t2=follow(wake['next_call'])['task'];assert t2['stage']=='review'
                        checked=review_fixture(t2)
                        final=call('submit_chat_learning',**t2['submit_call']['arguments'],invocation_id=key(),result=checked)
                        assert call('submit_chat_learning',**t2['submit_call']['arguments'],invocation_id=key(),result=checked)['state']=='already_completed'
                        assert final['state']=='completed';idle=follow(final['next_call']);assert idle['event']=='idle'
                        # Empty wait is bounded, no immediate no-task spin.
                        began=time.perf_counter();idle=follow(final['next_call']);idle_ms=(time.perf_counter()-began)*1000
                        assert idle['event']=='idle' and 900<=idle_ms<2500,(idle['event'],round(idle_ms,2))
                        library=ui.get('/api/mcp/chat-library',params={'conversation_id':'111:group:222'}).json()
                        assert library['library']['checked_expressions']==3 and not library['library']['expressions_in_use']
                        assert library['library']['known_jargon']==0 and library['library']['observing_jargon']==1
                        assert library['run']['completed_batches']==1 and library['library']['last_completed_at']
                        # Budget cannot be reset by reconnecting/new grant.
                        budget_change=dict(conversation_id='111:group:222',change=dict(hourly_calls=2))
                        assert ui.post('/api/mcp/chat-library',json=budget_change,headers=headers).json()['hourly_call_budget']==2
                        timer[0]+=31;receive(10,600)
                        packet=follow(final['next_call']);link=silence(packet)
                        began=time.perf_counter();idle=follow(link);budget_ms=(time.perf_counter()-began)*1000
                        assert idle['event']=='idle' and budget_ms>=900
                        assert ui.get('/api/mcp/chat-overview').json()['groups'][0]['learning_state']=='预算暂停'
                        budget_change['change']['hourly_calls']=8
                        assert ui.post('/api/mcp/chat-library',json=budget_change,headers=headers).json()['hourly_call_budget']==8
                        wake=follow(link);t=follow(wake['next_call'])['task']
                        failed=call('submit_chat_learning',**t['submit_call']['arguments'],invocation_id=key(),failure='timeout')
                        # A cooling expression task must not block jargon work.
                        jwake=follow(failed['next_call']);assert jwake['event']=='learning_ready'
                        jt=follow(jwake['next_call'])['task'];assert jt['stage']=='extract_jargon'
                        jf=call('submit_chat_learning',**jt['submit_call']['arguments'],invocation_id=key(),result=dict(jargon=[]))
                        began=time.perf_counter();cool=follow(failed['next_call']);cooldown_ms=(time.perf_counter()-began)*1000
                        assert cool['event']=='idle' and cooldown_ms>=900
                        timer[0]+=15;wake=follow(failed['next_call']);t=follow(wake['next_call'])['task']
                        call('stop_chat_session',session_id=sid)
                        assert call('submit_chat_learning',**t['submit_call']['arguments'],invocation_id=key(),result=result).get('state')!='completed'
                        chat.receiver.stop()
                        retained=ui.get('/api/mcp/chat-library',params={'conversation_id':'111:group:222'}).json()
                        assert retained['expression_count']==3 and retained['jargon_count']==1
                        assert ui.get('/api/mcp/chat-overview').json()['groups'][0]['learning_state']=='积累已保留'
                        access.revoke(g['id']);old=g;g=access.create(cfg)
                        start=call('start_chat_session',conversation_id='111:group:222',persona_preset='little_whale_v2',idempotency_key=key());sid=start['session']['id']
                        current=call('get_chat_learning',session_id=sid)
                        assert current['enabled'] and current['expression_count']==3 and current['jargon_count']==1 and current['run']['completed_batches']==0
                        assert current['hourly_call_budget']==8
                        from chatlocal.mcp_access import AccessDenied
                        try:tools.call(old['token'],'get_chat_learning',dict(session_id=sid),threading.Event())
                        except AccessDenied:pass
                        else:raise AssertionError('revoked token regained access')
                        # UI pause persists; same-account new grants cannot undo it.
                        change=dict(conversation_id='111:group:222',change=dict(enabled=False))
                        assert ui.post('/api/mcp/chat-library',json=change,headers=headers).json()['enabled'] is False
                        call('stop_chat_session',session_id=sid);g=access.create(cfg)
                        sid=call('start_chat_session',conversation_id='111:group:222',persona_preset='little_whale_v2',idempotency_key=key())['session']['id']
                        assert not call('get_chat_learning',session_id=sid)['enabled']
                        assert not ReactionBot.sent and not ReactionBot.writes
                        cost=dict(wake_ms=round(wake_ms,2),empty_wait_ms=round(idle_ms,2),budget_wait_ms=round(budget_ms,2),lease_wait_ms=round(lease_ms,2),cooldown_wait_ms=round(cooldown_ms,2),completed_model_stages=3,
                                  expression_count=3,jargon_candidates=1,new_model_services=0,qq_writes=0,
                                  host_boundary='one in-flight stage; lease 120s, host generation cannot be interrupted')
                        print('PASS start→silence→timer→wait→one stage→new chat first→review→persist→stop/revoke/new grant')
                        print(json.dumps(cost,ensure_ascii=False))
                        asyncio.run(handoff_wire(service))
                        asyncio.run(receipt_guards(service))
        finally:
            events.close()


if __name__=='__main__':main()
