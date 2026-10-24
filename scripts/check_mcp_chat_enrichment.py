"""Functional acceptance: isolated OneBot, MCP tools, local images. No LLM/QQ writes."""
import asyncio
import base64
from copy import deepcopy
import hashlib
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import time
import warnings
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_fixture_support import isolated_env
from check_mcp_reactions import ReactionBot, Events, eventually, ROOT
from chatlocal.mcp_routes import install_mcp_routes
from chatlocal.store import Store
from chatlocal.mcp_chat_local_media import read_path
from chatlocal.onebot import Client
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def wire(service,token,sid,eid):
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},timeout=20,trust_env=False) as http:
        async with streamable_http_client(service.status()['url'],http_client=http) as (read,write,_):
            async with ClientSession(read,write) as client:
                await client.initialize()
                assert 'read_chat_forward' in {t.name for t in (await client.list_tools()).tools}
                out=await client.call_tool('read_chat_forward',dict(session_id=sid,event_id=eid))
                assert not out.isError and out.structuredContent['event_id']==eid
                assert len(out.structuredContent['nodes'])==25 and not out.structuredContent['has_more']
                schema=next(t for t in (await client.list_tools()).tools if t.name=='read_chat_forward').inputSchema
                assert schema['properties']['limit']['default']==schema['properties']['limit']['maximum']==50
                small=await client.call_tool('read_chat_forward',dict(session_id=sid,event_id=eid,limit=20))
                assert len(small.structuredContent['nodes'])==20 and small.structuredContent['has_more']


async def wire_quote(service,token,sid,eid,detail):
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},timeout=20,trust_env=False) as http:
        async with streamable_http_client(service.status()['url'],http_client=http) as (r,w,_):
            async with ClientSession(r,w) as client:
                await client.initialize()
                out=await client.call_tool('read_chat_forward',dict(session_id=sid,event_id=eid,
                    reply_index=detail['reply_index'],forward_index=detail['forward_index'],limit=20))
                assert not out.isError,out
                first=out.structuredContent
                assert any(c.type=='image' for c in out.content),'quoted forward page omitted pixels'
                assert len(first['nodes'])==20 and first['has_more'] and first['next_call']['arguments']['reply_index']==detail['reply_index']
                continuation=first['next_call']
                out=await client.call_tool(continuation['tool'],continuation['arguments'])
                assert not out.isError and len(out.structuredContent['nodes'])==5 and not out.structuredContent['has_more'],out
                assert out.structuredContent['nodes'][0]['content']=='node 20[图片]'
                assert any(c.type=='image' for c in out.content),'quoted continuation omitted pixels'
                index=first['nodes'][0]['segments'][1]['image_index']
                image=await client.call_tool('read_chat_image',dict(session_id=sid,event_id=eid,index=index))
                assert not image.isError and any(c.type=='image' for c in image.content),image
                assert '客户端图片接入限制' in image.structuredContent['note']
                assert 'prefer_original' in image.structuredContent['note']
                for c in image.content:
                    if c.type=='image':
                        with Image.open(io.BytesIO(base64.b64decode(c.data))) as im:assert im.size==(64,48)


async def wire_pages(service,token,sid,eid,chat,grant):
    """Only returned pages supply pixels, including acknowledged parent events."""
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},timeout=20,trust_env=False) as http:
        async with streamable_http_client(service.status()['url'],http_client=http) as (r,w,_):
            async with ClientSession(r,w) as client:
                await client.initialize()
                async def page(**extra):
                    return await client.call_tool('read_chat_forward',dict(session_id=sid,event_id=eid,**extra))
                def pixels(out,count):
                    assert not out.isError and out.structuredContent['nodes'],out
                    entries=out.structuredContent['materials']['images']
                    assert len(entries)==count and len([c for c in out.content if c.type=='image'])==count
                    for e in entries:
                        assert e['event_id']==eid and e['state']=='pixels_delivered'
                        c=out.content[e['content_indexes'][0]]
                        with Image.open(io.BytesIO(base64.b64decode(c.data))) as im:assert im.size==(64,48)
                    return entries
                with chat.access.connect() as db:
                    before=db.execute('SELECT cursor FROM chat_sessions WHERE id=?',(sid,)).fetchone()[0]
                    db.execute('UPDATE chat_sessions SET cursor=? WHERE id=?',(eid,sid))
                try:
                    original=chat.media.event_image
                    with patch.object(chat.media,'event_image',wraps=original) as reads, \
                         patch('chatlocal.mcp_chat_media.download',side_effect=AssertionError('cached page downloaded')):
                        first=await page(limit=20);a=pixels(first,1)
                        assert reads.call_count==1 and reads.call_args.args[1]['index']==a[0]['image_index']
                        link=first.structuredContent['next_call'];second=await client.call_tool(link['tool'],link['arguments'])
                        b=pixels(second,1);assert a[0]['image_index']!=b[0]['image_index'] and reads.call_count==2
                        empty=await page(offset=1,limit=1);pixels(empty,0);assert reads.call_count==2
                    # Automatic recovery must still skip confirmed event pixels.
                    with patch.object(chat.media,'event_image',side_effect=AssertionError('auto-prefetch read confirmed history')):
                        restored=await client.call_tool('get_chat_session',dict(session_id=sid,response_format='compact'))
                    assert not restored.isError and not [c for c in restored.content if c.type=='image']
                    with chat.access.connect() as db:
                        db.execute('DELETE FROM calls WHERE grant_id=?',(grant['id'],))
                        db.executemany('INSERT INTO calls(grant_id,tool,at,status) VALUES(?,?,?,?)',[(grant['id'],'read_chat_image',time.time(),'ok')]*60)
                    limited=await page(limit=20)
                    assert not limited.isError and limited.structuredContent['nodes'] and not [c for c in limited.content if c.type=='image']
                    entry=limited.structuredContent['materials']['images'][0]
                    assert entry['state']=='rate_limited' and entry['read_call']['tool']=='read_chat_image'
                    with chat.access.connect() as db:
                        db.execute('DELETE FROM calls WHERE grant_id=?',(grant['id'],))
                        saved=db.execute('SELECT scope FROM grants WHERE id=?',(grant['id'],)).fetchone()[0]
                        scope=json.loads(saved);scope['chat_images']=False
                        db.execute('UPDATE grants SET scope=? WHERE id=?',(json.dumps(scope),grant['id']))
                    try:
                        denied=await page()
                        assert not denied.isError and denied.structuredContent['nodes'] and not [c for c in denied.content if c.type=='image']
                    finally:
                        with chat.access.connect() as db:db.execute('UPDATE grants SET scope=? WHERE id=?',(saved,grant['id']))
                    with patch.object(chat.media,'event_image',side_effect=ValueError('synthetic failed read')):
                        failed=await page(limit=20)
                    assert failed.structuredContent['nodes'] and failed.structuredContent['materials']['images'][0]['state']=='unavailable'
                    with patch.object(chat.turns,'policy',return_value=dict(chat.turns.policy(),detail_budget_seconds=-1)):
                        # Expire only this preparation budget, without changing
                        # the clocks used by HTTP or detail-query handling.
                        from chatlocal.mcp_chat_materials import prepare
                        body=deepcopy(first.structuredContent);body.pop('materials',None)
                        prepared,frames=prepare(chat,grant,body,threading.Event(),forward_session_id=sid)
                    assert prepared['nodes'] and not frames and prepared['materials']['images'][0]['state']=='budget_exhausted'
                    cancelled=threading.Event()
                    def cancel_read(*args,**kwargs):
                        result=original(*args,**kwargs);cancelled.set();return result
                    with patch.object(chat.media,'event_image',side_effect=cancel_read):
                        out=await asyncio.to_thread(service.tools.call,token,'read_chat_forward',dict(session_id=sid,event_id=eid),cancelled)
                    assert not [c for c in out.content if c.type=='image']
                finally:
                    with chat.access.connect() as db:db.execute('UPDATE chat_sessions SET cursor=? WHERE id=?',(before,sid))
    print('PASS: real MCP page-only pixels, stable parent indexes, acknowledged explicit reads, automatic history exclusion, quota/permission/failure/budget/cancel')


