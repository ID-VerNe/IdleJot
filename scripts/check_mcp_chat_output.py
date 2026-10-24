"""Compact polling preserves evidence, cursors and version refresh semantics."""
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from chatlocal.mcp_chat_output import compact_wait, compact_recovery, compact_sent


def main():
    message=dict(id=9,content='完整正文不能因为压缩辅助信息而截断。'*1000,
                 segments=[dict(type='image',image_index=0),dict(type='at',qq='333')])
    packet=dict(event='messages',messages=[message],read_through_id=9,has_more=True,decision_required=True,
        receiver=dict(state='connected',account='111',note='status'),
        input_batch=dict(id='batch',phase='DECIDING',bursts=[dict(event_ids=[9])],
                         plan_call=dict(tool='plan_chat_reply',arguments=dict(batch_id='batch'),requires=['action','intent'])),
        next_call=dict(tool='wait_chat_messages',arguments=dict(acknowledge_through_id=9)),
        chat_guidance=dict(prompt_version='v1',persona_name='fixture',capabilities=dict(chat_images=True),
            reminders=['cached rules'*1000],reactions=dict(candidates=['cached directory'*1000]),
            social_context=dict(observed_messages=1,may_be_unfinished=True,image_events=[dict(event_id=9,index=0)]),
            response_targets=dict(items=[dict(event_id=i) for i in range(1,10)]),
            reaction_notices=dict(items=[dict(notice_id=1)],new_items=[dict(notice_id=1)]),
            familiar_stickers=[dict(sticker_id='checked-sticker',sendable=True)],
            image_reads=[dict(tool='read_chat_image',arguments=dict(event_id=9,index=0))]))
    before=deepcopy(packet);out=compact_wait(packet)
    assert packet==before and out['messages']==before['messages']
    assert out['read_through_id']==9 and out['has_more'] and out['next_call']==packet['next_call']
    assert out['input_batch']==packet['input_batch']
    deciding=dict(packet,has_more=False,next_call=packet['input_batch']['plan_call'])
    assert compact_wait(deciding)['next_call']==packet['input_batch']['plan_call']
    assert 'acknowledge_through_id' not in compact_wait(deciding)['next_call']['arguments']
    assert out['chat_guidance']['reaction_notices']['new_items']==packet['chat_guidance']['reaction_notices']['new_items']
    assert out['chat_guidance']['image_reads']==packet['chat_guidance']['image_reads']
    assert out['chat_guidance']['familiar_stickers']==packet['chat_guidance']['familiar_stickers']
    assert all(t['event_id']<=9 for t in out['chat_guidance']['response_targets']['items'])
    packet.update(event='idle',messages=[],decision_required=False,has_more=False)
    idle=compact_wait(packet)
    deferred=compact_wait(dict(packet,deferred_until=12345))
    assert deferred['deferred_until']==12345 and '暂缓决定' in deferred['chat_guidance']['reminders'][0]
    assert len(json.dumps(idle,ensure_ascii=False))<1400
    assert idle['chat_guidance']['reaction_notices']==packet['chat_guidance']['reaction_notices']
    refresh=dict(packet,chat_prompt=dict(behavior='new behavior',persona='immutable snapshot'),prompt_changed=True)
    assert compact_wait(refresh)['chat_prompt']==refresh['chat_prompt']
    known=compact_wait(before,before['chat_guidance']['prompt_version'])
    assert known['messages']==before['messages'] and known['next_call']==before['next_call']
    assert 'capabilities' not in known['chat_guidance'] and 'persona_name' not in known['chat_guidance']
    assert 'capabilities' in compact_wait(before,'old')['chat_guidance']
    offline=dict(packet,event='source_unavailable',receiver=dict(state='account_mismatch',note='identity mismatch'))
    assert compact_wait(offline)['receiver']==offline['receiver']
    recovery=dict(session=dict(id='s'*32,prompt_version='v1',persona='persona snapshot',cursor=2,offered=9),
        instructions='static protocol',context=dict(messages=before['messages'],has_more=False),input_batch=before['input_batch'],
        chat_prompt=dict(before['chat_guidance'],persona='persona snapshot',behavior='static behavior'),
        recent_operations=[dict(id='op',state='UNKNOWN')],
        recent_turns=[dict(plan_id='plan',state='UNKNOWN',bubbles=[dict(index=0,state='UNKNOWN',
            expression=dict(text='submitted text'),receipt=dict(id='op',state='UNKNOWN'))])])
    original=deepcopy(recovery);out=compact_recovery(recovery,'v1')
    assert recovery==original and out['context']==original['context']
    assert out['session']['cursor']==2 and out['session']['offered']==9
    assert 'persona' not in out['session'] and 'persona' not in out['chat_prompt'] and 'instructions' not in out
    assert out['recent_operations']==recovery['recent_operations']
    assert out['recent_turns'][0]['bubble_states'][0]['receipt']['state']=='UNKNOWN'
    assert out['recent_turns'][0]['state']=='UNKNOWN' and out['next_call']==recovery['input_batch']['plan_call']
    refreshed=compact_recovery(recovery,'old')
    assert refreshed['prompt_changed'] and refreshed['chat_prompt']['persona']=='persona snapshot'
    assert refreshed['chat_prompt']['behavior']=='static behavior' and refreshed['instructions']=='static protocol'
    assert 'capabilities' in refreshed['chat_prompt'] and 'capabilities' not in out['chat_prompt']
    completed=dict(id='ok',kind='send',state='SUCCEEDED',summary='old sent text'*1000,
        result=dict(message_id=123,content='old sent text'*1000))
    turn=dict(plan_id='done',state='SUCCEEDED',turn_complete=True,decision=dict(action='reply',target_event_id=9,batch_id='old',watermark=9,intent='old intent'),
        expression_selection=dict(material='old choice'*1000),learned_language=dict(candidates=['old choice'*1000]),
        bubbles=[dict(index=0,state='SUCCEEDED',expression={'text':'old sent text'*1000},receipt=completed)])
    recovery['recent_operations'].append(completed);recovery['recent_turns'].append(turn)
    working=dict(plan_id='working',state='SENDING',bubbles=[dict(index=0,state='DISPATCHING',expression={'text':'queued'},receipt={'state':'UNKNOWN'})])
    recovery['recent_turns'].append(working)
    original=deepcopy(recovery);out=compact_recovery(recovery,'v1')
    assert recovery==original and out['context']['messages']==original['context']['messages']
    success=out['recent_turns'][1]
    assert success['bubble_states']==[dict(index=0,state='SUCCEEDED',operation_id='ok')]
    assert 'expression_selection' not in success and 'learned_language' not in success and 'intent' not in success['decision']
    assert 'result' not in out['recent_operations'][1] and 'old sent text' not in json.dumps(out)
    assert out['recent_turns'][0]['bubble_states']==original['recent_turns'][0]['bubbles']
    assert out['recent_turns'][2]['bubble_states']==working['bubbles']
    # Size assertion is a fixture regression, not a production character cap or latency benchmark.
    assert len(json.dumps(out['recent_turns'][1]))<len(json.dumps(turn))/10
    assert len(json.dumps(out['recent_operations'][1]))<len(json.dumps(completed))/10
    receipt=dict(id='op',kind='send',state='SUCCEEDED',note='固定说明',summary='完整摘要',result=dict(message_id=123,timestamp=123,
        content='实际发送正文',message_segments=[{'type':'text','data':{'text':'实际发送正文'}}],note='固定说明',display_note='固定显示说明'))
    sent=dict(state='SUCCEEDED',plan_id='plan',bubbles=[dict(state='SUCCEEDED',expression={'text':'实际发送正文'},receipt=receipt)],next_call={'tool':'wait_chat_messages'})
    original=deepcopy(sent);brief=compact_sent(sent)
    assert sent==original and brief['next_call']==sent['next_call'] and brief['bubbles'][0]['expression']==sent['bubbles'][0]['expression']
    assert brief['bubbles'][0]['receipt']['result']=={k:v for k,v in receipt['result'].items() if k not in ('note','display_note')}
    assert brief['bubbles'][0]['receipt']['summary']==receipt['summary'] and 'note' not in brief['bubbles'][0]['receipt']
    for state in ('UNKNOWN','INTERRUPTED','REJECTED','WAITING','SENDING'):
        uncertain=dict(sent,state=state);assert compact_sent(uncertain)==uncertain
    other=deepcopy(sent);other['bubbles'][0]['receipt']['kind']='reaction'
    assert compact_sent(other)==other
    print('PASS: no message truncation or source mutation; ACK/pagination, media targets, notices, offline identity and full version refresh preserved; idle <1400 chars.')
    print('PASS: compact recovery preserves messages, cursors and UNKNOWN audit; unchanged static context omitted, full version refresh retained.')


if __name__=='__main__':main()
