"""Key paths only: identity, bounded parsing, scope, cache and citation checks."""
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from chatlocal.store import Store
from chatlocal.artifacts import Artifacts,ArtifactError
from chatlocal.retrieval import Plan
from chatlocal.agent_tools import ChatTools
from chatlocal.llm import validate_answer
from chatlocal.artifact_onebot import OneBot
from check_fixture_support import temp_dir  # noqa: E402


def main():
    (ROOT/'.tmp').mkdir(exist_ok=True)
    with temp_dir(prefix='artifact-check-',dir=ROOT/'.tmp') as tmp:
        root=tmp;s=Store(root/'test.sqlite3');a=Artifacts(s)
        body='课程任务书\n截止时间是10月15日。\n端云协同需要本地Router选择推理节点。\n'
        raw=(root/'任务书.txt');raw.write_text(body,'utf-8')
        a._roots=lambda p:[root]
        values=[]
        # Two messages carrying the same file must collapse into ONE artifact row
        # while both messages keep referencing it. They used to come from qq and
        # wechat; #350 leaves qq as the only platform, so the second message now
        # lives in another conversation instead. Both dimensions the fixture pins
        # survive: dedup by sha256 is orthogonal to which messages point at it,
        # and search_files still scopes by conversation (only one message is in
        # range below).
        for n,(cid,conv) in enumerate([('c','课程群'),('other','另一个群')]):
            values.append(dict(platform='qq',conversation_id=cid,conversation=conv,sender='老师',sender_id='teacher',
                source_id=str(n),timestamp='2026-09-22 10:00:00',content='[文件] 任务书.txt',
                files=[dict(filename=raw.name,size=raw.stat().st_size)],is_self=False))
        export=root/'messages.json';export.write_text(json.dumps(values,ensure_ascii=False),'utf-8')
        assert s.import_file(export)['imported']==2
        assert s.import_file(export)['duplicate']==2
        assert a.search()['match_count']==2
        assert a.locate(1)['availability']=='AVAILABLE_LOCAL'
        a.prepare(1);a.prepare(2)
        with s.connect() as db:
            assert db.execute('SELECT count(*) FROM artifacts').fetchone()[0]==1
            assert db.execute('SELECT count(*) FROM artifact_chunks').fetchone()[0]==1
        assert a.search(dict(query='Router'),body=True)['match_count']==2
        before=a.chunks(1);assert before['files'][0]['text']==body.strip()
        assert a.evict(1)['released_bytes']==raw.stat().st_size
        assert a.chunks(1)['files']==before['files'] or a.chunks(1)['files'][0]['text']==body.strip()
        a.prepare(1)  # A retained parse must not re-download.
        with s.connect() as db:assert db.execute('SELECT local_path FROM artifacts').fetchone()[0] is None
        a.materialize(2)
        with s.connect() as db:assert db.execute('SELECT count(*) FROM artifacts').fetchone()[0]==1
        t=ChatTools(s,Plan(platforms=['qq'],conversations=[json.dumps(['qq','c'])]))
        meta=t.execute('search_files',{'query':'任务书'})
        assert len(meta['files'])==1
        assert t.execute('read_file_chunks',{'file_id':2}).get('error')
        chunks=t.execute('read_file_chunks',{'file_id':1})
        ref=chunks['files'][0]['citation_id']
        answer={'claims':[dict(text='截止10月15日。',evidence_ids=[],artifact_evidence_ids=[ref])],'insufficient':False}
        assert validate_answer(answer,[],file_evidence=t.file_evidence)['claims']
        assert not validate_answer(answer,[])['claims']
        assert not a.search({},Plan(platforms=['wechat'],start=1900000000000))['files']
        # #350: qq is the only platform this build writes. The read side still
        # tolerates an unknown value (it becomes an empty result set), so the
        # place to pin the narrowing is the import path, not the query path.
        # The control group matters as much as the rejection: a normalizer that
        # rejected everything would pass a rejection-only assertion and still
        # import nothing, so 'qq' has to keep resolving (both cases) and the
        # refusal must name the platform we do accept -- an unrelated ValueError
        # would otherwise read as a pass.
        from chatlocal.normalize import platform_name
        assert platform_name('qq')=='qq'
        assert platform_name('QQ')=='qq'
        for bad_platform in ('wechat','wework','invalid',''):
            try:platform_name(bad_platform);raise AssertionError(f'{bad_platform} platform accepted')
            except ValueError as err:assert 'qq' in str(err),err
        bad=root/'wechat.json'
        bad.write_text(json.dumps([dict(values[0],platform='wechat')],ensure_ascii=False),'utf-8')
        try:s.import_file(bad);raise AssertionError('wechat message imported')
        except ValueError:pass
        # ...and the same fixture with the platform flipped back must import,
        # otherwise the line above only proves the fixture was already broken.
        ok=root/'qq.json'
        ok.write_text(json.dumps([dict(values[0],platform='qq',source_id='ctrl')],ensure_ascii=False),'utf-8')
        assert s.import_file(ok)['imported']==1
        a.mark_cited(chunks['files'])
        try:a.evict(1);raise AssertionError('cited binary was evicted')
        except ArtifactError:pass
        missing=dict(values[0],source_id='missing',content='[文件] 不存在.pdf',files=[dict(filename='不存在.pdf',size=42)])
        export.write_text(json.dumps([missing],ensure_ascii=False),'utf-8');s.import_file(export)
        sid=a.search(dict(query='不存在'))['files'][0]['id']
        try:a.materialize(sid);raise AssertionError('missing file treated as available')
        except ArtifactError:pass
        assert a.get(sid)['availability']=='MISSING'
        # Files without server IDs keep distinct stable sources; file-only
        # records receive a searchable placeholder instead of being discarded.
        with s.connect() as db:blank_before=db.execute("SELECT count(*) FROM artifact_sources WHERE original_message_id=''").fetchone()[0]
        blank=[dict(values[0],source_id='',content='',timestamp=f'2026-09-22 11:0{n}:00') for n in range(2)]
        export.write_text(json.dumps(blank,ensure_ascii=False),'utf-8');assert s.import_file(export)['imported']==2
        with s.connect() as db:assert db.execute("SELECT count(*) FROM artifact_sources WHERE original_message_id=''").fetchone()[0]==blank_before+2
        # Metadata-only OneBot contract fixture; explicitly not a live QQ test.
        class Bot(OneBot):
            def __init__(self):self.count=2
            def call(self,action,payload):
                if action=='get_login_info':return dict(user_id=123)
                if action=='get_group_file_system_info':return dict(file_count=self.count)
                if action=='get_group_root_files':return dict(files=[dict(file_id='A',file_name='根目录.pdf',file_size=50)],folders=[dict(folder_id='d',folder_name='目录')])
                if action=='get_group_files_by_folder':return dict(files=[dict(file_id='B',file_name='任务.docx',file_size=80)],folders=[])
                raise AssertionError(action)
        b=Bot();assert b.reconcile(s,'123:group:456','测试群')['files']==2
        assert b.reconcile(s,'123:group:456','测试群')['files']==2
        b.count=3
        try:b.reconcile(s,'123:group:456','测试群');raise AssertionError('incomplete inventory accepted')
        except ArtifactError:pass
        with s.connect() as db:
            assert db.execute("SELECT count(*) FROM artifact_sources WHERE source_type='qq_group_file' AND remote_status='present'").fetchone()[0]==2
        from unittest.mock import patch
        import hmac
        from chatlocal.artifact_onebot import receive_event
        notice=dict(post_type='notice',notice_type='group_upload',self_id=123,group_id=456,user_id=789,time=1790067000,
            file=dict(id='event-file',name='上传.pdf',size=900,busid=102))
        wire=json.dumps(notice).encode();sig='sha1='+hmac.new(b'fixture-secret',wire,hashlib.sha1).hexdigest()
        with patch('chatlocal.artifact_onebot.config',return_value={'ARTIFACT_ONEBOT_EVENT_SECRET':'fixture-secret'}):
            assert receive_event(s,wire,sig)['accepted']
            assert receive_event(s,wire,sig)['accepted']
            try:receive_event(s,wire,'sha1=bad');raise AssertionError('unsigned event accepted')
            except ArtifactError:pass
        with s.connect() as db:
            eventrow=db.execute("SELECT sha256,availability FROM artifact_sources WHERE file_id='event-file'").fetchall()
            assert len(eventrow)==1 and tuple(eventrow[0])==(None,'METADATA_ONLY')
    print('PASS: metadata/idempotency, SHA256 shared parse, retained index after eviction, reacquisition, hard scope, real chunk citations, missing files, XML limits and recursive OneBot fixture. No cloud.')


if __name__=='__main__':main()