async def wire_page_guards(service,raw):
    chat=service.tools.chat;access=service.access
    g=access.create(dict(name='page guards',platforms=['qq'],conversations=[['qq','111:group:223']],send=True,chat=True,chat_images=True))
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+g['token']},timeout=20,trust_env=False) as http:
        async with streamable_http_client(service.status()['url'],http_client=http) as (r,w,_):
            async with ClientSession(r,w) as client:
                await client.initialize()
                started=await client.call_tool('start_chat_session',dict(conversation_id='111:group:223',persona='fixture',idempotency_key='page-guards-start'))
                sid=started.structuredContent['session']['id']
                def add(mid):
                    chat.receive(dict(post_type='message',message_type='group',self_id=111,group_id=223,message_id=mid,
                        user_id=444,time=time.time(),message=[dict(type='forward',data=dict(messages=[dict(message=[text('guard body'),
                            dict(type='image',data=dict(base64=base64.b64encode(raw).decode()))])]))]))
                    with access.connect() as db:return db.execute('SELECT id FROM chat_inbox WHERE session_id=? AND message_id=?',(sid,str(mid))).fetchone()[0]
                try:
                    for mode,mid in (('recall',760001),('revoke',760002)):
                        eid=add(mid)
                        await client.call_tool('get_chat_session',dict(session_id=sid,response_format='compact'))
                        original=chat.media.event_image
                        def invalidate(*args,**kwargs):
                            result=original(*args,**kwargs)
                            if mode=='recall':chat.recall(dict(self_id=111,group_id=223,message_id=mid))
                            else:access.revoke(g['id'])
                            return result
                        with patch.object(chat.media,'event_image',side_effect=invalidate):
                            out=await client.call_tool('read_chat_forward',dict(session_id=sid,event_id=eid))
                        assert not [c for c in out.content if c.type=='image'],out
                        if mode=='recall':
                            assert out.structuredContent['nodes'] and out.structuredContent['materials']['incomplete']
                        else:assert out.isError
                finally:chat.stop_all(g['id'],'isolated test ended');access.revoke(g['id'])
    print('PASS: forward page recall/revocation during pixel preparation never leaks image blocks')


async def wire_nested(service,token,raw):
    """Native MCP delivers ID-only children before the two controlled stages."""
    chat=service.tools.chat;serial=0;calls=[]
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},trust_env=False,timeout=30) as http:
        async with streamable_http_client(service.status()['url'],http_client=http) as (r,w,_):
            async with ClientSession(r,w) as client:
                await client.initialize()
                async def call(name,**args):
                    calls.append(name);out=await client.call_tool(name,args)
                    assert not out.isError,out
                    return out
                started=(await call('start_chat_session',conversation_id='111:group:223',persona='fixture',idempotency_key='nested-wire')).structuredContent
                sid=started['session']['id'];version=started['session']['prompt_version']
                assert 'persona' not in started['session'] and started['chat_prompt']['persona']=='fixture'
                full_start=(await call('start_chat_session',conversation_id='111:group:223',persona='fixture',idempotency_key='nested-wire',response_format='full')).structuredContent
                assert full_start['session']['persona']==full_start['chat_prompt']['persona']=='fixture'
                def add(segments):
                    nonlocal serial
                    serial+=1;mid=730000+serial
                    chat.receive(dict(post_type='message',message_type='group',self_id=111,group_id=223,message_id=mid,
                        user_id=444,time=time.time(),sender=dict(user_id=444),message=segments))
                    with chat.access.connect() as db:return db.execute('SELECT id FROM chat_inbox WHERE session_id=? AND message_id=?',(sid,str(mid))).fetchone()[0]
                async def wait(cursor=0):
                    return await call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0,acknowledge_through_id=cursor,known_prompt_version=version)
                async def reply(out,eid):
                    before=len(calls);await asyncio.sleep(.12)
                    plan=(await call('plan_chat_reply',**out.structuredContent['input_batch']['plan_call']['arguments'],
                        action='reply',target_event_id=eid,expressions=['text'],intent='synthetic nested fixture')).structuredContent
                    assert plan['state']=='READY',plan
                    sent=(await call('send_chat_reply',**plan['next_call']['arguments'],expression_ids=[],bubbles=[dict(kind='text',text='synthetic nested reply')])).structuredContent
                    assert sent['state']=='SUCCEEDED' and calls[before:]==['plan_chat_reply','send_chat_reply'],sent
                    assert 'note' not in sent['bubbles'][0]['receipt']
                    return sent['next_call']['arguments']['acknowledge_through_id']
                def child(ident):return dict(type='forward',data=dict(id=ident))
                def nodes(n,label):return [dict(sender=dict(user_id=444),message=[text(label+str(i))]) for i in range(n)]
                ids=['nested-38','nested-4','nested-empty-a','nested-empty-b','nested-empty-c']
                DetailBot.forwards.update({ident:dict(messages=nodes(count,'child body ')) for ident,count in zip(ids,[38,4,0,0,0])})
                DetailBot.forwards[ids[0]]['messages'][0]['message'].append(dict(type='image',data=dict(base64=base64.b64encode(raw).decode())))
                DetailBot.forwards['nested-outer']=dict(messages=[dict(message=[child(ident)]) for ident in ids])
                before=len(DetailBot.queries);eid=add([child('nested-outer')]);out=await wait()
                detail=out.structuredContent['messages'][-1]['referenced_messages'][0]
                assert [len(n['segments'][0]['nodes']) for n in detail['nodes']]==[38,4,0,0,0],detail
                assert not out.structuredContent['materials']['incomplete']
                assert 'capabilities' not in out.structuredContent['chat_guidance']
                image=out.structuredContent['materials']['images'][0]
                assert image['event_id']==eid and image['image_index']==0
                block=out.content[image['content_indexes'][0]];assert block.type=='image'
                with Image.open(io.BytesIO(base64.b64decode(block.data))) as im:assert im.size==(64,48)
                assert len(DetailBot.queries)-before==6 and chat.row(sid)['cursor']<eid
                queried=len(DetailBot.queries)
                with patch('chatlocal.mcp_chat_media.download',side_effect=AssertionError('nested cache downloaded')):
                    restored=await call('get_chat_session',session_id=sid,response_format='compact',known_prompt_version=version)
                assert len(DetailBot.queries)==queried and any(b.type=='image' for b in restored.content)
                assert restored.structuredContent['context']['messages'][-1]['referenced_messages']==out.structuredContent['messages'][-1]['referenced_messages']
                cursor=await reply(out,eid)
                full=(await call('get_chat_session',session_id=sid,response_format='full')).structuredContent
                assert 'note' in full['recent_turns'][0]['bubbles'][0]['receipt']
                # A verified quote follows the identical resource path and uses
                # its received parent event for all image handles.
                mid=740001;DetailBot.history[str(mid)]=dict(message_id=mid,self_id=111,group_id=223,message_type='group',
                    user_id=444,sender=dict(user_id=444),time=time.time(),message=[text('quote'),child('nested-outer')])
                eid=add([dict(type='reply',data=dict(id=str(mid)))]);out=await wait(cursor)
                quoted=out.structuredContent['messages'][-1]['referenced_messages'][0]['segments'][1]
                assert quoted['reply_index']==0 and quoted['forward_index']==1 and len(quoted['nodes'][0]['segments'][0]['nodes'])==38
                assert out.structuredContent['materials']['images'][0]['event_id']==eid and any(b.type=='image' for b in out.content)
                assert len(DetailBot.queries)==queried,'cached child resources were fetched again'
                cursor=await reply(out,eid)
                DetailBot.forwards['nested-repeat']=dict(messages=nodes(4,'repeat '))
                eid=add([dict(type='forward',data=dict(messages=[dict(message=[child('nested-repeat')])]*2))]);out=await wait(cursor)
                assert len(out.structuredContent['messages'][-1]['referenced_messages'][0]['nodes'])==2
                assert sum(body.get('id')=='nested-repeat' for _,body in DetailBot.queries)==1
                cursor=out.structuredContent['read_through_id']
                eid=add([dict(type='forward',data=dict(messages=[dict(message=[child('nested-later')])]*2))]);out=await wait(cursor)
                root=out.structuredContent['messages'][-1]['referenced_messages'][0];missing=root['nodes'][0]['segments'][0]
                assert missing['state']=='unavailable' and out.structuredContent['materials']['incomplete']
                assert sum(body.get('id')=='nested-later' for _,body in DetailBot.queries)==1
                DetailBot.forwards['nested-later']=dict(messages=nodes(4,'recovered '))
                link=missing['read_call'];page=(await call(link['tool'],**link['arguments'])).structuredContent
                assert len(page['nodes'][0]['segments'][0]['nodes'])==4 and not page['has_more'] and 'next_call' not in page
                assert sum(body.get('id')=='nested-later' for _,body in DetailBot.queries)==2
                await call('stop_chat_session',session_id=sid)
    print('PASS: real MCP ID-only nested/quoted forwards, 38+4 shared budget, actual parent pixels, cached repeat/recovery, executable failed-child retry and controlled plan→send')


