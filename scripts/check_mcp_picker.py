"""Synthetic platform filtering and pagination coverage.

#350 narrowed this build to qq only, so the platform-filter half of the original
fixture (82 qq + 82 wechat conversations) has nothing left to distinguish. The
conversation *picker* still has to page, search and validate, so the fixture
keeps those dimensions on the single platform that survives: 164 qq conversations
(so page 1 fills at 80 and page 2 still has 4 + the unnamed one), plus the
unnamed-chatroom row that pins "display name is searchable".
"""
import sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1])]
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from chatlocal.store import Store
from chatlocal.mcp_routes import install_mcp_routes
from check_fixture_support import temp_dir  # noqa: E402

def main():
    ROOT=Path(__file__).resolve().parents[1]
    (ROOT/'.tmp').mkdir(exist_ok=True)
    with temp_dir(dir=ROOT/'.tmp',prefix='mcp-picker-') as tmp:
        root=tmp;store=Store(root/'chats.sqlite3')
        rows=[dict(platform='qq',conversation_id=f'qq-{i}',conversation=f'合成群{i:03}',
                   conversation_type='group',sender_id='fixture',sender='合成成员',
                   timestamp='2026-10-01 10:00:00',content='合成消息',source_id=f'qq-{i}',is_self=False)
              for i in range(164)]
        rows.append(dict(rows[-1],conversation_id='unnamed@chatroom',source_id='unnamed',
                         conversation='联系人备注、乙、丙（4人群）'))
        source=root/'fixture.json';source.write_text(json.dumps(rows),'utf-8');store.import_file(source)
        app=FastAPI();install_mcp_routes(app,store)
        with TestClient(app) as client:
            first=client.get('/api/mcp/conversations',params={'platform':'qq'}).json()
            assert len(first['items'])==80 and first['has_more']
            second=client.get('/api/mcp/conversations',params={'platform':'qq','offset':first['next_offset']}).json()
            # 165 conversations total: 80 + 80 + 5.
            assert len(second['items'])==80 and second['has_more']
            third=client.get('/api/mcp/conversations',params={'platform':'qq','offset':second['next_offset']}).json()
            assert len(third['items'])==5 and not third['has_more']
            assert all(item['platform']=='qq' for item in first['items']+second['items']+third['items'])
            result=client.get('/api/mcp/conversations',params={'platform':'qq','query':'联系人备注'}).json()
            assert [item['conversation_id'] for item in result['items']]==['unnamed@chatroom']
            ids=client.get('/api/mcp/conversations',params={'platform':'qq','query':'-81'}).json()['items']
            assert [item['conversation_id'] for item in ids]==['qq-81']
            # Any platform other than qq is a validation error rather than an
            # empty page: "we don't read that" and "nothing matched" must not
            # look the same (#350).
            for bad in ('invalid','wechat'):
                assert client.get('/api/mcp/conversations',params={'platform':bad}).status_code==400, bad
            assert client.get('/api/mcp/conversations').json()['items'][0]['platform']=='qq'
    print('PASS: platform filtering before pagination, search and validation')

if __name__=='__main__':main()
