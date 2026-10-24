"""Bounded MCP replay with real Tulpa code and loopback-only fake OneBot.

Private input snapshots are supplied by the operator. No production database,
credential or QQ transport is opened. Stop by creating <output>/STOP.
"""
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import time
from copy import deepcopy
from http.server import ThreadingHTTPServer
from unittest.mock import patch

ssl.create_default_context()  # Initialize Windows CA paths before env isolation.
sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_mcp_reactions import ReactionBot, Events, eventually
from check_mcp_language_learning import ground_fixture_rows
from chatlocal.store import Store
from chatlocal.mcp_routes import install_mcp_routes
from chatlocal import reply_timing
from fastapi import FastAPI
from fastapi.testclient import TestClient


def main():
    out=Path(sys.argv[1]).resolve()
    source=json.loads((out/'source-wait.json').read_text('utf-8'))
    source_plan=json.loads((out/'source-plan.json').read_text('utf-8'))
    prompt=json.loads((out/'source-prompt.json').read_text('utf-8'))
    reply_timing.PATH=out/'reply-timing.jsonl'
    events=Events();ReactionBot.sent=[];ReactionBot.writes=[];ReactionBot.history={};ReactionBot.intercept=None
    server=ThreadingHTTPServer(('127.0.0.1',0),ReactionBot)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    ports={server.server_port,events.port}
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    ports.add(port)
    original_connect=socket.create_connection
    def restricted_connect(address,*args,**kwargs):
        if not (address[0] in ('127.0.0.1','localhost','::1') and address[1] in ports):
            raise RuntimeError('Replay network boundary: only fixture ports are allowed')
        return original_connect(address,*args,**kwargs)
    try:
        with tempfile.TemporaryDirectory(prefix='tulpa-ag-replay-') as tmp,patch.dict(os.environ,{},clear=True),patch('socket.create_connection',restricted_connect):
            root=Path(tmp);store=Store(root/'chats.sqlite3')
            (root/'.env').write_text(f'REPLY_ONEBOT_URL=http://127.0.0.1:{server.server_port}\nREPLY_ONEBOT_WS_URL=ws://127.0.0.1:{events.port}\nREPLY_ONEBOT_WS_TOKEN=event-fixture\n','utf-8')
            with patch('chatlocal.onebot.ROOT',root),patch('chatlocal.message_sender.ROOT',root):
                service=install_mcp_routes(app:=FastAPI(),store);access=service.access
                with TestClient(app):
                    access.configure(True,port);service.start()
                    grant=access.create(dict(name='antigravity-isolated-replay',platforms=['qq'],conversations=[['qq','111:group:222']],chat=True,send=True,chat_reactions=True))
                    (access.path.parent/'mcp-chat-policy.json').write_text('{"quiet_seconds":0.1,"reply_cooldown_seconds":0}','utf-8')
                    call=lambda name,args:service.tools.call(grant['token'],name,args,threading.Event()).structuredContent
                    started=call('start_chat_session',dict(conversation_id='111:group:222',persona=prompt['persona'],idempotency_key='isolated-replay-start'))
                    sid=started['session']['id'];learning=service.tools.chat.learning;scope='qq:111:group:222'
                    candidates=source_plan['learned_language'].get('candidates',[])
                    with learning.store.db() as db:
                        for i in range(max(10,len(candidates))):
                            c=candidates[i%len(candidates)] if candidates else dict(situation='闲聊',style='简短自然')
                            eid=c['id'] if i<len(candidates) else f'{10000+i:032x}'
                            db.execute('INSERT INTO expressions(id,scope,situation,style,count,updated,independence) VALUES(?,?,?,?,2,?,?)',
                                (eid,scope,c['situation'] if i<len(candidates) else f'fixture-only padding {i}',c['style'],time.time(),'degraded'))
                    ground_fixture_rows(learning.store,scope)
                    original_context=learning.store.context
                    def context(*args,**kwargs):
                        kwargs['retriever']=lambda *_:[c['id'] for c in candidates]
                        return original_context(*args,**kwargs)
                    learning.store.context=context
                    for i,m in enumerate(source['messages']):
                        event=dict(post_type='message',message_type='group',self_id=111,group_id=222,user_id=111 if m.get('is_self') else 333,
                            message_id=-990000-i,message_seq=990000+i,time=int(time.time()),sender={'user_id':333,'nickname':m.get('sender','回放群友')},
                            message=[dict(type='text',data={'text':m['content']})])
                        ReactionBot.history[str(event['message_id'])]=deepcopy(event)
                        service.tools.chat.receive(event)
                    eventually(lambda:service.tools.chat.receiver.status()['state']=='connected')
                    ready=dict(url=service.status()['url'],token=grant['token'],session_id=sid,chat_prompt=started['chat_prompt'],
                        source_step=899,source_message_count=len(source['messages']),candidate_count=len(candidates),
                        safety='Only loopback fixture ports; separate temporary stores; fake QQ sender')
                    (out/'ready.json').write_text(json.dumps(ready,ensure_ascii=False),encoding='utf-8')
                    print(json.dumps(dict(ready=True,port=port,source_message_count=len(source['messages'])),ensure_ascii=False),flush=True)
                    until=time.monotonic()+900
                    heartbeat=0
                    while time.monotonic()<until and not (out/'STOP').exists():
                        if time.monotonic()>=heartbeat:
                            events.send(dict(post_type='meta_event',meta_event_type='heartbeat',self_id=111,time=int(time.time()),interval=5000,status=dict(online=True,good=True)))
                            heartbeat=time.monotonic()+5
                        with access.connect() as db:
                            calls=[dict(r) for r in db.execute('SELECT id,tool,at,status,seconds FROM calls ORDER BY id')]
                            plans=[dict(r) for r in db.execute('SELECT id,state,created,updated,payload,queue FROM chat_turn_plans ORDER BY created')]
                        report=dict(real_qq_writes=0,fake_text_writes=deepcopy(ReactionBot.sent),fake_reaction_writes=deepcopy(ReactionBot.writes),calls=calls,plans=plans)
                        pending=out/'result.pending.json'
                        pending.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                        pending.replace(out/'result.json')
                        time.sleep(.2)
    finally:server.shutdown();events.close()


if __name__=='__main__':main()