def nested_limits(chat,row):
    from chatlocal.mcp_chat_details import sanitize_nodes,MAX_TEXT
    child=lambda ident:dict(type='forward',data=dict(id=ident))
    resources={'edge-cycle-a':[dict(message=[child('edge-cycle-b')])],
        'edge-cycle-b':[dict(message=[child('edge-cycle-a')])],
        'edge-large':[dict(message=[text('x'*1000)]) for _ in range(38)],
        'edge-nodes':[dict(message=[text('n')]) for _ in range(38)],
        'edge-depth-a':[dict(message=[child('edge-depth-b')])],
        'edge-depth-b':[dict(message=[child('edge-depth-c')])]}
    queries=[];deadlines=[]
    def query(row,action,args,deadline,cancel):
        assert not chat.lock._is_owned() and not chat.turns.lock(row['id']).locked()
        with chat.access.connect() as db:db.execute('BEGIN IMMEDIATE')
        queries.append(args['id']);deadlines.append(deadline);return dict(messages=resources[args['id']])
    def walk(v):
        yield v
        for k in ('nodes','segments'):
            for c in v.get(k,[]):yield from walk(c)
    with patch.object(chat.details,'query',side_effect=query):
        cycle=chat.details.forward(row,dict(id='edge-cycle-a'),time.monotonic()+3,threading.Event())
        assert queries==['edge-cycle-a','edge-cycle-b'] and any(v.get('state')=='cycle' for v in walk(cycle))
        assert deadlines[0]==deadlines[1]
        rendered=chat.details.render_forward(cycle,[],1,True,None,dict(session_id=row['id'],event_id=1,forward_index=0))
        assert rendered['limit_reason']=='cycle' and not rendered['continuation_supported'] and 'next_call' not in rendered
        large=chat.details.forward(row,dict(nodes=sanitize_nodes([dict(message=[child('edge-large')])]*5)),time.monotonic()+3,threading.Event())
        assert sum(len(v.get('nodes',[])) for v in walk(large))<=50
        assert sum(len(v.get('text','')) for v in walk(large))<=MAX_TEXT
        assert any(v.get('truncated') for v in walk(large))
        bounded=chat.details.forward(row,dict(nodes=sanitize_nodes([dict(message=[child('edge-nodes')])]*5)),time.monotonic()+3,threading.Event())
        assert sum(len(v.get('nodes',[])) for v in walk(bounded))==50
        rendered=chat.details.render_forward(bounded,[],1,True,None,dict(session_id=row['id'],event_id=1,forward_index=0))
        assert rendered['unrecoverable_truncation'] and not rendered['has_more'] and not rendered['continuation_supported'] and 'next_call' not in rendered
        deep=chat.details.forward(row,dict(id='edge-depth-a'),time.monotonic()+3,threading.Event())
        assert 'edge-depth-c' not in queries and any(v.get('state')=='limit_reached' for v in walk(deep))
    with patch.object(chat.actions,'client_factory',side_effect=AssertionError('expired/cancelled nested fetch')):
        assert chat.details.forward(row,{},time.monotonic()+3,threading.Event())['state']=='unavailable'
        # Missing IDs cannot be recovered by reading the same parent again.
        for ref in ({},dict(nodes=[],truncated=True)):
            for nested in (False,True):
                source=dict(nodes=[dict(segments=[dict(type='forward',**ref)])]) if nested else ref
                for _ in range(2):
                    missing=chat.details.forward(row,source,time.monotonic()+3,threading.Event())
                    rendered=chat.details.render_forward(missing,[],1,True,None,dict(session_id=row['id'],event_id=1,forward_index=0))
                    assert rendered['unrecoverable_truncation'] and rendered['limit_reason']=='missing_resource_id'
                    for detail in (rendered,rendered['nodes'][0]['segments'][0]) if nested else (rendered,):
                        assert not detail['continuation_supported'] and 'read_call' not in detail and 'next_call' not in detail
        empty=chat.details.forward(row,dict(nodes=[]),time.monotonic()+3,threading.Event())
        assert empty['state']=='available' and not empty['truncated']
        pending=dict(nodes=sanitize_nodes([dict(message=[child('edge-expired')])]))
        expired=chat.details.forward(row,pending,time.monotonic()-1,threading.Event())
        assert expired['nodes'][0]['segments'][0]['state']=='budget_exhausted'
        cancelled=threading.Event();cancelled.set()
        assert chat.details.forward(row,pending,time.monotonic()+3,cancelled)['state']=='cancelled'
    with patch.object(chat.details,'cached',side_effect=AssertionError('expired child still read cache')):
        assert chat.details.forward(row,dict(id='past-budget'),time.monotonic()-1,threading.Event())['state']=='budget_exhausted'
    mixed=dict(state='available',nodes=[dict(sender_id='444',segments=[
        dict(type='forward',state='unavailable',nodes=[],truncated=True,limit_reason='missing_resource_id',unrecoverable_truncation=True),
        dict(type='forward',state='unavailable',nodes=[],truncated=False)])])
    page=chat.details.render_forward(mixed,[],1,True,None,dict(session_id=row['id'],event_id=1,forward_index=0))
    assert page['unrecoverable_truncation'] and page['read_call'] and page['continuation_supported']
    assert 'read_call' not in page['nodes'][0]['segments'][0] and page['nodes'][0]['segments'][1]['read_call']
    with patch.object(chat.details,'query',side_effect=AssertionError('complete inline content queried')):
        inline=chat.details.forward(row,dict(nodes=sanitize_nodes([dict(message=[dict(type='forward',data=dict(messages=[dict(message=[text('inline')])]))])])),time.monotonic()+3,threading.Event())
        assert inline['nodes'][0]['segments'][0]['nodes'][0]['segments'][0]['text']=='inline'
    print('PASS: nested shared text/node/depth limits, cycles, expired budget/cancel and unlocked queries')


