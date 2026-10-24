"""Independent jargon stages: fake time/models only; no external writes."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).parent))
from check_mcp_language_learning import Clock,ROOT,jargon_fixture,meaning_fixture,expression_fixture,review_fixture
from chatlocal.mcp_language_store import LanguageStore,LearningError


def rejected(code,fn):
    try:fn()
    except LearningError as e:assert e.code==code,(e.code,code)
    else:raise AssertionError('expected '+code)


def main():
    assert Path(__import__('chatlocal.mcp_jargon_learning',fromlist=['x']).__file__).resolve()==ROOT/'chatlocal/mcp_jargon_learning.py'
    clock=Clock();b=dict(scope='qq:111:group:222',gid='grant-a',sid='session-a',epoch='epoch-a')
    with tempfile.TemporaryDirectory(prefix='tulpa-jargon-pipeline-') as tmp:
        store=LanguageStore(Path(tmp)/'learning.sqlite3',clock=clock,policy=dict(hourly_calls=100))
        store.attach(b);serial=0;phases=[]
        def feed(tag):
            clock.advance(31)
            for i in range(10):store.observe(b,dict(peer=True,source_id=f'{tag}-{i}',at=clock(),
                text=f'云朵开机 今天开始组队啦 合成场景{i} 🧐 @某人 普通词'))
        def claim():
            t=store.claim(b,'pipeline-context')
            if t:phases.append(t['stage'])
            return t
        def submit(t,r=None,fail=None):
            nonlocal serial
            serial+=1
            return store.submit(b,t['job_id'],t['lease'],f'invocation-{serial}',r,failure=fail)
        def word():
            with store.db() as db:return dict(db.execute('SELECT * FROM jargon WHERE scope=?',(b['scope'],)).fetchone())

        # Only the dedicated contract can introduce candidates; neither a
        # quoted bot answer nor a forged extraction source is evidence.
        feed('first');e=claim();assert e['stage']=='extract'
        rejected('jargon_separate_stage_required',lambda:submit(e,dict(expressions=[],jargon=[dict(term='云朵开机',source_id='x')])))
        submit(e,dict(expressions=[expression_fixture(e['material']['messages'][i],f'situation{i}',f'style{i}') for i in range(3)]))
        t=claim();assert t['stage']=='extract_jargon'
        good=jargon_fixture(t)
        bad=copy.deepcopy(good);bad['jargon'][0]['checks']['not_ordinary_word']=False
        rejected('jargon_invalid_candidate',lambda:submit(t,bad))
        bad=copy.deepcopy(good);bad['jargon'][0]['source_id']='forged'
        rejected('invalid_source',lambda:submit(t,bad))
        bad=copy.deepcopy(good);bad['jargon'][0]['evidence_quote']='不存在的机器人回复'
        rejected('invalid_source',lambda:submit(t,bad))
        for term in ('🧐','@某人'):
            bad=jargon_fixture(t,(term,))
            rejected('jargon_invalid_candidate',lambda:submit(t,bad))
        bad=dict(jargon=[dict(term='云朵开机',source_id=t['material']['messages'][0]['source_id'])])
        rejected('jargon_evidence_required',lambda:submit(t,bad))
        submit(t,good);assert word()['count']==1
        assert store.status(b)['expression_count']==0 # review still pending
        review=claim();assert review['stage']=='review'
        submit(review,review_fixture(review));assert store.status(b)['expression_count']==3

        for tag in ('second','third'):
            feed(tag);e=claim();submit(e,dict(expressions=[]))
            t=claim();assert t['stage']=='extract_jargon'
            # Model misses an existing word; mechanical matching counts once.
            result=dict(jargon=[]);submit(t,result)
            with ThreadPoolExecutor(4) as pool:
                receipts=list(pool.map(lambda _:store.submit(b,t['job_id'],t['lease'],'same-retry',result),range(4)))
            assert all(x['state']=='already_completed' for x in receipts)
        assert word()['count']==3

        # Expression retry does not block extraction or the inference milestone.
        feed('fourth');e=claim();submit(e,fail='timeout')
        t=claim();assert t['stage']=='extract_jargon';submit(t,dict(jargon=[]))
        assert word()['count']==4
        wake=store.readiness(b);assert wake['stage']=='with_context'
        with ThreadPoolExecutor(2) as pool:tasks=list(pool.map(lambda _:store.claim(b,'pipeline-context',wake_id=wake['wake_id']),range(2)))
        assert sum(t is not None for t in tasks)==1;t=next(t for t in tasks if t)
        assert t['context_verification']['isolated'] is False and t['independence']=='degraded'
        bad=meaning_fixture(t);bad['usage_evidence'][0]['quote']='编造依据'
        rejected('invalid_source',lambda:submit(t,bad))
        result=meaning_fixture(t);submit(t,result)
        t=claim();assert t['stage']=='without_context' and set(t['material'])=={'term'}
        assert '不是独立盲测' in t['prompt']
        submit(t,dict(meaning='',insufficient=True,reason='常见词义中无法确定'))
        t=claim();assert t['stage']=='compare'
        submit(t,dict(is_similar=False,sufficient=True,reason='合成上下文确实用它表示开组'))
        assert word()['is_jargon'] and word()['last_inference_count']==4
        assert store.context(b,'use-word',['云朵开机'],'fixture')['jargon']
        assert store.readiness(b)['reason']=='retry_cooldown'
        clock.advance(15);retry=claim();assert retry['job_id']==e['job_id']
        submit(retry,fail='timeout');assert not store.readiness(b)['ready']
        print('PASS candidate/source rejection; standalone extraction before review; timeout bypass; milestone inference; sequential limitation; concurrent dedup')

        # At 8, insufficient evidence stops after one phase and preserves the
        # previously supported meaning. A late compare cannot change it.
        with store.db() as db:db.execute('UPDATE jargon SET count=7 WHERE scope=?',(b['scope'],))
        feed('eighth');e=claim();submit(e,dict(expressions=[]));t=claim();submit(t,dict(jargon=[]))
        t=claim();assert t['stage']=='with_context';before=word()['meaning']
        done=submit(t,dict(meaning='',insufficient=True,reason='新材料不足以重新解释',usage_evidence=[]))
        assert done['state']=='completed' and done['next_stage'] is None
        assert word()['last_inference_count']==8 and word()['meaning']==before and not claim()

        # At 25, an ordinary meaning is not injected, but the prior meaning is
        # retained for future re-evaluation, just like the reference algorithm.
        with store.db() as db:db.execute('UPDATE jargon SET count=24 WHERE scope=?',(b['scope'],))
        feed('twenty-five');e=claim();submit(e,dict(expressions=[]));t=claim();submit(t,dict(jargon=[]))
        t=claim();submit(t,meaning_fixture(t));t=claim();submit(t,dict(meaning='通常意义一致',insufficient=False,reason='fixture'))
        t=claim();submit(t,dict(is_similar=True,sufficient=True,reason='无需群内知识也能解释'))
        assert not word()['is_jargon'] and word()['meaning']==before
        assert not store.context(b,'ordinary',['云朵开机'],'fixture')['jargon']
        print('PASS insufficient early stop/no extra calls; ordinary-word suppression; prior meaning retained')

        # A constant stream cannot monopolize the expression path. Every ready
        # job goes behind existing work after a stage; chat still wins entirely.
        feed('round-robin');e=claim();submit(e,dict(expressions=[expression_fixture(e['material']['messages'][i],f'r{i}',f'r{i}') for i in range(3)]))
        feed('new-backlog')
        assert store.readiness(b,reply_pending=True)['reason']=='chat_priority'
        t=claim();assert t['stage']=='extract_jargon';submit(t,dict(jargon=[]))
        t=claim();assert t['stage']=='review';submit(t,review_fixture(t))
        t=claim();assert t['stage']=='extract'
        store.cancel(dict(sid=b['sid']))
        rejected('task_unavailable',lambda:submit(t,dict(expressions=[])))
        count=word()['count'];store=LanguageStore(store.path,clock=clock)
        b=dict(b,gid='new-grant',sid='new-session',epoch='new-epoch');store.attach(b)
        assert word()['count']==count
        assert not store.claim(b,'fresh-pipeline-context')
        for scope in ('qq:999:group:222','qq:111:group:223'):
            foreign=dict(b,scope=scope,gid=scope,sid=scope);store.attach(foreign)
            assert store.status(foreign)['jargon_count']==0
        print('PASS bounded fair queue; chat priority; stop/late denial; restart same account+group; cross account/group isolation')
        print(json.dumps(dict(status='passed',model='deterministic fixture',network_calls=0,qq_writes=0,
                              context_isolation=False,source='synthetic peers',claimed_stages=len(phases))))


if __name__=='__main__':main()
