"""Timing isolation, secret exclusion, rotation and failure transparency."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from chatlocal import reply_timing as timing


def main():
    secret = 'PRIVATE-TOKEN-CHAT-URL'
    @timing.trace('mcp.tool', tool=True)
    def call(name, arguments, token):
        with timing.span('outer'):
            with timing.span('onebot.http_roundtrip'):
                timing.note(plan_id=arguments['plan_id'])
            for _ in range(700):
                with timing.span('wait.event_sleep'):
                    pass
            with timing.span('after_long_wait'):
                pass
        if arguments.get('fail'):
            raise ValueError(secret)
        return dict(state='SUCCEEDED', text=secret, url=secret)

    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'timing.jsonl'
        with patch.object(timing,'PATH',path):
            def run(i):
                return call('send_chat_reply',dict(plan_id=str(i),text=secret),secret)
            with ThreadPoolExecutor(4) as pool:
                assert all(x['state']=='SUCCEEDED' for x in pool.map(run,range(12)))
            rows=[json.loads(line) for line in path.read_text('utf-8').splitlines()]
            assert len(rows)==12 and len({r['trace_id'] for r in rows})==12
            for row in rows:
                assert row['marks'][0]['plan_id']==row['links']['plan_id']
                assert row['polling']['wait.event_sleep']['count']==700
                assert any(x['name']=='after_long_wait' for x in row['spans'])
                assert all(x['seconds']>=0 for x in row['spans'])
                assert next(x for x in row['spans'] if x['name']=='onebot.http_roundtrip')['parent']==1
            try:
                call('send_chat_reply',dict(plan_id='failure',fail=True),secret)
            except ValueError as exc:
                assert str(exc)==secret
            else:
                raise AssertionError('exception swallowed')
            assert secret not in path.read_text('utf-8')
            assert json.loads(path.read_text('utf-8').splitlines()[-1])['error_type']=='ValueError'
            with patch.object(timing,'MAX_BYTES',1):
                run(20);run(21)
            assert path.with_suffix('.jsonl.1').exists() and path.with_suffix('.jsonl.2').exists()
        # A non-directory parent simulates an unwritable diagnostics destination.
        blocked=Path(temp)/'blocked';blocked.write_text('fixture')
        with patch.object(timing,'PATH',blocked/'timing.jsonl'):
            assert run(30)['state']=='SUCCEEDED'
    print('PASS: isolated concurrent traces, no secrets, nested timing, long-poll aggregation, rotation, original exceptions and unwritable-log safety.')
    from scripts.reply_timing_report import report
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'report.jsonl'
        def row(tool,start,end,marks,result,spans=()):
            return dict(trace_id=tool,tool=tool,started_at=start,finished_at=end,seconds=end-start,
                        marks=marks,result=result,spans=list(spans),links={'session_id':'fixture'},status='returned')
        rows=[row('wait_chat_messages',10,12,[dict(batch_created_at=11,batch_id='batch',event_ids=[1])],
                  dict(last_received_at=9,first_received_at=8)),
              row('plan_chat_reply',14,15,[],{}),
              row('send_chat_reply',18,22,[dict(batch_id='batch',batch_created_at=11,plan_created_at=14)],
                  dict(state='SUCCEEDED'),[dict(name='onebot.send_group_msg',start=19,seconds=1)])]
        path.write_text('\n'.join(json.dumps(r) for r in rows)+'\n{"partial":',encoding='utf-8')
        result=report(path)
        assert result['malformed_lines']==1
        assert result['recent_host_gaps'][0]['seconds']==2
        turn=result['recent_turns'][0]
        assert turn['batch_to_send_call']==7 and turn['plan_to_send_call']==4
        assert turn['send_call_to_first_http_receipt']==2
        assert turn['last_batch_receive_to_first_http_receipt']==11
    print('PASS: report correlation, host gaps, first receipt boundaries and partial-line tolerance.')
    # The old aggregate "image.dns" mixed system DNS with the Fake-IP
    # fallback. Exercise both paths without real DNS or private image URLs.
    import socket
    from types import SimpleNamespace
    from check_mcp_chat_media import dns_response
    from chatlocal.mcp_chat_media import public_addresses
    @timing.trace('dns.fixture')
    def resolve():return public_addresses('fixture.qq.com',443)
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'dns.jsonl'
        public=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('175.27.35.211',443))]
        fake=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('198.18.0.97',443))]
        with patch.object(timing,'PATH',path):
            with patch('dns.resolver.Resolver.resolve_name',return_value=SimpleNamespace(addresses=lambda:['175.27.35.211'])):assert resolve()==public
            response=dns_response('fixture.qq.com','175.27.35.211')
            with patch('dns.resolver.Resolver.resolve_name',return_value=SimpleNamespace(addresses=lambda:['198.18.0.97'])),patch('dns.query.tls',return_value=response):
                assert resolve()==public
        direct,fallback=[json.loads(line) for line in path.read_text('utf-8').splitlines()]
        assert {s['name'] for s in direct['spans']}=={'dns.fixture','image.dns','image.dns.system_lookup'}
        assert direct['marks'][0]['dns_fake_ip_detected'] is False
        spans={s['name']:s for s in fallback['spans']}
        assert 'image.dns.tls_query' in spans
        assert fallback['marks'][0]['dns_fake_ip_detected'] is True and fallback['marks'][-1]['dns_tls_record_count']==1
        assert spans['image.dns.tls_query']['parent']==next(i for i,s in enumerate(fallback['spans']) if s['name']=='image.dns')
        assert 'fixture.qq.com' not in path.read_text('utf-8') and 'https://' not in path.read_text('utf-8')
    print('PASS: configured DNS vs Fake-IP fallback stages, nested TLS timing and no host/URL persistence; no real network calls.')


if __name__=='__main__':main()