async def wire_materials(service,token,chat,grant,raw):
    """A controlled host checks real MCP content; it does not simulate model requests."""
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},timeout=30,trust_env=False) as http:
        async with streamable_http_client(service.status()['url'],http_client=http) as (read,write,_):
            async with ClientSession(read,write) as client:
                await client.initialize();calls=[]
                async def call(name,**args):
                    calls.append(name);result=await client.call_tool(name,args)
                    assert not result.isError,result
                    return result
                start=await call('start_chat_session',conversation_id='111:group:223',persona='fixture',idempotency_key='materials-wire')
                sid=start.structuredContent['session']['id'];serial=0
                with chat.access.connect() as db:
                    prior_library=db.execute('SELECT count(*) FROM sticker_library WHERE grant_id=?',(grant['id'],)).fetchone()[0]
                def add(segments):
                    nonlocal serial
                    serial+=1;mid=600000+serial
                    chat.receive(dict(post_type='message',message_type='group',self_id=111,group_id=223,message_id=mid,
                        user_id=444,time=int(time.time()),sender={'user_id':444},message=segments))
                    with chat.access.connect() as db:
                        return db.execute('SELECT id FROM chat_inbox WHERE session_id=? AND message_id=?',(sid,str(mid))).fetchone()[0],mid
                def photo(data):return dict(type='image',data={'base64':base64.b64encode(data).decode()})
                async def wait(**args):
                    return await call('wait_chat_messages',session_id=sid,timeout_seconds=1,quiet_seconds=0,**args)
                def check(result,eid,count):
                    data=result.structuredContent.get('updated_chat',result.structuredContent)
                    entries=[e for e in data['materials']['images'] if e['event_id']==eid]
                    assert len(entries)==count,entries
                    for e in entries:
                        assert e['state']=='pixels_delivered' and len(e['content_indexes'])==1,e
                        block=result.content[e['content_indexes'][0]]
                        assert block.type=='image'
                        with Image.open(io.BytesIO(base64.b64decode(block.data))) as im:assert im.size==(64,48)
                    return entries
                async def reply(result,target):
                    data=result.structuredContent.get('updated_chat',result.structuredContent)
                    args=data['input_batch']['plan_call']['arguments'];before=len(calls)
                    await asyncio.sleep(.12)
                    plan=(await call('plan_chat_reply',**args,action='reply',intent='fixture',target_event_id=target,expressions=['text'])).structuredContent
                    assert plan['state']=='READY' and plan['quote_guidance']['threshold']==3,plan
                    sent=await call('send_chat_reply',**plan['next_call']['arguments'],expression_ids=[],bubbles=[dict(kind='text',text='synthetic fixture')])
                    assert sent.structuredContent['state']=='SUCCEEDED',sent
                    assert calls[before:]==['plan_chat_reply','send_chat_reply']
                    return sent.structuredContent['next_call']['arguments']['acknowledge_through_id']
                eid,_=add([text('synthetic text')]);packet=await wait();assert not [c for c in packet.content if c.type=='image']
                cursor=await reply(packet,eid)
                red=io.BytesIO();Image.new('RGB',(64,48),'red').save(red,'PNG')
                eid,_=add([photo(raw),photo(red.getvalue())]);packet=await wait(acknowledge_through_id=cursor)
                entries=check(packet,eid,2)
                colors=[]
                for e in entries:
                    with Image.open(io.BytesIO(base64.b64decode(packet.content[e['content_indexes'][0]].data))) as im:colors.append(im.getpixel((20,20)))
                assert colors[0][2]>200 and colors[1][0]>200,colors
                assert chat.row(sid)['cursor']<eid,'preparation acknowledged unseen input'
                assert 'image_reads' not in packet.structuredContent['chat_guidance']
                before_writes=len(DetailBot.sent)
                restored=await call('get_chat_session',session_id=sid,response_format='compact',known_prompt_version=start.structuredContent['session']['prompt_version'])
                check(restored,eid,2);assert len(DetailBot.sent)==before_writes
                cursor=await reply(packet,eid)
                source,mid=add([photo(raw)])
                parent,_=add([dict(type='reply',data={'id':str(mid)})]);packet=await wait(acknowledge_through_id=cursor)
                check(packet,parent,1);cursor=await reply(packet,parent)
                nodes=[dict(sender={'user_id':444,'nickname':'Author'},message=[text('node '+str(i))]) for i in range(25)]
                nodes[-1]['message'].append(photo(raw))
                eid,_=add([dict(type='forward',data={'messages':nodes})]);packet=await wait(acknowledge_through_id=cursor)
                assert len(packet.structuredContent['messages'][-1]['referenced_messages'][0]['nodes'])==25
                check(packet,eid,1);cursor=await reply(packet,eid)
                eid,_=add([text('old decision')]);packet=await wait(acknowledge_through_id=cursor);await asyncio.sleep(.12)
                plan=(await call('plan_chat_reply',**packet.structuredContent['input_batch']['plan_call']['arguments'],action='reply',intent='fixture',target_event_id=eid,expressions=['text'])).structuredContent
                latest,_=add([text('correction'),photo(raw)])
                with patch.object(chat,'read_context',wraps=chat.read_context) as reads, \
                     patch.object(chat.details,'enrich',wraps=chat.details.enrich) as details, \
                     patch.object(chat.media,'event_image',wraps=chat.media.event_image) as pixels:
                    blocked=await call('send_chat_reply',**plan['next_call']['arguments'],expression_ids=[],bubbles=[dict(kind='text',text='old answer')])
                assert reads.call_count==details.call_count==pixels.call_count==1,'rejected recovery prepared the same input twice'
                assert blocked.structuredContent['error_code']=='stale';check(blocked,latest,1)
                assert blocked.structuredContent['recovery']['next_call']==blocked.structuredContent['updated_chat']['next_call']
                cursor=await reply(blocked,latest)
                eid,_=add([photo(raw),photo(raw)])
                original=chat.media.event_image
                def slow_read(*args,**kwargs):
                    assert not chat.lock._is_owned() and not chat.turns.lock(sid).locked()
                    with chat.access.connect() as db:db.execute('BEGIN IMMEDIATE')
                    result=original(*args,**kwargs);time.sleep(.15);return result
                with patch.object(chat.turns,'policy',return_value=dict(chat.turns.policy(),detail_budget_seconds=.1)),patch.object(chat.media,'event_image',side_effect=slow_read):
                    packet=await wait(acknowledge_through_id=cursor)
                assert [e['state'] for e in packet.structuredContent['materials']['images']]==['pixels_delivered','budget_exhausted']
                assert packet.structuredContent['materials']['incomplete']
                cursor=packet.structuredContent['read_through_id']
                eid,mid=add([photo(raw)])
                cancelled=threading.Event()
                def withdraw(*args,**kwargs):
                    result=original(*args,**kwargs);chat.recall(dict(self_id=111,group_id=223,message_id=mid));return result
                with patch.object(chat.media,'event_image',side_effect=withdraw):packet=await wait(acknowledge_through_id=cursor)
                assert packet.structuredContent['materials']['incomplete'] and not [c for c in packet.content if c.type=='image']
                cursor=packet.structuredContent['read_through_id']
                eid,_=add([photo(b'invalid-image')]);packet=await wait(acknowledge_through_id=cursor)
                assert packet.structuredContent['materials']['incomplete'] and packet.structuredContent['materials']['images'][0]['state']=='unavailable'
                cursor=packet.structuredContent['read_through_id']
                eid,limited_mid=add([photo(raw) for _ in range(9)]);packet=await wait(acknowledge_through_id=cursor)
                assert packet.structuredContent['materials']['incomplete'] and any(e['state']=='image_limit' for e in packet.structuredContent['materials']['images'])
                assert len([c for c in packet.content if c.type=='image'])==8
                quoted_limit,_=add([dict(type='reply',data={'id':str(limited_mid)})])
                packet=await wait(acknowledge_through_id=packet.structuredContent['read_through_id'])
                quoted=packet.structuredContent['messages'][-1]['referenced_messages'][0]
                assert quoted['segments'][-1]['state']=='image_limit' and packet.structuredContent['materials']['incomplete']
                cursor=packet.structuredContent['read_through_id']
                # Media exhaustion is independent of ordinary reads and never removes the text batch.
                with chat.access.connect() as db:
                    db.executemany('INSERT INTO calls(grant_id,tool,at,status) VALUES(?,?,?,?)',[(grant['id'],'read_chat_image',time.time(),'ok')]*60)
                packet=await wait()
                assert packet.structuredContent['messages'] and any(e.get('state')=='rate_limited' for e in packet.structuredContent['materials']['images'])
                with chat.access.connect() as db:db.execute('DELETE FROM calls WHERE grant_id=?',(grant['id'],))
                eid,_=add([dict(type='forward',data={'messages':nodes*3})]);packet=await wait(acknowledge_through_id=cursor)
                assert packet.structuredContent['materials']['incomplete'] and packet.structuredContent['messages'][-1]['referenced_messages'][0]['truncated']
                # Cancel during pixel preparation: no output pixels and no ACK advancement.
                eid,_=add([photo(raw)]);previous=chat.row(sid)['cursor']
                def cancel_read(*args,**kwargs):cancelled.set();return original(*args,**kwargs)
                with patch.object(chat.media,'event_image',side_effect=cancel_read):
                    result=await asyncio.to_thread(service.tools.call,token,'get_chat_session',dict(session_id=sid,response_format='compact'),cancelled)
                assert not [c for c in result.content if c.type=='image'] and chat.row(sid)['cursor']==previous
                with chat.access.connect() as db:
                    assert db.execute('SELECT count(*) FROM sticker_library WHERE grant_id=?',(grant['id'],)).fetchone()[0]==prior_library,'prefetch auto-collected knowledge'
                    original_scope=db.execute('SELECT scope FROM grants WHERE id=?',(grant['id'],)).fetchone()[0]
                    limited=json.loads(original_scope);limited['chat_images']=False
                    db.execute('UPDATE grants SET scope=? WHERE id=?',(json.dumps(limited),grant['id']))
                try:
                    packet=await wait()
                    assert packet.structuredContent['materials']['incomplete'] and not [c for c in packet.content if c.type=='image']
                finally:
                    with chat.access.connect() as db:db.execute('UPDATE grants SET scope=? WHERE id=?',(original_scope,grant['id']))
                await call('stop_chat_session',session_id=sid)
                assert not {'read_chat_image','read_chat_forward','select_chat_expressions'}&set(calls)
                print('PASS: real MCP HTTP automatic text/multi-image/quote/25-node forward pixels, exact content mapping, controlled plan→send, updated pixels, recall/cancel/failure/limits and no auto-collection/ACK')


