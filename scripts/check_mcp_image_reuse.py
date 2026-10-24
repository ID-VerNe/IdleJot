"""Local pixels and bounded DNS, using synthetic files and isolated OneBot only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
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
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_fixture_support import isolated_env
from check_mcp_chat_enrichment import DetailBot, Events, eventually, ROOT
from check_mcp_chat_media import fixture_image, dns_response
from chatlocal.media import cache_image
from chatlocal.mcp_chat_local_media import history_image
from chatlocal.mcp_chat_media import public_addresses
from chatlocal.mcp_routes import install_mcp_routes
from chatlocal.store import Store
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import dns.exception
import dns.query
import dns.resolver
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def dns_checks():
    # A real unanswered UDP exchange must expire without ever calling libc DNS.
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sink:
        sink.bind(('127.0.0.1',0))
        resolver=dns.resolver.Resolver(configure=False)
        resolver.nameservers=['127.0.0.1'];resolver.port=sink.getsockname()[1];resolver.timeout=.05
        with patch('socket.getaddrinfo',side_effect=lambda *a,**k:time.sleep(12)) as system, \
             patch('dns.resolver.Resolver',return_value=resolver),patch('dns.query.tls',side_effect=dns.exception.Timeout) as fallback:
            began=time.monotonic()
            try:public_addresses('fixture.qq.com',443);raise AssertionError('unanswered DNS succeeded')
            except ValueError:pass
            assert time.monotonic()-began<2.3 and not system.called
            assert fallback.call_args.kwargs['timeout']<2
            assert fallback.call_args.kwargs['server_hostname']=='dns.alidns.com'
            assert fallback.call_args.kwargs['verify'] is True
    # Stall the TLS handshake itself. dnspython's absolute expiry must apply.
    with socket.socket() as sink:
        sink.bind(('127.0.0.1',0));sink.listen();sink.settimeout(1)
        release=threading.Event()
        def accept():
            try:
                conn,_=sink.accept()
                with conn:release.wait(2)
            except OSError:pass
        worker=threading.Thread(target=accept);worker.start();original=dns.query.tls
        def stalled(query,address,**kwargs):
            assert address=='223.5.5.5' and kwargs['server_hostname']=='dns.alidns.com'
            return original(query,'127.0.0.1',port=sink.getsockname()[1],**kwargs)
        try:
            with patch('socket.getaddrinfo',side_effect=AssertionError('fallback resolved its hostname')), \
                 patch('dns.resolver.Resolver.resolve_name',return_value=SimpleNamespace(addresses=lambda:['198.18.0.2'])), \
                 patch('dns.query.tls',side_effect=stalled):
                began=time.monotonic()
                try:public_addresses('fixture.qq.com',443,deadline=began+.15);raise AssertionError('stalled TLS succeeded')
                except ValueError:pass
                assert time.monotonic()-began<.6
        finally:release.set();worker.join(2)
    with patch('dns.resolver.Resolver.resolve_name',side_effect=dns.exception.Timeout), \
         patch('dns.query.tls',return_value=dns_response('fixture.qq.com','175.27.35.211')):
        assert public_addresses('fixture.qq.com',443)[0][4][0]=='175.27.35.211'
    print('PASS: real DNS timeout, no getaddrinfo, TLS bootstrap/handshake deadline and safe fallback')


async def wire(service,token,sid,eid,thumbnail=True,source='history_cache'):
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},trust_env=False,timeout=20) as http:
        async with streamable_http_client(service.status()['url'],http_client=http) as (r,w,_):
            async with ClientSession(r,w) as client:
                await client.initialize()
                result=await client.call_tool('read_chat_image',dict(session_id=sid,event_id=eid))
                assert not result.isError and result.structuredContent['thumbnail'] is thumbnail,result
                assert result.structuredContent['representation']['source']==source
                frame=next(c for c in result.content if c.type=='image')
                import base64
                with Image.open(io.BytesIO(base64.b64decode(frame.data))) as image:assert image.size==(80,60)


def main():
    ssl.create_default_context()
    dns_checks()
    events=Events();upstream=ThreadingHTTPServer(('127.0.0.1',0),DetailBot)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    raw=fixture_image();animated=fixture_image(True)
    try:
        with tempfile.TemporaryDirectory(dir=ROOT/'.tmp',prefix='image-reuse-') as tmp,patch.dict('os.environ',isolated_env(),clear=True):
            root=Path(tmp);data=root/'data';data.mkdir()
            (root/'.env').write_text(f'REPLY_ONEBOT_URL=http://127.0.0.1:{upstream.server_port}\nREPLY_ONEBOT_WS_URL=ws://127.0.0.1:{events.port}\nREPLY_ONEBOT_WS_TOKEN=event-fixture\n')
            with patch('chatlocal.onebot.ROOT',root),patch('chatlocal.message_sender.ROOT',root), \
                 patch('chatlocal.media.DATA',data),patch('chatlocal.media.ROOT',root), \
                 patch('chatlocal.media.local_path',side_effect=lambda p:(root/p).resolve()):
                store=Store(data/'chats.sqlite3');app=FastAPI();service=install_mcp_routes(app,store)
                access=service.access;chat=service.tools.chat;media=chat.media
                with TestClient(app):
                    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
                    access.configure(True,port);service.start()
                    g=access.create(dict(name='reuse fixture',platforms=['qq'],conversations=[['qq','111:group:222']],chat=True,send=True,chat_images=True))
                    def call(name,**args):return service.tools.call(g['token'],name,args,threading.Event())
                    sid=call('start_chat_session',conversation_id='111:group:222',persona='fixture',idempotency_key='reuse-start').structuredContent['session']['id']
                    eventually(lambda:chat.receiver.status()['state']=='connected')
                    stamp=int(time.time())*1000;serial=0
                    def add(file,url='https://gchat.qpic.cn/reuse',segments=None):
                        nonlocal serial
                        serial+=1
                        chat.receive(dict(post_type='message',message_type='group',self_id=111,group_id=222,user_id=333,
                            message_id=80000+serial,sender=dict(user_id=333),time=stamp/1000,
                            message=segments or [dict(type='image',data=dict(file=file,url=url))]))
                        with access.connect() as db:return db.execute('SELECT id FROM chat_inbox WHERE session_id=? AND message_id=?',(sid,str(80000+serial))).fetchone()[0]
                    def history(blob,md5,thumb=False,at=None,**extra):
                        item=cache_image(blob,md5=md5,thumbnail=thumb);item.update(extra)
                        with store.connect() as db:
                            db.execute('''INSERT INTO messages(dedup_key,platform,conversation_id,conversation,sender_id,sender,timestamp,content,is_self,source_id,media_json)
                                VALUES(?,'qq','111:group:222','fixture','333','fixture',?,'[image]',0,'native-not-onebot',?)''',
                                (item['sha256']+str(thumb)+str(serial),stamp if at is None else at,json.dumps([item])))
                        return item
                    original_hash=hashlib.md5(raw).hexdigest();original_at=stamp-86400000
                    original=history(raw,original_hash,at=original_at)
                    eid=add(original_hash+'.png')
                    with patch.object(media,'client',side_effect=AssertionError('history hit queried adapter')), \
                         patch('chatlocal.mcp_chat_media.download',side_effect=AssertionError('history hit downloaded')) as download, \
                         patch.object(media,'store_bytes',wraps=media.store_bytes) as saved:
                        out=call('read_chat_image',session_id=sid,event_id=eid)
                        assert not out.isError and out.structuredContent['representation']['source']=='history_cache',out
                        assert out.structuredContent['thumbnail'] is False
                        call('get_chat_session',session_id=sid)
                        assert download.call_count==0
                        assert saved.call_count==1, saved.call_count
                    with patch.object(media,'client',side_effect=AssertionError('repeat queried adapter')), \
                         patch('chatlocal.mcp_chat_media.download',side_effect=AssertionError('repeat downloaded')):
                        for elapsed in (60000,86400000):
                            stamp+=elapsed
                            repeated=add(original_hash+'.png')
                            # Force the history path rather than the realtime cache hint.
                            with access.connect() as db:db.execute("UPDATE chat_media_assets SET digest='',fetch_key=''")
                            asyncio.run(wire(service,g['token'],sid,repeated,thumbnail=False))
                    times=[]
                    for _ in range(10):
                        began=time.perf_counter();source={}
                        assert history_image(store,'111:group:222',dict(file=original_hash,timestamp=stamp),threading.Event(),source)==raw
                        times.append((time.perf_counter()-began)*1000)
                    assert max(times)<200,times
                    with store.connect() as db:
                        from chatlocal.image_identity import LOOKUP,ORDER
                        plan=db.execute('EXPLAIN QUERY PLAN '+LOOKUP+ORDER,('111:group:222',original_hash)).fetchall()
                    assert any('image_identity_lookup' in r['detail'] and 'SEARCH' in r['detail'] for r in plan)
                    assert not any('SCAN ' in r['detail'] for r in plan),[r['detail'] for r in plan]
                    for cid,ref in (('111:group:223',dict(file=original_hash,timestamp=stamp)),('999:group:222',dict(file=original_hash,timestamp=stamp)),('111:direct:222',dict(file=original_hash,timestamp=stamp)),('111:group:222',dict(file='no-hash',timestamp=stamp))):
                        assert history_image(store,cid,ref,threading.Event(),{}) is None
                    assert history_image(store,'111:group:222',dict(file=original_hash),threading.Event(),{})==raw
                    assert history_image(store,'111:group:222',dict(file=original_hash,timestamp=stamp),threading.Event(),{},plan=SimpleNamespace(start=stamp,end=None)) is None
                    # Missing metadata is not treated as proof; originals can
                    # still be verified against the actual requested MD5.
                    missing=dict(original,metadata={});missing.pop('sha256')
                    with store.connect() as db:db.execute('UPDATE messages SET media_json=?,timestamp=? WHERE id=1',(json.dumps([missing]),stamp))
                    assert history_image(store,'111:group:222',dict(file=original_hash,timestamp=stamp),threading.Event(),{})==raw
                    with store.connect() as db:db.execute('UPDATE messages SET media_json=?,timestamp=? WHERE id=1',(json.dumps([original]),original_at))
                    thumb_hash='a'*32;history(raw,thumb_hash,thumb=True);thumb=add(thumb_hash+'.png')
                    with patch('chatlocal.mcp_chat_media.download',side_effect=AssertionError('thumbnail downloaded')):
                        asyncio.run(wire(service,g['token'],sid,thumb))
                    # Detail request can replace a known thumbnail; URL-only adapter is not a local hit.
                    DetailBot.image={'url':'https://gchat.qpic.cn/reuse'}
                    with patch('chatlocal.mcp_chat_media.download',return_value=animated) as download:
                        out=call('read_chat_image',session_id=sid,event_id=thumb,prefer_original=True)
                        assert not out.isError and out.structuredContent['frame_indices']==[0,1,2],out
                        assert out.structuredContent['representation']['source']=='remote' and download.call_count==1
                    print('PASS: indexed history originals/thumbnails, exact hash and scope, real MCP pixels, original preference; lookup max_ms='+str(round(max(times),2)))
                    # Quoted and forwarded originals can predate the parent event.
                    old=io.BytesIO();Image.new('RGB',(80,60),'blue').save(old,'PNG');old=old.getvalue()
                    old_hash=hashlib.md5(old).hexdigest();old_stamp=stamp-60000
                    history(old,old_hash,at=old_stamp)
                    picture=dict(type='image',data=dict(file=old_hash+'.png',url='https://gchat.qpic.cn/older'))
                    DetailBot.history['70000']=dict(message_id=70000,message_type='group',group_id=222,
                        user_id=444,sender=dict(user_id=444),time=old_stamp/1000,message=[picture])
                    quoted=add('',segments=[dict(type='reply',data=dict(id='70000')),dict(type='forward',data=dict(nodes=[
                        dict(sender=dict(user_id=444),time=old_stamp/1000,content=[picture])]))])
                    with patch.object(media,'client',side_effect=AssertionError('old history hit queried adapter')), \
                         patch('chatlocal.mcp_chat_media.download',side_effect=AssertionError('old quote/forward downloaded')):
                        packet=call('get_chat_session',session_id=sid)
                        assert not packet.isError,packet
                        entries=[e for e in packet.structuredContent['materials']['images'] if e['event_id']==quoted]
                        assert len(entries)==2 and all(e['representation']['source']=='history_cache' for e in entries),entries
                    # Two event handles, including a forward node, share one download.
                    shared_hash=hashlib.md5(animated).hexdigest();first=add(shared_hash+'.gif')
                    forward=add('',segments=[dict(type='forward',data=dict(nodes=[dict(sender=dict(user_id=444),time=stamp/1000,
                        content=[dict(type='image',data=dict(file=shared_hash+'.gif',url='https://gchat.qpic.cn/reuse'))])]))])
                    with access.connect() as db:row=db.execute('SELECT payload FROM chat_inbox WHERE id=?',(forward,)).fetchone()
                    grant=access.authorize(g['token'],source=False)
                    chat.details.enrich(grant,chat.row(sid),[dict(json.loads(row[0]),id=forward)])
                    entered=threading.Event();release=threading.Event()
                    def fetch(*args,**kwargs):entered.set();assert release.wait(5);return animated
                    with patch('chatlocal.mcp_chat_media.download',side_effect=fetch) as download, \
                         patch.object(media,'store_bytes',wraps=media.store_bytes) as saved,ThreadPoolExecutor(2) as pool:
                        one=pool.submit(call,'read_chat_image',session_id=sid,event_id=first)
                        assert entered.wait(3)
                        two=pool.submit(call,'read_chat_image',session_id=sid,event_id=forward)
                        release.set();results=[one.result(5),two.result(5)]
                        assert all(not r.isError for r in results),results
                        assert download.call_count==1
                        assert saved.call_count==1, saved.call_count
                    with patch('chatlocal.mcp_chat_media.download',side_effect=AssertionError('refresh downloaded')), \
                         patch.object(media,'store_bytes',wraps=media.store_bytes) as saved:
                        refreshed=call('get_chat_session',session_id=sid)
                        assert not refreshed.isError and refreshed.structuredContent['materials']['images']
                        assert saved.call_count==0, saved.call_count
                    # A missing or corrupt cache file is reacquired, failures are never sticky.
                    digest=hashlib.sha256(animated).hexdigest();cache=media.root/(digest+'.bin')
                    for state in ('missing','corrupt'):
                        if state=='missing':cache.unlink()
                        else:cache.write_bytes(b'corrupt')
                        with patch('chatlocal.mcp_chat_media.download',return_value=animated) as download:
                            assert not call('read_chat_image',session_id=sid,event_id=first).isError
                            assert not call('read_chat_image',session_id=sid,event_id=forward).isError
                            assert download.call_count==1
                    retry=add('b'*32+'.png')
                    with patch('chatlocal.mcp_chat_media.download',side_effect=[ValueError('fixture failed'),raw]) as download:
                        assert call('read_chat_image',session_id=sid,event_id=retry).isError
                        assert not call('read_chat_image',session_id=sid,event_id=retry).isError
                        assert download.call_count==2
                    # Corrupt history bytes cannot be mistaken for a local hit.
                    (root/original['local_path']).write_bytes(b'changed')
                    assert history_image(store,'111:group:222',dict(file=original_hash,timestamp=stamp),threading.Event(),{}) is None
                    assert chat.row(sid)['cursor']==0
                    assert not DetailBot.sent
                    call('stop_chat_session',session_id=sid)
                    # A different grant cannot use history outside its date window.
                    (root/original['local_path']).write_bytes(raw)
                    g=access.create(dict(name='bounded reuse fixture',platforms=['qq'],conversations=[['qq','111:group:222']],
                        start=time.strftime('%Y-%m-%d'),chat=True,send=True,chat_images=True))
                    sid=call('start_chat_session',conversation_id='111:group:222',persona='fixture',idempotency_key='bounded-start').structuredContent['session']['id']
                    bounded=add(original_hash+'.png')
                    with patch('chatlocal.mcp_chat_media.download',return_value=raw) as download:
                        asyncio.run(wire(service,g['token'],sid,bounded,thumbnail=None,source='remote'))
                        assert download.call_count==1,'out-of-window history reused'
                    call('stop_chat_session',session_id=sid)
                    print('PASS: concurrent direct/forward fetch once, recovery reuse, missing/corrupt retry, failed fetch retry, no sends/ACK')
    finally:upstream.shutdown();upstream.server_close();events.close()


if __name__=='__main__':main()
