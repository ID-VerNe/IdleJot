"""Opt-in real Codex CLI model check; synthetic MCP/QQ, no production configuration.

The input is fetched through real MCP HTTP before invoking the CLI. ImageContent
is supplied through the CLI's native image input, not a textual image answer.
This measures that CLI path only, not the Codex desktop's MCP rendering.
"""
import asyncio
import faulthandler
import base64
from copy import deepcopy
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_mcp_reactions import ReactionBot, Events, eventually
from chatlocal.mcp_routes import install_mcp_routes
from chatlocal.store import Store
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image, ImageDraw


async def initial(service,token,sid):
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},trust_env=False) as http:
        async with streamable_http_client(service.status()['url'],http_client=http) as (read,write,_):
            async with ClientSession(read,write) as client:
                await client.initialize()
                return await client.call_tool('wait_chat_messages',dict(session_id=sid,timeout_seconds=1,quiet_seconds=0))


def toml(value):
    if isinstance(value,dict):return '{'+','.join(json.dumps(k)+'='+toml(v) for k,v in value.items())+'}'
    if isinstance(value,list):return '['+','.join(toml(v) for v in value)+']'
    return json.dumps(value,ensure_ascii=False)


def generations(directory):
    # A completed API response emits token_count with cumulative token usage.
    # Deduplicate repeated usage notifications; never infer this from tool count.
    counts=[];tools=[];evidence=[]
    for path in directory.rglob('rollout-*.jsonl'):
        usage=set()
        for line in path.read_text('utf-8').splitlines():
            item=json.loads(line);payload=item.get('payload',{})
            if item.get('type')=='event_msg' and payload.get('type')=='token_count' and payload.get('info'):
                total=payload['info'].get('total_token_usage')
                if total:
                    key=json.dumps(total,sort_keys=True)
                    if key not in usage:evidence.append(dict(timestamp=item.get('timestamp'),event='completed_response_usage',total_token_usage=total))
                    usage.add(key)
            if item.get('type')=='response_item' and payload.get('type') in ('function_call','custom_tool_call'):
                tools.append(payload.get('name',''))
                args=payload.get('arguments',payload.get('input',''))
                if not isinstance(args,str):args=json.dumps(args)
                evidence.append(dict(timestamp=item.get('timestamp'),event='tool_call',tool=payload.get('name',''),
                    referenced_tools=[name for name in ('plan_chat_reply','send_chat_reply','read_chat_image','read_chat_forward','get_chat_session','select_chat_expressions') if name in args]))
        counts.append(len(usage))
    return sum(counts) if counts and all(counts) else None,tools,evidence