def text(value):return dict(type='text',data=dict(text=value))


def concurrent_details(chat,grant,row,items,raw):
    """Synchronization proves overlap and merge correctness, without a speed benchmark."""
    entered=[threading.Event(),threading.Event()];release=threading.Event();other=threading.Event()
    results={};errors=[];requests=[]
    def transport(request):
        args=json.loads(request.content);requests.append((request.url.path,args))
        if request.url.path=='/get_login_info':data={'user_id':111}
        else:
            label=threading.current_thread().name
            if label in ('detail-0','detail-1'):
                entered[int(label[-1])].set()
                assert release.wait(5),'test did not release blocked queries'
            else:other.set()
            images=[dict(type='image',data={'base64':base64.b64encode(raw).decode()})]
            images.append(dict(type='image',data={'file':label}))
            data={'messages':[dict(sender={'user_id':444,'nickname':'Author'},message=images)]}
        return httpx.Response(200,json={'status':'ok','retcode':0,'data':data})
    def run(label,session,item):
        try:results[label]=chat.details.enrich(grant,session,[item])
        except BaseException as exc:errors.append(exc)
    factory=lambda:Client({'url':'http://127.0.0.1'},transport=httpx.MockTransport(transport))
    threads=[threading.Thread(name=f'detail-{i}',target=run,args=(i,row,items[0])) for i in range(2)]
    # A separate authorized group uses the same ChatDetails instance and database.
    with patch.object(chat.actions,'client_factory',factory):
        try:
            for thread in threads:thread.start()
            assert all(event.wait(5) for event in entered),'same-parent queries serialized'
            second=threading.Thread(name='other-group',target=run,args=(2,items[1][0],items[1][1]));threads.append(second);second.start()
            assert other.wait(5),'group B could not enter while group A was fetching'
        finally:
            release.set()
            for thread in threads:thread.join(6)
    assert not errors and all(not t.is_alive() for t in threads),errors
    with chat.access.connect() as db:
        media=json.loads(db.execute('SELECT media FROM chat_inbox WHERE id=?',(items[0]['id'],)).fetchone()[0])
        other_media=json.loads(db.execute('SELECT media FROM chat_inbox WHERE id=?',(items[1][1]['id'],)).fetchone()[0])
    assert len(media)==3 and len(other_media)==2,(media,other_media)
    indexes=[results[i][0]['referenced_messages'][0]['nodes'][0]['segments'][0]['image_index'] for i in range(2)]
    assert indexes[0]==indexes[1] and len({json.dumps(m,sort_keys=True) for m in media})==3
    assert all(args.keys()=={'id'} for action,args in requests if action=='/get_forward_msg')
    # A late successful fetch cannot replace a recall tombstone, including after expiry/pruning.
    chat.details.store(row['id'],'reply:race',{'state':'recalled'})
    chat.details.store(row['id'],'reply:race',{'state':'available','segments':[]})
    assert chat.details.cached(row['id'],'reply:race')['state']=='recalled'
    print('PASS: concurrent groups query independently; same-parent image merge is stable, deduplicated and scoped; recall wins late fetch')


class DetailBot(ReactionBot):
    queries=[]
    image={}
    forwards={}

    def do_POST(self):
        if self.path not in ('/get_forward_msg','/get_image'):return super().do_POST()
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.queries.append((self.path,body))
        data=self.image if self.path=='/get_image' else self.forwards.get(body.get('message_id') or body.get('id'))
        raw=json.dumps(dict(status='ok',retcode=0,data=data)).encode()
        self.send_response(200);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)


