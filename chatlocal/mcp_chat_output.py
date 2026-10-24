"""Compact live-chat wire packets; original data and full reads stay intact.

Static behavior, persona and capability catalogs are delivered by start/get.
Polling sends dynamic state only, with no message truncation or cursor changes.
"""
from .mcp_chat_hints import DOSSIER_REMINDER, habit_hints


def compact_wait(data,known_prompt_version=None,brief=None):
    if not isinstance(data,dict) or 'chat_guidance' not in data:return data
    full=data['chat_guidance'];state=full.get('social_context',{});event=data.get('event')
    active=bool(data.get('decision_required'))
    hints=[]
    if event=='source_unavailable':hints.append('来源断开，继续等待恢复；不要发言。')
    elif data.get('deferred_until'):hints.append('本批暂缓决定，到期会重新交付；直接 next_call 等待，不确认处理完成。')
    elif data.get('has_more'):hints.append('还有未读消息，先接着读再决定，不把一页当全部。')
    elif brief:hints.append(brief)
    elif not active:hints.append('没有新消息时直接 next_call 继续等待，不提交空规划、不结束群聊。')
    else:hints.append('先判断对谁说；plan_chat_reply 后直接 send_chat_reply，一并提交 expression_ids 与 bubbles。首条文字气泡带 "quote": true 开启 QQ 原生引用回复精准指向该群友。')
    if active and state.get('may_be_unfinished'):hints.append('最后一句可能没说完，先等短窗口听后续。')
    if active and state.get('consecutive_own_messages',0)>=2:hints.append('自己的话较密，先让别人接；被追问时仍可回应。')
    if active:
        hints.extend(habit_hints(state, brief=True))
    if state.get('image_events'):hints.append('先看图片与 materials 映射，按 materials.note 区分失败补读与客户端图片接入限制。')
    guidance={k:full[k] for k in ('prompt_version','persona_name','capabilities') if k in full}
    if known_prompt_version and known_prompt_version==full.get('prompt_version'):
        guidance.pop('persona_name',None);guidance.pop('capabilities',None)
    guidance['reminders']=hints
    if active:
        guidance['social_context']={k:v for k,v in state.items() if k not in ('observation','window_seconds')}
        targets=full.get('response_targets',{}).get('items',[])
        guidance['response_targets']=dict(items=targets[-3:])
        if full.get('image_reads'):guidance['image_reads']=full['image_reads']
        if full.get('familiar_stickers'):guidance['familiar_stickers']=full['familiar_stickers']
    notices=full.get('reaction_notices',{})
    # Passive add/remove observations can accompany an idle packet without
    # requesting a reply. They are evidence, not redundant static guidance.
    guidance['reaction_notices']={k:notices.get(k,[]) for k in ('items','new_items')}
    learning=full.get('language_learning')
    if learning:guidance['language_learning']={k:learning[k] for k in ('enabled','automatic') if k in learning}
    if 'member_dossiers' in full:
        guidance['member_dossiers']=full['member_dossiers']
        guidance['reminders'].append(DOSSIER_REMINDER)
    result={k:v for k,v in data.items() if k not in ('chat_guidance','note')}
    result['chat_guidance']=guidance
    if 'receiver' in result:
        receiver=result['receiver']
        result['receiver']={k:v for k,v in receiver.items() if k in ('state','account') or (k=='note' and receiver.get('state')!='connected')}
    if 'input_batch' in result:
        batch=dict(result['input_batch'])
        batch.pop('next',None)
        if 'plan_call' in batch:
            batch['plan_call']={k:v for k,v in batch['plan_call'].items() if k!='optional'}
        result['input_batch']=batch
        if result.get('next_call',{}).get('tool')=='plan_chat_reply':
            result['next_call']=batch['plan_call']
    # Full version-refresh packets are deliberately kept: compactness must not
    # silently leave a host following old behavior or an outdated persona.
    return result


def compact_recovery(data,known_prompt_version=None):
    """Keep live recovery evidence; completed receipts have full-read IDs."""
    result=dict(data);session=dict(data['session']);prompt=data.get('chat_prompt',{})
    changed=not known_prompt_version or known_prompt_version!=session.get('prompt_version')
    result['prompt_changed']=changed
    session.pop('persona',None)  # Full refreshed persona is in chat_prompt once.
    if not changed:
        result.pop('instructions',None)
        dynamic=compact_wait(dict(event='messages',decision_required=True,chat_guidance=prompt),known_prompt_version)['chat_guidance']
        result['chat_prompt']=dynamic
    result['session']=session
    result['full_read']=dict(tool='get_chat_session',arguments=dict(session_id=session['id'],response_format='full'))
    context=result.get('context',{})
    result['next_call']=context.get('next_call') if context.get('has_more') else result.get('input_batch',{}).get('plan_call') if context.get('messages') else prompt.get('continue_with')
    if 'recent_operations' in result:
        result['recent_operations']=[dict({k:op[k] for k in ('id','kind','state','created','updated') if k in op},
            summary='已成功；完整回执见 full。') if op['state']=='SUCCEEDED' else op for op in result['recent_operations']]
    turns=[]
    for turn in result.get('recent_turns',[]):
        if turn['state'] in ('SUCCEEDED','SILENT'):
            brief={k:turn[k] for k in ('plan_id','state','reason','turn_complete') if k in turn}
            brief['decision']={k:v for k,v in turn.get('decision',{}).items() if k in ('action','target_event_id','batch_id','watermark')}
            brief['bubble_states']=[dict({k:b[k] for k in ('index','state') if k in b},
                **({'operation_id':b['receipt']['id']} if b.get('receipt',{}).get('id') else {})) for b in turn.get('bubbles',[])]
            brief['summary']='本轮已结束，不重发；完整记录见 full。'
        else:
            brief={k:v for k,v in turn.items() if k not in ('next','bubbles')}
            brief['bubble_states']=turn.get('bubbles',[])
        turns.append(brief)
    if 'recent_turns' in result:result['recent_turns']=turns
    result['recovery_note']='直接使用 context 和 input_batch 恢复判断；无需先读取完整结果文件。完整人格仅在 prompt_changed 时刷新。成功旧记录已精简；UNKNOWN、进行中及中断队列仅供核对，不重发或恢复旧尾部，完整记录见 full。'
    return result


def compact_sent(data):
    """Only completed send notes are redundant; full session reads keep them."""
    if data.get('state')!='SUCCEEDED' or 'bubbles' not in data:return data
    result=dict(data);bubbles=[]
    for bubble in data['bubbles']:
        item=dict(bubble)
        if (item.get('state')=='SUCCEEDED' and item.get('receipt',{}).get('state')=='SUCCEEDED'
                and item['receipt'].get('kind')=='send'):
            receipt={k:v for k,v in item['receipt'].items() if k!='note'}
            if isinstance(receipt.get('result'),dict):
                receipt['result']={k:v for k,v in receipt['result'].items() if k not in ('note','display_note')}
            item['receipt']=receipt
        bubbles.append(item)
    result['bubbles']=bubbles
    return result