def main():
    if len(sys.argv)!=2:raise SystemExit('Usage: check_mcp_chat_materials_clients.py <private-output-directory>')
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    faulthandler.dump_traceback_later(60)
    print('fixture: initializing',flush=True)
    ssl.create_default_context();events=Events();ReactionBot.sent=[];ReactionBot.writes=[];ReactionBot.history={};ReactionBot.intercept=None
    upstream=ThreadingHTTPServer(('127.0.0.1',0),ReactionBot)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    source_home=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
    config=tomllib.loads((source_home/'config.toml').read_text('utf-8'))
    print('fixture: fake OneBot and source config ready',flush=True)
    summary=[]
    try:
        with tempfile.TemporaryDirectory(prefix='tulpa-materials-clients-') as tmp:
            root=Path(tmp);store=Store(root/'chats.sqlite3')
            (root/'.env').write_text(f'REPLY_ONEBOT_URL=http://127.0.0.1:{upstream.server_port}\nREPLY_ONEBOT_WS_URL=ws://127.0.0.1:{events.port}\nREPLY_ONEBOT_WS_TOKEN=event-fixture\n','utf-8')
            with patch('chatlocal.onebot.ROOT',root),patch('chatlocal.message_sender.ROOT',root),patch.dict(os.environ,{
                'REPLY_ONEBOT_URL':f'http://127.0.0.1:{upstream.server_port}','REPLY_ONEBOT_TOKEN':'',
                'REPLY_ONEBOT_WS_URL':f'ws://127.0.0.1:{events.port}','REPLY_ONEBOT_WS_TOKEN':'event-fixture'}):
                app=FastAPI();service=install_mcp_routes(app,store);access=service.access;chat=service.tools.chat
                with TestClient(app) as ui:
                    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                    access.configure(True,port);service.start()
                    print('fixture: MCP ready',flush=True)
                    grant=access.create(dict(name='isolated-materials-clients',platforms=['qq'],conversations=[['qq','111:group:222']],chat=True,send=True,chat_images=True))
                    (root/'mcp-chat-policy.json').write_text('{"quiet_seconds":0.1,"reply_cooldown_seconds":0}','utf-8')
                    call=lambda name,args:service.tools.call(grant['token'],name,args,threading.Event()).structuredContent
                    for number,scenario in enumerate(('text','image','correction')):
                        started=call('start_chat_session',dict(conversation_id='111:group:222',persona='Reply briefly using the synthetic messages.',idempotency_key='materials-'+scenario))
                        sid=started['session']['id']
                        ui.put(f'/api/mcp/chats/{sid}/learning',json={'enabled':False},headers={'X-ChatWeave-UI':'1'})
                        def receive(text,image=False,mid=700000+number*10):
                            segments=[dict(type='text',data={'text':text})]
                            if image:
                                im=Image.new('RGB',(320,160),'white');draw=ImageDraw.Draw(im)
                                draw.rectangle((20,30,120,130),fill='red');draw.ellipse((190,30,290,130),fill='blue')
                                raw=io.BytesIO();im.save(raw,'PNG');segments.append(dict(type='image',data={'base64':base64.b64encode(raw.getvalue()).decode()}))
                            event=dict(post_type='message',message_type='group',self_id=111,group_id=222,message_id=mid,message_seq=mid,
                                user_id=333,sender={'user_id':333,'nickname':'Fixture'},time=int(time.time()),message=segments)
                            ReactionBot.history[str(mid)]=deepcopy(event);chat.receive(event)
                        receive('What shapes and colors are on the left and right?' if scenario=='image' else 'Reply with the word MORNING for this synthetic check.',scenario=='image')
                        eventually(lambda:chat.receiver.status()['state']=='connected')
                        packet=asyncio.run(initial(service,grant['token'],sid));assert not packet.isError
                        print('fixture: input ready '+scenario,flush=True)
                        home=root/('codex-'+scenario);home.mkdir()
                        for name in ('auth.json','models_cache.json'):
                            if (source_home/name).is_file():shutil.copy2(source_home/name,home/name)
                        args=['codex.cmd','exec','--json','--skip-git-repo-check','-s','read-only','-C',str(home),'-c','approval_policy="never"']
                        for key in ('model','model_provider','model_providers','openai_base_url','model_catalog_json','model_reasoning_effort'):
                            if key in config:args+=['-c',key+'='+toml(config[key])]
                        mcp={'url':service.status()['url'],'http_headers':{'Authorization':'Bearer '+grant['token']},
                            'enabled_tools':['plan_chat_reply','send_chat_reply','read_chat_image','read_chat_forward','get_chat_session','select_chat_expressions'],'tool_timeout_sec':30,
                            'tools':{'plan_chat_reply':{'approval_mode':'approve'},'send_chat_reply':{'approval_mode':'approve'}}}
                        args+=['-c','mcp_servers.materials='+toml(mcp)]
                        for index,block in enumerate(packet.content):
                            if block.type=='image':
                                path=home/f'input-{index}.jpg';path.write_bytes(base64.b64decode(block.data));args+=['--image',str(path)]
                        prompt='You are validating an isolated MCP fake QQ group. Use ONLY materials MCP tools; no shell, files, other agents or web. The input below was already delivered via MCP HTTP before your first request; attached images are its actual ImageContent. Plan with plan_chat_reply using this input_batch, then send_chat_reply with expression_ids=[] and text bubbles. Do not read the session or images again. If stale returns updated_chat, directly replan from that data and send the corrected answer. End after success.\n'+json.dumps(packet.structuredContent,ensure_ascii=False)
                        args+=['-'];env=dict(os.environ,CODEX_HOME=str(home))
                        original=chat.turns.plan;injected=[]
                        def plan(*a,**kw):
                            result=original(*a,**kw)
                            if scenario=='correction' and result.get('state')=='READY' and not injected:
                                injected.append(True);receive('Correction: reply AFTERNOON instead of MORNING.',mid=700099)
                            return result
                        before=len(ReactionBot.sent)
                        faulthandler.cancel_dump_traceback_later()
                        with patch.object(chat.turns,'plan',side_effect=plan):
                            try:p=subprocess.run(args,input=prompt,cwd=home,env=env,capture_output=True,text=True,encoding='utf-8',timeout=240)
                            except subprocess.TimeoutExpired:
                                summary.append(dict(client='Codex CLI',scenario=scenario,state='timeout',model_requests=None));break
                        (out/f'codex-{scenario}.jsonl').write_text(p.stdout,encoding='utf-8');(out/f'codex-{scenario}.stderr').write_text(p.stderr,encoding='utf-8')
                        count,used,evidence=generations(home/'sessions')
                        (out/f'codex-{scenario}.generations.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
                        observed=[]
                        for line in p.stdout.splitlines():
                            event=json.loads(line);item=event.get('item',{})
                            if item.get('type')=='mcp_tool_call' and item.get('status') in ('completed','failed'):
                                observed.append(dict(tool=item['tool'],state=item['status'],error=item.get('error')))
                        with access.connect() as db:
                            turns=[dict(r) for r in db.execute('SELECT state,reason FROM chat_turn_plans WHERE session_id=?',(sid,))]
                        texts=[x['message'] for x in ReactionBot.sent[before:]]
                        wording=json.dumps(texts,ensure_ascii=False).lower()
                        correct=(len(texts)==1 and ('morning' in wording if scenario=='text' else
                            all(word in wording for word in ('left','red','square','right','blue','circle')) if scenario=='image' else
                            'afternoon' in wording and 'morning' not in wording))
                        summary.append(dict(client='Codex CLI',scenario=scenario,exit_code=p.returncode,model_requests=count,tools=used,mcp_calls=observed,
                            semantic_check=correct,fake_writes=len(texts),messages=texts,turns=turns,input_image_blocks=sum(c.type=='image' for c in packet.content),
                            boundary='initial MCP HTTP input injected before first CLI request; includes final non-tool response; desktop native MCP rendering not verified'))
                        call('stop_chat_session',dict(session_id=sid))
                        print(json.dumps({k:v for k,v in summary[-1].items() if k not in ('messages',)},ensure_ascii=False),flush=True)
                    access.revoke(grant['id']);service.stop()
    finally:
        upstream.shutdown();upstream.server_close();events.close()
        (out/'clients.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