def main():
    from chatlocal.mcp_chat_details import sanitize_nodes,MAX_TEXT
    from chatlocal.mcp_chat_materials import incomplete
    for count in (50,51):
        nodes=sanitize_nodes([dict(message=[text('x')]) for _ in range(count)])
        assert len(nodes)==50 and incomplete(dict(nodes=nodes))==(count>50)
    for count in (MAX_TEXT,MAX_TEXT+1):
        nodes=sanitize_nodes([dict(message=[text('x'*count)])])
        assert incomplete(dict(nodes=nodes))==(count>MAX_TEXT)
    nested=sanitize_nodes([dict(message=[dict(type='forward',data={'messages':[dict(message=[text('x')]) for _ in range(49)]})])])
    assert not incomplete(dict(nodes=nested)),'exact nested node budget was incorrectly marked incomplete'
    # Windows OpenSSL needs its first initialization before the fixture clears env.
    ssl.create_default_context()
    events=Events();DetailBot.sent=[];DetailBot.writes=[];DetailBot.history={};DetailBot.intercept=None
    upstream=ThreadingHTTPServer(('127.0.0.1',0),DetailBot)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    out=io.BytesIO();Image.new('RGB',(64,48),'blue').save(out,'PNG');raw=out.getvalue()
    try:
        with tempfile.TemporaryDirectory(prefix='tulpa-enrichment-') as tmp,patch.dict('os.environ',isolated_env(),clear=True):
            folder=Path(tmp);store=Store(folder/'chats.sqlite3')
            (folder/'.env').write_text(f'REPLY_ONEBOT_URL=http://127.0.0.1:{upstream.server_port}\nREPLY_ONEBOT_WS_URL=ws://127.0.0.1:{events.port}\nREPLY_ONEBOT_WS_TOKEN=event-fixture\n','utf-8')
            cache=folder/'trusted';cache.mkdir()
            (folder/'mcp-chat-media-policy.json').write_text(json.dumps(dict(local_roots={'111':[str(cache)]})),'utf-8')
            (folder/'mcp-chat-policy.json').write_text(json.dumps(dict(quiet_seconds=.1,reply_cooldown_seconds=0)),'utf-8')
            with patch('chatlocal.onebot.ROOT',folder),patch('chatlocal.message_sender.ROOT',folder):
                app=FastAPI();service=install_mcp_routes(app,store);access=service.access;tools=service.tools
                with TestClient(app):
                    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                    access.configure(True,port);service.start()
                    g=access.create(dict(name='enrichment',platforms=['qq'],conversations=[['qq','111:group:222'],['qq','111:group:223']],send=True,chat=True,chat_images=True))
                    grant=access.authorize(g['token'],source=False)
                    serial=0
                    def call(name,**args):
                        r=tools.call(g['token'],name,args,threading.Event())
                        assert not r.isError,(name,r)
                        return r.structuredContent
                    start=call('start_chat_session',conversation_id='111:group:222',persona='简短自然聊天',idempotency_key='enrichment-start')
                    sid=start['session']['id'];eventually(lambda:tools.chat.receiver.status()['state']=='connected')
                    def add(segments=None,uid=333,**extra):
                        nonlocal serial
                        serial+=1;mid=90000+serial
                        event=dict(post_type='message',message_type='group',self_id=111,group_id=222,message_id=mid,
                            message_seq=mid,user_id=uid,sender={'user_id':uid,'nickname':'Fixture'},time=time.time(),message=segments or [text('fixture')])|extra
                        DetailBot.history[str(mid)]=deepcopy(event);tools.chat.receive(event)
                        with access.connect() as db:eid=db.execute('SELECT id FROM chat_inbox WHERE session_id=? AND message_id=?',(sid,str(mid))).fetchone()[0]
                        return eid,mid
                    def packet():return call('get_chat_session',session_id=sid)
                    def message(p,eid):return next(m for m in p['context']['messages'] if m['id']==eid)
                    def quote_plan(target):
                        p=packet();time.sleep(.12)
                        return call('plan_chat_reply',session_id=sid,batch_id=p['input_batch']['id'],target_event_id=target,
                            expressions=['text'],intent='fixture reply',action='reply')

                    target,_=add()
                    assert quote_plan(target)['quote_guidance']['messages_after_target']==0
                    add(uid=111);add(virtual=True);add();add()
                    q=quote_plan(target)['quote_guidance'];assert q['messages_after_target']==2 and not q['quote_recommended'],q
                    last,mid=add();tools.chat.receive(deepcopy(DetailBot.history[str(mid)]))
                    q=quote_plan(target)['quote_guidance'];assert q['messages_after_target']==3 and q['quote_recommended'] and q['threshold']==3,q
                    tools.chat.recall(dict(self_id=111,group_id=222,message_id=mid))
                    assert quote_plan(target)['quote_guidance']['messages_after_target']==2
                    print('PASS: quote threshold 0/2/3, self/system/recall/duplicate exclusions; suggestion in existing plan result')

                    quoted,mid=add([text('original wording')]);cached_parent,_=add([dict(type='reply',data={'id':str(mid)}),text('agree')])
                    with patch.object(tools.chat.details,'query',side_effect=AssertionError('cached quote queried OneBot')):
                        d=message(packet(),cached_parent)['referenced_messages'][0]
                    assert d['source']=='inbox' and d['content']=='original wording',d
                    remote=81001
                    DetailBot.history[str(remote)]=dict(message_id=remote,group_id=222,message_type='group',user_id=444,time=time.time(),sender={'nickname':'Remote'},message=[text('remote original')])
                    parent,_=add([dict(type='reply',data={'id':str(remote)})])
                    assert message(packet(),parent)['referenced_messages'][0]['content']=='remote original'
                    with patch.object(tools.chat.details,'query',side_effect=AssertionError('remote quote cache missed')):packet()
                    foreign=81002;DetailBot.history[str(foreign)]={**DetailBot.history[str(remote)],'message_id':foreign,'group_id':999}
                    bad,_=add([dict(type='reply',data={'id':str(foreign)})])
                    assert message(packet(),bad)['referenced_messages'][0]['state']=='unverified'
                    tools.chat.recall(dict(self_id=111,group_id=222,message_id=mid))
                    assert message(packet(),cached_parent)['referenced_messages'][0]['state']=='recalled'
                    tools.chat.recall(dict(self_id=111,group_id=222,message_id=remote))
                    tools.chat.recall(dict(self_id=111,group_id=222,message_id=foreign))
                    assert message(packet(),parent)['referenced_messages'][0]['state']=='recalled'
                    print('PASS: local/remote quote originals, scoped identity check and cache reuse')

                    nodes=[dict(sender={'user_id':444,'nickname':'ForwardAuthor'},time=1,content=[text(f'node {i}')]) for i in range(25)]
                    # One image in the merge is scoped to its received parent event.
                    nodes[0]['content'].append(dict(type='image',data={'base64':base64.b64encode(raw).decode()}))
                    nodes[20]['content'].append(dict(type='image',data={'base64':base64.b64encode(raw).decode(),'file':'second-page-fixture'}))
                    forward,_=add([dict(type='forward',data={'messages':nodes})])
                    with patch('chatlocal.mcp_chat_media.download',side_effect=AssertionError('unexpected remote download')):
                        d=message(packet(),forward)['referenced_messages'][0]
                        assert len(d['nodes'])==25 and not d['has_more'] and d['nodes'][0]['sender']=='ForwardAuthor'
                        more=call('read_chat_forward',session_id=sid,event_id=forward,offset=20)
                        assert len(more['nodes'])==5 and not more['has_more'] and more['nodes'][0]['content']=='node 20[图片]'
                        image_index=d['nodes'][0]['segments'][1]['image_index']
                        assert call('read_chat_image',session_id=sid,event_id=forward,index=image_index)['dimensions']==[64,48]
                        asyncio.run(wire(service,g['token'],sid,forward))
                        asyncio.run(wire_pages(service,g['token'],sid,forward,tools.chat,grant))
                    DetailBot.forwards['bundle']={'messages':nodes}
                    remote_fwd,_=add([dict(type='forward',data={'id':'bundle'})])
                    assert message(packet(),remote_fwd)['referenced_messages'][0]['total_nodes']==25
                    count=len(DetailBot.queries);packet();assert len(DetailBot.queries)==count
                    failed,_=add([dict(type='forward',data={'id':'missing'})])
                    assert message(packet(),failed)['referenced_messages'][0]['state']=='unavailable'
                    assert tools.call(g['token'],'read_chat_forward',dict(session_id=sid,event_id=999999),threading.Event()).isError
                    from chatlocal.mcp_chat_details import sanitize_nodes
                    nested={'messages':[dict(content=[dict(type='forward',data={'id':'opaque nested'})])]}
                    assert sanitize_nodes(nested)[0]['segments'][0]['truncated']
                    print('PASS: inline/remote forward, sender/order, paging, parent-scoped image pixels, cache and unavailable states')

                    # Local and remote quotes reuse the same forward/rendering
                    # path; the returned parent-relative positions are executable.
                    with access.connect() as db:local_mid=db.execute('SELECT message_id FROM chat_inbox WHERE id=?',(forward,)).fetchone()[0]
                    quoted_forward,_=add([dict(type='reply',data={'id':local_mid})])
                    quoted=message(packet(),quoted_forward)['referenced_messages'][0]['segments'][0]
                    assert len(quoted['nodes'])==25 and quoted['reply_index']==0 and quoted['forward_index']==0
                    asyncio.run(wire_quote(service,g['token'],sid,quoted_forward,quoted))
                    quote_mid=82001
                    DetailBot.history[str(quote_mid)]=dict(message_id=quote_mid,group_id=222,message_type='group',self_id=111,
                        user_id=444,time=time.time(),sender={'nickname':'Remote'},message=[text('quoted lead'),dict(type='forward',data={'id':'bundle'})])
                    remote_quote,_=add([dict(type='reply',data={'id':str(quote_mid)})])
                    unread=tools.call(g['token'],'read_chat_forward',dict(session_id=sid,event_id=remote_quote,reply_index=0,forward_index=1),threading.Event())
                    assert unread.isError,'unoffered quote was readable'
                    quoted=message(packet(),remote_quote)['referenced_messages'][0]['segments'][1]
                    assert quoted['forward_index']==1 and len(quoted['nodes'])==25
                    asyncio.run(wire_quote(service,g['token'],sid,remote_quote,quoted))
                    count=len(DetailBot.queries);packet()
                    assert not any(body.get('id')=='bundle' for _,body in DetailBot.queries[count:]),'forward cache missed'
                    with access.connect() as db:
                        assert tools.chat.row(sid)['cursor']==0,'quoted material advanced confirmation'
                    tools.chat.recall(dict(self_id=111,group_id=222,message_id=quote_mid))
                    recalled=call('read_chat_forward',session_id=sid,event_id=remote_quote,reply_index=0,forward_index=1)
                    assert recalled['state']=='recalled' and not recalled['nodes']
                    unavailable_mid=82002
                    DetailBot.history[str(unavailable_mid)]=dict(DetailBot.history[str(quote_mid)],message_id=unavailable_mid,
                        message=[text('lead'),dict(type='forward',data={'id':'later-available'})])
                    retry_parent,_=add([dict(type='reply',data={'id':str(unavailable_mid)})])
                    missing=message(packet(),retry_parent)['referenced_messages'][0]['segments'][1]
                    assert missing['state']=='unavailable' and missing['read_call']['arguments']['forward_index']==1
                    DetailBot.forwards['later-available']={'messages':nodes}
                    link=missing['read_call'];retried=call(link['tool'],**link['arguments'])
                    assert retried['state']=='available' and len(retried['nodes'])==25 and not retried['has_more']
                    assert 'next_call' not in retried
                    for source_mid,changes,expected in ((82003,dict(self_id=999),'unverified'),
                                                        (82004,dict(time=time.time()-172800),'outside_scope')):
                        DetailBot.history[str(source_mid)]=dict(DetailBot.history[str(quote_mid)],message_id=source_mid,**changes)
                        denied_parent,_=add([dict(type='reply',data={'id':str(source_mid)})])
                        with access.connect() as db:
                            saved=db.execute('SELECT scope FROM grants WHERE id=?',(grant['id'],)).fetchone()[0]
                            scoped=json.loads(saved);scoped['start']=time.strftime('%Y-%m-%d')
                            db.execute('UPDATE grants SET scope=? WHERE id=?',(json.dumps(scoped),grant['id']))
                        try:
                            packet();denied=call('read_chat_forward',session_id=sid,event_id=denied_parent,reply_index=0,forward_index=1)
                            assert denied['state']==expected and not denied['nodes'],denied
                        finally:
                            with access.connect() as db:db.execute('UPDATE grants SET scope=? WHERE id=?',(saved,grant['id']))
                    with access.connect() as db:
                        scoped=json.loads(saved);scoped['chat_images']=False
                        db.execute('UPDATE grants SET scope=? WHERE id=?',(json.dumps(scoped),grant['id']))
                    try:
                        limited=call('read_chat_forward',session_id=sid,event_id=retry_parent,reply_index=0,forward_index=1)
                        image=limited['nodes'][0]['segments'][1]
                        assert not image['readable'] and image['state']=='permission_denied' and 'image_index' not in image
                    finally:
                        with access.connect() as db:db.execute('UPDATE grants SET scope=? WHERE id=?',(saved,grant['id']))
                    # Hard limits never advertise an endless continuation past
                    # the retained content; supported inline nested content stays.
                    oversized,_=add([dict(type='forward',data={'messages':nodes*3})])
                    packet();page=call('read_chat_forward',session_id=sid,event_id=oversized)
                    assert len(page['nodes'])==50 and not page['has_more']
                    small=call('read_chat_forward',session_id=sid,event_id=oversized,limit=20)
                    assert small['unrecoverable_truncation'] and small['has_more'] and small['next_call']['arguments']['limit']==20
                    retained=[]
                    while True:
                        retained.extend(page['nodes'])
                        assert page['unrecoverable_truncation'] and page['limit_reason']=='processing_limit'
                        if not page['has_more']:break
                        link=page['next_call'];page=call(link['tool'],**link['arguments'])
                    assert len(retained)==50 and 'next_call' not in page and not page['continuation_supported']
                    nested_event,_=add([dict(type='forward',data={'messages':[dict(content=[dict(type='forward',data={'messages':nodes[:2]})])]})])
                    inner=message(packet(),nested_event)['referenced_messages'][0]['nodes'][0]['segments'][0]
                    assert len(inner['nodes'])==2 and inner['state']=='available' and not inner['truncated']
                    opaque,_=add([dict(type='forward',data=nested)])
                    inner=message(packet(),opaque)['referenced_messages'][0]['nodes'][0]['segments'][0]
                    assert inner['state']=='unavailable' and inner['continuation_supported'] and inner['read_call']['tool']=='read_chat_forward'
                    assert 'id' not in inner and 'next_call' not in inner
                    print('PASS: quoted local/remote forward paging, parent pixels, recall, cached resolution, hard-limit continuation and inline nesting')
                    # The added pixel cases have their own quota assertions;
                    # leave the original local-source fixtures a fresh budget.
                    with access.connect() as db:db.execute("DELETE FROM calls WHERE tool IN ('read_chat_image','read_chat_sticker')")

                    # Use the actual Client contract, with controlled read-only transport failures.
                    row=tools.chat.row(sid);cancel=threading.Event()
                    nested_limits(tools.chat,row)
                    for failure in ('timeout','wrong-account','empty'):
                        queries=[]
                        def fail(request):
                            queries.append(request.url.path)
                            if failure=='timeout':raise httpx.ReadTimeout('fixture')
                            data={'user_id':999 if failure=='wrong-account' else 111} if request.url.path=='/get_login_info' else None
                            return httpx.Response(200,json={'status':'ok','retcode':0,'data':data})
                        with patch.object(tools.chat.actions,'client_factory',lambda:Client({'url':'http://127.0.0.1'},transport=httpx.MockTransport(fail))):
                            assert tools.chat.details.forward(row,{'id':'failure-'+failure},time.monotonic()+3,cancel)['state']=='unavailable'
                        assert queries.count('/get_forward_msg')==(1 if failure=='empty' else 0),queries
                    with patch.object(tools.chat.actions,'client_factory',side_effect=AssertionError('expired/cancelled request')):
                        assert tools.chat.details.query(row,'get_msg',{},time.monotonic()-1,cancel) is None
                        assert tools.chat.details.forward(row,{'id':'expired'},time.monotonic()-1,cancel)['state']=='budget_exhausted'
                        cancel.set();assert tools.chat.details.query(row,'get_msg',{},time.monotonic()+3,cancel) is None
                    budget_event,_=add([dict(type='forward',data={'id':'budget-a'}),dict(type='forward',data={'id':'budget-b'})])
                    with access.connect() as db:
                        budget_row=db.execute('SELECT * FROM chat_inbox WHERE id=?',(budget_event,)).fetchone()
                    deadlines=[]
                    def budget_query(row,action,args,deadline,cancel):
                        deadlines.append(deadline);return None
                    with patch.object(tools.chat.details,'query',side_effect=budget_query):
                        tools.chat.details.enrich(grant,row,tools.chat.messages([budget_row]))
                    assert len(deadlines)==2 and deadlines[0]==deadlines[1]
                    print('PASS: timeout, account mismatch and empty response never retry parameter aliases; deadline/cancellation stop I/O')

                    parallel,_=add([dict(type='forward',data={'id':'parallel'})])
                    second=call('start_chat_session',conversation_id='111:group:223',persona='fixture',idempotency_key='parallel-group')['session']['id']
                    assert tools.call(g['token'],'read_chat_forward',dict(session_id=second,event_id=quoted_forward,reply_index=0,forward_index=0),threading.Event()).isError
                    tools.chat.receive(dict(post_type='message',message_type='group',self_id=111,group_id=223,message_id=777777,
                        user_id=444,time=time.time(),message=[dict(type='forward',data={'id':'other-group'})]))
                    with access.connect() as db:
                        a=db.execute('SELECT * FROM chat_inbox WHERE id=?',(parallel,)).fetchone()
                        b=db.execute('SELECT * FROM chat_inbox WHERE session_id=?',(second,)).fetchone()
                    concurrent_details(tools.chat,grant,row,[tools.chat.messages([a])[0],(tools.chat.row(second),tools.chat.messages([b])[0])],raw)
                    call('stop_chat_session',session_id=second)

                    # The old persisted format remains readable; only obsolete successful quote caches expire.
                    legacy,_=add([dict(type='forward',data={'id':'legacy'})])
                    old_nodes=[dict(sender_id='444',sender='OldAuthor',timestamp=1,segments=[
                        dict(type='text',data={'text':'old cache'}),dict(type='image',data={'base64':base64.b64encode(raw).decode()})])]
                    with access.connect() as db:
                        db.execute('UPDATE chat_inbox SET details=? WHERE id=?',(json.dumps([dict(type='forward',nodes=old_nodes)]),legacy))
                    old=message(packet(),legacy)['referenced_messages'][0]
                    assert old['nodes'][0]['content']=='old cache[图片]' and old['nodes'][0]['sender']=='OldAuthor'
                    assert call('read_chat_image',session_id=sid,event_id=legacy,index=old['nodes'][0]['segments'][1]['image_index'])['dimensions']==[64,48]
                    tools.chat.details.store(sid,'reply:old-format',dict(state='available',raw_segments=[]))
                    from chatlocal.mcp_chat_details import ChatDetails
                    restored=ChatDetails(tools.chat)
                    assert restored.cached(sid,'reply:old-format') is None and restored.cached(sid,'reply:race')['state']=='recalled'
                    quoted_image,image_mid=add([dict(type='image',data={'base64':base64.b64encode(raw).decode()})])
                    quoted_parent,_=add([dict(type='reply',data={'id':str(image_mid)})])
                    detail=message(packet(),quoted_parent)['referenced_messages'][0]
                    assert call('read_chat_image',session_id=sid,event_id=quoted_parent,index=detail['segments'][0]['image_index'])['dimensions']==[64,48]
                    tools.chat.recall(dict(self_id=111,group_id=222,message_id=image_mid))
                    assert tools.call(g['token'],'read_chat_image',dict(session_id=sid,event_id=quoted_parent,index=detail['segments'][0]['image_index']),threading.Event()).isError
                    limited=[];restored.render([dict(type='image',image={'file':str(i)}) for i in range(10)],limited,1)
                    assert len(limited)==8
                    print('PASS: persisted forward nodes and recall markers survive format cleanup; quoted image scope/recall and eight-image limit preserved')

                    native=cache/'Pic'/time.strftime('%Y-%m')/'Ori';native.mkdir(parents=True)
                    digest=hashlib.md5(raw).hexdigest();(native/(digest+'.png')).write_bytes(raw)
                    local,_=add([dict(type='image',data={'file':digest.upper()+'.png','url':'https://gchat.qpic.cn/not-needed'})])
                    with patch('chatlocal.mcp_chat_media.download',side_effect=AssertionError('local cache contacted remote')),patch.object(tools.chat.media,'client',side_effect=AssertionError('local hit queried adapter')):
                        assert call('read_chat_image',session_id=sid,event_id=local)['dimensions']==[64,48]
                    assert read_path(str(folder/'chats.sqlite3'),[cache.resolve()]) is None
                    shared=folder/'shared-cache';thumb=shared/'Pic'/time.strftime('%Y-%m')/'Thumb';thumb.mkdir(parents=True)
                    shared_hash='a'*32;(thumb/(shared_hash+'_0.png')).write_bytes(raw)
                    (folder/'mcp-chat-media-policy.json').write_text(json.dumps(dict(local_roots={'111':[str(cache)]},hash_cache_roots={'111':[str(shared)]})),'utf-8')
                    thumbnail,_=add([dict(type='image',data={'file':shared_hash+'.png','url':'https://gchat.qpic.cn/not-needed'})])
                    with patch('chatlocal.mcp_chat_media.download',side_effect=AssertionError('hash cache contacted remote')):
                        first=call('read_chat_image',session_id=sid,event_id=thumbnail)
                        again=call('read_chat_image',session_id=sid,event_id=thumbnail)
                        assert first['thumbnail'] and again['thumbnail']
                    from chatlocal.mcp_chat_local_media import roots, reference_bytes
                    assert reference_bytes({'file':str(thumb/(shared_hash+'_0.png'))},roots(folder,'111')) is None
                    policy=folder/'mcp-chat-media-policy.json';saved=policy.read_bytes()
                    try:
                        for invalid in ([],{'local_roots':[]},{'local_roots':{'111':'private-path'}},{'local_roots':{'111':[42]}}):
                            policy.write_text(json.dumps(invalid),'utf-8')
                            with warnings.catch_warnings(record=True) as notices:
                                warnings.simplefilter('always');roots(folder,'111')
                            assert len(notices)==1 and 'private-path' not in str(notices[0].message)
                        policy.unlink()
                        with warnings.catch_warnings(record=True) as notices:
                            roots(folder,'111')
                        assert not notices
                    finally:policy.write_bytes(saved)
                    local_path=cache/'adapter.png';local_path.write_bytes(raw);DetailBot.image={'file':str(local_path)}
                    adapter,_=add([dict(type='image',data={'file':'adapter-id','url':'https://gchat.qpic.cn/not-needed'})])
                    with patch('chatlocal.mcp_chat_media.download',side_effect=AssertionError('adapter local bytes contacted remote')):
                        assert call('read_chat_image',session_id=sid,event_id=adapter)['dimensions']==[64,48]
                    DetailBot.image={'file':str(folder/'chats.sqlite3')}
                    fallback,_=add([dict(type='image',data={'file':'bad-path','url':'https://gchat.qpic.cn/fallback'})])
                    with patch('chatlocal.mcp_chat_media.download',return_value=raw) as download:
                        call('read_chat_image',session_id=sid,event_id=fallback);assert download.call_count==1
                    print('PASS: real local QQ layout, adapter local bytes, forbidden path rejection and network fallback')

                    version=packet()['session']['prompt_version']
                    compact=call('get_chat_session',session_id=sid,response_format='compact',known_prompt_version=version)
                    assert not compact['prompt_changed'] and 'persona' not in compact['session'] and 'instructions' not in compact
                    assert compact['context']['messages'] and compact['input_batch']['plan_call'] and 'recent_operations' in compact
                    refresh=call('get_chat_session',session_id=sid,response_format='compact',known_prompt_version='old')
                    assert refresh['prompt_changed'] and refresh['chat_prompt']['persona']=='简短自然聊天' and 'behavior' in refresh['chat_prompt']
                    with patch.object(tools.chat.details,'query',return_value=None):
                        for _ in range(55):add()
                        paged=call('get_chat_session',session_id=sid,response_format='compact',known_prompt_version=version)
                    assert paged['context']['has_more'] and paged['next_call']['tool']=='wait_chat_messages'
                    assert paged['next_call']['arguments']['acknowledge_through_id']==paged['context']['messages'][-1]['id']
                    assert not DetailBot.sent,'Real reply not needed for enrichment acceptance'
                    # This separate HTTP scenario has its own media allowance, after the many legacy image regressions above.
                    with access.connect() as db:db.execute('DELETE FROM calls WHERE grant_id=?',(grant['id'],))
                    asyncio.run(wire_page_guards(service,raw))
                    asyncio.run(wire_nested(service,g['token'],raw))
                    asyncio.run(wire_materials(service,g['token'],tools.chat,grant,raw))
                    call('stop_chat_session',session_id=sid)
                    print('PASS: compact recovery, full prompt refresh, preserved cursors/audit and pending pagination; no real QQ writes')
    finally:upstream.shutdown();upstream.server_close();events.close()


if __name__=='__main__':main()
