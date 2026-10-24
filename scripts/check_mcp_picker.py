"""Synthetic platform filtering and pagination coverage."""
import sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parent),str(Path(__file__).resolve().parents[1])]
import json,tempfile
from fastapi import FastAPI
from fastapi.testclient import TestClient
from chatlocal.store import Store
from chatlocal.mcp_routes import install_mcp_routes

def main():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);store=Store(root/'chats.sqlite3')
        rows=[dict(platform=p,conversation_id=f'{p}-{i}',conversation=f'合成群{i:03}',
                   conversation_type='group',sender_id='fixture',sender='合成成员',
                   timestamp='2026-10-01 10:00:00',content='合成消息',source_id=f'{p}-{i}',is_self=False)
              for p in ('qq','wechat') for i in range(82)]
        rows.append(dict(rows[-1],conversation_id='unnamed@chatroom',source_id='unnamed',
                         conversation='联系人备注、乙、丙（4人群）'))
        source=root/'fixture.json';source.write_text(json.dumps(rows),'utf-8');store.import_file(source)
        app=FastAPI();install_mcp_routes(app,store)
        with TestClient(app) as client:
            for platform in ('qq','wechat'):
                first=client.get('/api/mcp/conversations',params={'platform':platform}).json()
                assert len(first['items'])==80 and first['has_more']
                second=client.get('/api/mcp/conversations',params={'platform':platform,'offset':first['next_offset']}).json()
                assert len(second['items'])==(2 if platform=='qq' else 3) and not second['has_more']
                assert all(item['platform']==platform for item in first['items']+second['items'])
            result=client.get('/api/mcp/conversations',params={'platform':'wechat','query':'联系人备注'}).json()
            assert [item['conversation_id'] for item in result['items']]==['unnamed@chatroom']
            for platform in ('qq','wechat'):
                ids=client.get('/api/mcp/conversations',params={'platform':platform,'query':'-81'}).json()['items']
                assert [item['conversation_id'] for item in ids]==[platform+'-81']
            assert client.get('/api/mcp/conversations?platform=invalid').status_code==400
            assert client.get('/api/mcp/conversations').json()['items'][0]['platform']=='qq'
    print('PASS: platform filtering before pagination, search and validation')

if __name__=='__main__':main()
