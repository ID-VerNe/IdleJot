"""Shared OneBot .env settings -> live MCP tool visibility -> HTTP notice read; no LLM key, hot token replacement, no sending. Isolated OneBot fixture.

Water-group slim: the desktop /api/desktop/settings + /api/desktop/onebot/test UI
surface is removed in Phase 3. The OneBot HTTP endpoint+token now lives in .env
(REPLY_ONEBOT_URL/REPLY_ONEBOT_TOKEN), shared by the reply sender and the read
tools. This gate writes .env directly and clears the availability cache to
verify the same guarantees the deleted UI surface used to carry: read_qq_group
and get_group_knowledge visibility tracks the live OneBot probe, a bad token
removes availability without a restart, no LLM key is written, no real QQ send.
"""
import asyncio
import json
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT=Path(sys.argv[sys.argv.index('--package')+1]).resolve() if '--package' in sys.argv else Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_fixture_support import isolated_env
from fixture_server import FixtureHandler,fixture_server
from fastapi import FastAPI
from fastapi.testclient import TestClient
from chatlocal.store import Store
from chatlocal.mcp_routes import install_mcp_routes
from chatlocal.mcp_connect import test_connection
from chatlocal.onebot import invalidate_availability
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import httpx


class Fixture(FixtureHandler):
    actions=[]
    def do_POST(self):
        if self.headers.get('Authorization')!='Bearer fixture-onebot':
            self.send_response(401);self.end_headers();return
        body=self.read_json()
        action=self.path.removeprefix('/');self.actions.append(action)
        data=None
        if action=='get_login_info':data=dict(user_id=111)
        elif action=='_get_group_notice':
            assert str(body['group_id'])=='222'
            data=[dict(notice_id='n1',sender_id=333,publish_time=1790784000,message=dict(text='共享配置公告验收'))]
        elif action=='get_essence_msg_list':data=[]
        else:raise AssertionError('Unexpected OneBot action: '+action)
        self.send_json(dict(status='ok',retcode=0,data=data))


async def read_notice(url,token):
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},trust_env=False) as http:
        async with streamable_http_client(url,http_client=http) as (read,write,_):
            async with ClientSession(read,write) as client:
                await client.initialize()
                names={t.name for t in (await client.list_tools()).tools}
                assert 'get_group_knowledge' in names and 'qq_group_admin' not in names
                result=await client.call_tool('get_group_knowledge',dict(group_id='111:group:222',types=['notice']))
                assert not result.isError,result
                assert '共享配置公告验收' in str(result.structuredContent),result


def write_onebot_env(folder,url,token):
    # The slim config surface: OneBot HTTP endpoint+token in .env, shared by the
    # reply sender and the read tools. No API_KEY/MODEL is ever written here.
    (folder/'.env').write_text(f'REPLY_ONEBOT_URL={url}\nREPLY_ONEBOT_TOKEN={token}\n','utf-8')


def main():
    try:
        with fixture_server(Fixture) as server,tempfile.TemporaryDirectory(dir=ROOT/'.tmp',prefix='mcp-onebot-') as tmp,patch.dict('os.environ',isolated_env(),clear=True):
            folder=Path(tmp);(folder/'.tmp').mkdir();store=Store(folder/'chats.sqlite3')
            imported=folder/'fixture.json';imported.write_text(json.dumps([dict(platform='qq',conversation_id='111:group:222',conversation='Fixture group',conversation_type='group',sender='Fixture',source_id='1',content='测试资料',timestamp='2026-10-01')]),'utf-8');store.import_file(imported)
            app=FastAPI();service=install_mcp_routes(app,store)
            headers={'X-ChatWeave-UI':'1'}
            with patch('chatlocal.onebot.ROOT',folder),TestClient(app) as ui:
                invalidate_availability()
                with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                assert ui.put('/api/mcp',json=dict(enabled=True,port=port),headers=headers).json()['running']
                created=ui.post('/api/mcp/connections',headers=headers,json=dict(name='Fixture',platforms=['qq'],all_conversations=True,onebot=True)).json()
                url=service.status()['url'];token=created['token']
                # No OneBot configured: the availability probe has no endpoint, so
                # read_qq_group/get_group_knowledge stay hidden from tools/list.
                assert not asyncio.run(test_connection(url,token))['onebot']
                # Share the OneBot HTTP endpoint+token via .env; the probe now succeeds
                # and the QQ read tools become visible without a service restart.
                write_onebot_env(folder,'http://127.0.0.1:'+str(server.server_port),'fixture-onebot')
                invalidate_availability()
                assert asyncio.run(test_connection(url,token))['onebot']
                asyncio.run(read_notice(url,token))
                assert 'API_KEY' not in (folder/'.env').read_text('utf-8')
                # Hot token replacement: a bad token fails the probe and hides the
                # QQ read tools again, with no restart and no real send.
                write_onebot_env(folder,'http://127.0.0.1:'+str(server.server_port),'incorrect')
                invalidate_availability()
                assert not asyncio.run(test_connection(url,token))['onebot']
    finally:invalidate_availability()
    assert '_get_group_notice' in Fixture.actions
    print('PASS shared OneBot .env settings -> live MCP tool visibility -> HTTP notice read; no LLM key, hot token replacement, no sending. Isolated OneBot fixture.')


if __name__=='__main__':main()
