"""Bounded Planner/Replyer protocol for one external Agent. No model calls.

The session lock serializes expressions, not receiving, waiting or stopping.
Freshness is checked again inside each existing adapter's dispatch guard.
"""
from . import reply_timing as timing
import hashlib
import json
import math
import threading
import time
import uuid

from .mcp_actions import dump
from .mcp_schemas import SESSION_ID_DESCRIPTION, TURN_KEY_DESCRIPTION, event_id, idempotency_key, session_id

TOOLS = {'plan_chat_reply', 'send_chat_reply'}
DEFAULTS = dict(quiet_seconds=2., max_collect_seconds=8., reply_cooldown_seconds=3.,
                batch_ttl_seconds=120., plan_ttl_seconds=90., bubble_min_seconds=1.2,
                bubble_max_seconds=4., bubble_seconds_per_char=.035, quote_threshold=3,
                detail_budget_seconds=3.)
RANGES = dict(quiet_seconds=(.1,5), max_collect_seconds=(1,30), reply_cooldown_seconds=(0,60),
              batch_ttl_seconds=(10,300), plan_ttl_seconds=(5,180), bubble_min_seconds=(.1,5),
              bubble_max_seconds=(.1,10), bubble_seconds_per_char=(0,.2),
              quote_threshold=(1,100), detail_budget_seconds=(.1,10))


class TurnRejected(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def schema(tool):
    sid=session_id(SESSION_ID_DESCRIPTION)
    key=idempotency_key(TURN_KEY_DESCRIPTION)
    event=event_id('来自所选 input_batch 的事件 id，不能使用旧批次或 QQ 消息编号。')
    return [tool('plan_chat_reply',
        'Planner 阶段：根据 get/wait 返回的 input_batch.id 决定。target_event_id/topic_event_ids 都来自所选批次；reply 选非空 expressions，wait/silence 不带表达。'
        '程序核对相关消息静默与新鲜度；READY 返回候选，下一次直接 send_chat_reply 同时提交 expression_ids 和 bubbles，选择与表达合为一次决策。只写简短沟通意图，不发进群。',
        dict(session_id=sid,idempotency_key=key,batch_id=sid,
             action=dict(type='string',enum=['wait','silence','reply']),
             target_event_id=event, topic_event_ids=dict(type='array',maxItems=5,uniqueItems=True,items=event),
             topic_terms=dict(type='array',maxItems=6,uniqueItems=True,items=dict(type='string',minLength=2,maxLength=24)),
             expressions=dict(type='array',maxItems=3,uniqueItems=True,description='action=reply 时非空；wait/silence 省略或传 []。',items=dict(type='string',enum=['text','sticker','reaction','voice','video','file','ai_voice','qq_action'])),
             intent=dict(type='string',minLength=1,maxLength=200),
             wait_seconds=dict(type='number',minimum=0,maximum=30)),
        ['session_id','batch_id','action','intent']),
        tool('send_chat_reply',
        'Replyer 阶段：按 READY 计划一次提交 1–3 个语义气泡，不按标点机械拆句。'
        '有学习候选时同时提交 expression_ids（0–5 个候选 ID；[] 明确不用）和 bubbles，无需先调用 select_chat_expressions。兼容此前已单独完成的选择；缺少明确选择仍拒绝发送。'
        '程序串行发送、气泡间留间隔并重新检查相关新消息/停止/授权。STALE 附 updated_chat 时直接重规划，has_more 先分页；否则沿 recovery 补读。已发不撤回，旧尾部不续发；UNKNOWN 禁止重试。'
        '纯 reaction、图片、语音、视频、文件均可结束本轮，不必配文。voice/video/file 气泡使用 import_qq_media 的 asset_id；ai_voice 使用 character_id 和 text；qq_action 使用 tool、action、params。',
        dict(session_id=sid,plan_id=sid,idempotency_key=key,
             expression_ids=dict(type='array',maxItems=5,uniqueItems=True,items=sid,
                                 description='从本计划 learned_language.candidates 选 0–5 个 ID；[] 明确不用。与 bubbles 一起提交，重试保持相同选择。'),
             bubbles=dict(type='array',minItems=1,maxItems=3,items=dict(type='object',additionalProperties=False,
                properties=dict(kind=dict(type='string',enum=['text','sticker','reaction','voice','video','file','ai_voice','qq_action']),
                    text=dict(type='string',minLength=1,maxLength=4000),sticker_id=sid,
                    asset_id=sid,folder_id=dict(type='string',minLength=1,maxLength=512),
                    character_id=dict(type='string',minLength=1,maxLength=200),
                    tool=dict(type='string',enum=['qq_interact','qq_message_action']),
                    action=dict(type='string',minLength=1,maxLength=80),params=dict(type='object'),
                    event_id=event,reaction_id=dict(type='string',maxLength=64),
                    operation=dict(type='string',enum=['add','remove']),
                    quote=dict(type='boolean'),mention_user_ids=dict(type='array',maxItems=5,uniqueItems=True,
                        items=dict(type='string',pattern='^[1-9][0-9]{0,19}$'))),required=['kind']))),
        ['session_id','plan_id','bubbles'])]


class ChatTurns:
    def __init__(self, chat):
        self.chat, self.access = chat, chat.access
        self.epoch=uuid.uuid4().hex
        self.locks={}; self.locks_guard=threading.Lock(); self.dispatch=threading.local()
        with self.access.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS chat_input_batches(
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, epoch TEXT NOT NULL,
                    created REAL NOT NULL, watermark INTEGER NOT NULL, ids TEXT NOT NULL, gap INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS chat_batch_session ON chat_input_batches(session_id,created);
                CREATE TABLE IF NOT EXISTS chat_turn_plans(
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_key TEXT NOT NULL, signature TEXT NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    queue TEXT NOT NULL DEFAULT '[]', queue_signature TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '', UNIQUE(session_id,request_key));
                CREATE INDEX IF NOT EXISTS chat_plan_session ON chat_turn_plans(session_id,created);
            ''')
            # Never resume an old tail automatically. Dispatch uncertainty stays visible.
            for row in db.execute("SELECT * FROM chat_turn_plans WHERE state IN ('READY','SENDING','WAITING')").fetchall():
                queue=json.loads(row['queue'])
                for item in queue:
                    if item['state']=='DISPATCHING': item['state']='UNKNOWN'
                    elif item['state']=='PENDING': item['state']='CANCELLED'
                db.execute('UPDATE chat_turn_plans SET state=?,queue=?,reason=? WHERE id=?',
                    ('UNKNOWN' if any(x['state']=='UNKNOWN' for x in queue) else 'CANCELLED',dump(queue),'service_restarted',row['id']))
            db.execute('DELETE FROM chat_input_batches')

    def policy(self):
        path=self.access.path.parent/'mcp-chat-policy.json'
        try:
            cfg=json.loads(path.read_text('utf-8-sig')) if path.exists() else {}
            if not isinstance(cfg,dict) or set(cfg)-set(DEFAULTS): raise ValueError()
            policy=DEFAULTS|cfg
            for key,value in policy.items():
                low,high=RANGES[key]
                if type(value) not in (float,int) or not math.isfinite(value) or not low<=value<=high: raise ValueError()
            if int(policy['quote_threshold'])!=policy['quote_threshold']:raise ValueError()
            if policy['bubble_max_seconds']<policy['bubble_min_seconds']:raise ValueError()
            return policy
        except (ValueError,OSError):
            raise TurnRejected('chat_policy_invalid','聊天节奏配置无效，请修正本地 mcp-chat-policy.json。') from None

    def lock(self,sid):
        with self.locks_guard:return self.locks.setdefault(sid,threading.Lock())

    def rejected(self,exc,sid=None):
        timing.note(session_id=sid, error_code=exc.code)
        result=dict(state='REJECTED',plan_id=None,error_code=exc.code,note=str(exc),**exc.details)
        refresh={'stale','needs_refresh','batch_expired','plan_expired','target_expired','plan_missing','plan_not_ready'}
        if 'correction_call' in result:
            result['recovery']=dict(action='correct_request',automatic_write_retry=False,next_call=result.pop('correction_call'))
        elif exc.code in refresh:
            result['recovery']=dict(action='refresh_and_replan',automatic_write_retry=False,
                next_call=self.recovery_read(sid))
        elif exc.code in {'needs_wait','reply_cooldown','source_unavailable','queue_busy'}:
            delay=min(30,max(1,exc.details.get('wait_seconds',2)))
            continuation=self.chat.continuation(sid)
            continuation['arguments']['minimum_wait_seconds']=delay
            result['recovery']=dict(action='wait',automatic_write_retry=False,
                retry_after_seconds=delay,next_call=continuation)
        elif exc.code=='expression_selection_required':
            result['recovery']=dict(action='select_expressions',automatic_write_retry=False,next_call=exc.details['next_call'])
        else:
            result['recovery']=dict(action='fix_request_or_stop',automatic_write_retry=False)
        result['recovery_guide']=f"当前规划已被状态机拒绝 ({exc.code})：{str(exc)}。若附带 updated_chat 请直接提取 input_batch 重新调用 plan_chat_reply，或沿 recovery.next_call 执行；严禁在 state!=READY 时调用 send_chat_reply。"
        return result

    def recovery_read(self,sid):
        row=self.chat.row(sid)
        return dict(tool='get_chat_session',arguments=dict(session_id=sid,response_format='compact',
                    known_prompt_version=row.get('prompt_delivered_version','')))

    def quote_guidance(self,p,data):
        target=data.get('target_event_id');threshold=self.policy()['quote_threshold']
        rows=self.data(p['session_id']);index={m['id']:m for m in rows}
        count=None
        if target in index:
            count=len({m['onebot_message_id'] for m in rows if target<m['id']<=data['watermark']
                       and m.get('delivered') and not any(m.get(k) for k in ('is_self','synthetic','recalled'))})
        return dict(target_event_id=target,messages_after_target=count,threshold=threshold,
                    quote_recommended=bool(count is not None and count>=threshold),
                    rule='同一次 Replyer 组织正文时决定首条文字的 quote；间隔达到阈值建议引用，可按指代歧义覆盖。回应整个话题可不引用，不自动@，不为引用另读或等待。')

    @timing.stage('turn.batch')
    def batch(self,row,items,collection=None):
        now=time.time();bid=uuid.uuid4().hex;ids=[m['id'] for m in items]
        timing.note(session_id=row['id'], batch_id=bid, batch_created_at=now,
                    event_ids=ids[:50], collection_reason=(collection or {}).get('reason'))
        with self.access.connect() as db:
            db.execute('INSERT INTO chat_input_batches VALUES(?,?,?,?,?,?,?)',
                       (bid,row['id'],self.epoch,now,max(ids,default=row['cursor']),dump(ids),row['gap_count']))
            db.execute('DELETE FROM chat_input_batches WHERE session_id=? AND id NOT IN (SELECT id FROM chat_input_batches WHERE session_id=? ORDER BY created DESC LIMIT 64)',(row['id'],row['id']))
        # Preserve individual IDs/time/sources; grouping is only a reading aid.
        bursts=[]
        for m in items:
            if not bursts or bursts[-1]['sender_id']!=m['sender_id'] or m['timestamp']-bursts[-1]['last_timestamp']>5000:
                bursts.append(dict(sender_id=m['sender_id'],event_ids=[],last_timestamp=m['timestamp']))
            bursts[-1]['event_ids'].append(m['id']);bursts[-1]['last_timestamp']=m['timestamp']
        return dict(id=bid,version=max(ids,default=row['cursor']),phase='DECIDING',
                    collection=collection or dict(reason='context_read',ready_to_send=False),bursts=bursts,
                    plan_call=dict(tool='plan_chat_reply',arguments=dict(session_id=row['id'],batch_id=bid),
                                   requires=['action','intent'],optional=['target_event_id','expressions']),
                    next='plan_chat_reply：target_event_id/topic_event_ids 仅用本批事件；reply 选表达，wait/silence 不带表达。读取不豁免发送前检查。')

    def status(self,row):
        with self.access.connect() as db:
            p=db.execute('SELECT id,state,reason FROM chat_turn_plans WHERE session_id=? ORDER BY created DESC LIMIT 1',(row['id'],)).fetchone()
            pending=db.execute('SELECT 1 FROM chat_inbox WHERE session_id=? AND id>? LIMIT 1',(row['id'],row['cursor'])).fetchone()
        phase=('STOPPED' if not row['active'] else 'COLLECTING' if row['id'] in self.chat.waiters and pending
               else 'WAITING' if row['id'] in self.chat.waiters else 'REPLYING' if p and p['state']=='READY'
               else 'SENDING' if p and p['state']=='SENDING' else 'DECIDING')
        return dict(phase=phase,last_plan=dict(p) if p else None)

    def waiting(self,sid):
        with self.access.connect() as db:
            p=db.execute("SELECT payload FROM chat_turn_plans WHERE session_id=? AND state='WAITING' ORDER BY created DESC LIMIT 1",(sid,)).fetchone()
        return json.loads(p[0]) if p else None

    def remaining_wait(self,sid):
        p=self.waiting(sid)
        return max(0,p['not_before']-time.time()) if p else 0

    def data(self,sid):
        with self.access.connect() as db:
            rows=db.execute('SELECT id,payload,delivered FROM chat_inbox WHERE session_id=? ORDER BY id',(sid,)).fetchall()
        return [dict(json.loads(r['payload']),id=r['id'],delivered=bool(r['delivered'])) for r in rows]

    @staticmethod
    def related_messages(messages,focus,account):
        peers=[m for m in messages if not m.get('is_self')]
        if focus.get('all'):return peers
        message_ids=set(map(str,focus['message_ids']))
        terms=[term.casefold() for term in focus['terms']]
        following=set();related=[];started=False;participants=set()
        for message in messages:
            sender=message['sender_id'];mid=str(message['onebot_message_id'])
            if mid in message_ids:started=True
            if not started or message.get('is_self'):continue
            replies={str(s.get('onebot_message_id')) for s in message.get('segments',[]) if s.get('type')=='reply'}
            mentions={str(s.get('qq')) for s in message.get('segments',[]) if s.get('type')=='at' and s.get('qq') not in ('all','',None)}
            direct=(mid in message_ids or bool(replies & message_ids) or (sender in participants and account in mentions)
                    or any(term in message.get('content','').casefold() for term in terms))
            if direct:
                participants.add(sender)
                following.add(sender)
            elif replies or mentions:
                following.discard(sender)
                continue
            elif sender not in following:continue
            related.append(message)
            # Preserve reply chains, including another participant quoting a
            # supplement and then adding an unmarked correction of their own.
            message_ids.add(mid)
        return related

    def collection(self,row,pending,quiet,started):
        policy=self.policy();items=self.chat.messages(pending)
        peers=[m for m in items if not m['is_self']]
        if not peers:return dict(ready=True,reason='no_peer_input',remaining_seconds=0)
        # The oldest pending speaker anchors this bounded collection. Unrelated
        # hot-group traffic cannot slide its tail forever. @ is prioritized later.
        anchor=peers[0]
        focus=dict(message_ids=[anchor['onebot_message_id']],terms=[])
        relevant=self.related_messages(peers,focus,row['conversation_id'].split(':')[0])
        remaining=max(0,quiet-(time.time()-max(m['received_at'] for m in relevant)))
        capped=time.monotonic()-started>=policy['max_collect_seconds']
        return dict(ready=remaining==0 or capped,reason='collection_limit' if capped and remaining else 'quiet' if not remaining else 'collecting',
                    remaining_seconds=round(remaining,2),ready_to_send=remaining==0)

    def load(self,pid,sid):
        with self.access.connect() as db:
            r=db.execute('SELECT * FROM chat_turn_plans WHERE id=? AND session_id=?',(pid,sid)).fetchone()
        if not r:raise TurnRejected('plan_missing','计划不属于当前会话，请补读并规划。')
        return dict(r)

    def record_language_context(self,p,context):
        """Audit prepared MCP reference material, never claim host prompt use.

        Keep only IDs/counts/digests in the existing bounded turn receipt. Full
        candidates remain in the short-lived learning store. No new raw chat log.
        """
        decision=context['expression_decision'];complete=context['selection_complete']
        candidates=context.get('candidates',[]);selected=context.get('selected',[])
        material={k:context.get(k,[]) for k in (('selected','jargon') if complete else ('candidates','jargon'))}
        fingerprint=hashlib.sha256(dump(material).encode()).hexdigest()
        with self.access.connect() as db:
            row=db.execute('SELECT payload,state,queue_signature FROM chat_turn_plans WHERE id=? AND session_id=?',
                           (p['id'],p['session_id'])).fetchone()
            if not row:return
            if row['state']!='READY' or row['queue_signature']:
                p['payload']=row['payload'];return
            payload=json.loads(row['payload']);old=payload.get('expression_selection',{})
            audit=dict(state=decision['state'],reason=decision['reason'],candidate_ids=[x['id'] for x in candidates],
                       selected_ids=[x['id'] for x in selected],jargon_ids=[x['id'] for x in context.get('jargon',[])],
                       delivery='tool_result_prepared',host_injection='unverified',model_use='unverified')
            if 'candidate_context' in old:audit['candidate_context']=old['candidate_context']
            key='selected_context' if complete else 'candidate_context'
            prior=old.get(key,{})
            audit[key]=dict(sha256=fingerprint,chars=len(dump(material)),
                            prepared_at=prior.get('prepared_at',time.time()) if prior.get('sha256')==fingerprint else time.time())
            if old!=audit:
                payload['expression_selection']=audit
                db.execute('UPDATE chat_turn_plans SET payload=? WHERE id=?',(dump(payload),p['id']))
            p['payload']=dump(payload)

    @timing.stage('turn.prepare_output')
    def public(self,p):
        data=json.loads(p['payload'])
        timing.note(session_id=p['session_id'], plan_id=p['id'], batch_id=data['batch_id'],
                    batch_created_at=data['read_at'], plan_created_at=p['created'], state=p['state'],
                    target_event_ids=data.get('focus_ids',[])[:5])
        result=dict(plan_id=p['id'],state=p['state'],phase='REPLYING' if p['state']=='READY' else p['state'],
                    decision={k:data[k] for k in ('action','target_event_id','expressions','intent','batch_id','watermark','wait_seconds')},
                    bubbles=json.loads(p['queue']),reason=p['reason'],turn_complete=p['state'] in ('SUCCEEDED','SILENT'),
                    next='同一 Agent 组织表达后调用 send_chat_reply' if p['state']=='READY' else '继续 wait_chat_messages；旧队列尾部不会恢复')
        if p['state']=='READY':
            result['quote_guidance']=self.quote_guidance(p,data)
            result['next_call']=dict(tool='send_chat_reply',arguments=dict(session_id=p['session_id'],plan_id=p['id']),requires=['bubbles'])
            patch=self.chat.apps.plan_context(p)
            if patch:
                result[patch['field']]=patch['context']
                if patch.get('phase'):result['phase']=patch['phase']
                if patch.get('next'):result['next']=patch['next']
                if patch.get('next_call'):result['next_call']=patch['next_call']
        elif p['state']=='WAITING':
            result['next']='暂缓决定，尚未处理完本批；沿 next_call 等待，到期或新输入后重新规划。'
            result['next_call']=self.chat.continuation(p['session_id'])
        elif p['state'] in ('SILENT','SUCCEEDED'):
            result['next_call']=self.chat.continuation(p['session_id'],through=data['watermark'])
        else:
            # Neither a pending/unknown queue nor an interrupted tail is a new
            # write request. Reading the durable receipt is always the next step.
            result['recovery']=dict(action='inspect_result',automatic_write_retry=False,
                next_call=self.recovery_read(p['session_id']))
        audit=json.loads(p['payload']).get('expression_selection')
        if audit:result['expression_selection']=audit
        return result

    def save(self,p,state,queue=None,reason=''):
        with self.access.connect() as db:
            db.execute('UPDATE chat_turn_plans SET state=?,queue=?,reason=?,updated=? WHERE id=?',
                       (state,dump(json.loads(p['queue']) if queue is None else queue),reason,time.time(),p['id']))
        return self.load(p['id'],p['session_id'])

    @timing.stage('turn.freshness_check')
    def check(self,p,cancel,*,cooldown=False):
        data=json.loads(p['payload']);sid=p['session_id']
        _,row=self.chat.validate(sid,data['grant_id'])
        if cancel.is_set():raise TurnRejected('cancelled','本次发送已取消。')
        policy=self.policy();now=time.time()
        if data['epoch']!=self.epoch or row['gap_count']!=data['gap']:
            raise TurnRejected('needs_refresh','实时连接曾中断或缓存缺失，请补读并重新规划。')
        if now-p['created']>policy['plan_ttl_seconds']:
            raise TurnRejected('plan_expired','计划已过期，请补读并重新规划。')
        source=self.chat.receiver.status()
        if source['state']!='connected' or source['account']!=row['conversation_id'].split(':')[0]:
            raise TurnRejected('source_unavailable','实时接收未连接，暂停发言。')
        rows=self.data(sid);indexed={m['id']:m for m in rows}
        for eid in data['focus_ids']:
            m=indexed.get(eid)
            if not m or m.get('recalled') or m.get('synthetic') or not m['delivered']:
                raise TurnRejected('target_expired','目标已失效，请重新读取。')
        focus=data['focus'];account=row['conversation_id'].split(':')[0]
        # Replies to a first bubble are relevant even from a different speaker.
        with self.access.connect() as db:
            queued=db.execute('SELECT queue FROM chat_turn_plans WHERE id=?',(p['id'],)).fetchone()
        if queued:
            for item in json.loads(queued[0]):
                mid=item.get('receipt',{}).get('result',{}).get('message_id')
                if mid:focus['message_ids'].append(str(mid))
        relevant=self.related_messages(rows,focus,account)
        fresh=[m['id'] for m in relevant if m['id']>data['watermark']]
        if fresh:raise TurnRejected('stale','目标相关的新消息尚未纳入决策，请补读并重新规划。',refresh_event_ids=fresh[:12])
        last=max((m['received_at'] for m in relevant),default=data['read_at']-policy['quiet_seconds'])
        remaining=max(last+policy['quiet_seconds'],data['not_before'])-now
        if remaining>0:raise TurnRejected('needs_wait','相关消息仍在收集或计划要求等待；继续 wait 后重规划。',wait_seconds=round(remaining,2))
        if cooldown:
            with self.access.connect() as db:
                prev=db.execute("SELECT max(updated) FROM chat_turn_plans WHERE session_id=? AND id!=? AND queue!='[]'",(sid,p['id'])).fetchone()[0]
            if prev and now-prev<policy['reply_cooldown_seconds']:
                raise TurnRejected('reply_cooldown','上一轮表达刚结束，请留出接话空隙。',wait_seconds=round(policy['reply_cooldown_seconds']-(now-prev),2))

    @timing.stage('turn.plan')
    def plan(self,grant,args,cancel):
        sid=args['session_id'];self.chat.validate(sid,grant['id'])
        args=dict(args)
        if 'idempotency_key' not in args:
            args['idempotency_key']='auto-'+hashlib.sha256(dump(args).encode()).hexdigest()
        lock=self.lock(sid)
        if not lock.acquire(False):return self.rejected(TurnRejected('queue_busy','本会话正在发送，不能并行规划。'),sid)
        try:
            signature=hashlib.sha256(dump(args).encode()).hexdigest()
            with self.access.connect() as db:
                old=db.execute('SELECT * FROM chat_turn_plans WHERE session_id=? AND request_key=?',(sid,args['idempotency_key'])).fetchone()
                batch=db.execute('SELECT * FROM chat_input_batches WHERE id=? AND session_id=?',(args['batch_id'],sid)).fetchone()
            if old:
                if old['signature']!=signature:raise TurnRejected('idempotency_conflict','计划编号已用于另一决定。')
                return self.public(dict(old))
            policy=self.policy();now=time.time()
            if not batch or batch['epoch']!=self.epoch or now-batch['created']>policy['batch_ttl_seconds']:
                raise TurnRejected('batch_expired','读取批次已失效；使用 updated_chat 重规划，未附更新才按 recovery 补读。')
            target=args.get('target_event_id');focus_ids=list(dict.fromkeys(([target] if target else [])+args.get('topic_event_ids',[])))
            correction=dict(correction_call=dict(tool='plan_chat_reply',arguments=dict(session_id=sid,batch_id=batch['id']),
                requires=['action','intent']),valid_event_ids=json.loads(batch['ids']),
                parameter_rule='target_event_id 和 topic_event_ids 仅从 valid_event_ids 选；reply 必须选 expressions，wait/silence 省略或 []；自行修正后提交。')
            if any(eid not in json.loads(batch['ids']) for eid in focus_ids):
                raise TurnRejected('target_not_in_batch','目标或话题消息不在本批，按返回编号修正；无需先读会话。',**correction)
            rows=self.data(sid);index={m['id']:m for m in rows};focus_rows=[index[eid] for eid in focus_ids if eid in index]
            if len(focus_rows)!=len(focus_ids):raise TurnRejected('target_expired','目标已淘汰，请补读。')
            expressions=args.get('expressions',[])
            if (args['action']=='reply')!=bool(expressions):raise TurnRejected('expression_mismatch','reply 选择表达；wait/silence 不提交表达。保留原决定，自行修正参数。',**correction)
            with self.access.connect() as db:
                waiting=db.execute("SELECT payload FROM chat_turn_plans WHERE session_id=? AND state='WAITING' ORDER BY created DESC LIMIT 1",(sid,)).fetchone()
            if waiting and args['action']=='reply':
                previous=json.loads(waiting[0]);delay=previous['not_before']-now
                if delay>0 and batch['watermark']<=previous['watermark']:
                    raise TurnRejected('needs_wait','已选择等待，尚无新输入或到期；继续 wait。',wait_seconds=round(delay,2))
            for kind in expressions:
                owner=self.chat.apps.expression_owner(kind)
                if owner and (denied:=owner.deny(grant,target,self.chat.row(sid))):
                    raise TurnRejected(*denied)
            if 'sticker' in expressions and not grant['scope'].get('chat_sticker_send'):
                raise TurnRejected('sticker_permission','连接未获发送表情包权限。')
            for expression,flag in (('voice','chat_voice'),('ai_voice','chat_voice'),('video','chat_video'),('file','chat_files')):
                if expression in expressions and not grant['scope'].get(flag):raise TurnRejected('attachment_permission','连接没有所选附件权限。')
            if 'qq_action' in expressions and not any(grant['scope'].get(f) for f in ('qq_messages','qq_interactions')):
                raise TurnRejected('qq_permission','连接没有消息操作或互动权限。')
            # Untargeted proactive replies use all peer input as their subject.
            focus=dict(message_ids=[m['onebot_message_id'] for m in focus_rows],
                       terms=args.get('topic_terms',[]),all=not bool(focus_ids))
            row=self.chat.row(sid);pid=uuid.uuid4().hex
            data=dict(action=args['action'],target_event_id=target,focus_ids=focus_ids,focus=focus,
                expressions=expressions,intent=args['intent'],batch_id=batch['id'],watermark=batch['watermark'],
                read_at=batch['created'],epoch=self.epoch,gap=batch['gap'],grant_id=grant['id'],
                wait_seconds=args.get('wait_seconds',0),not_before=now+args.get('wait_seconds',0))
            if args['action']!='reply':
                data['expression_selection']=dict(state='not_applicable',reason='no_reply',delivery='none',
                    candidate_ids=[],selected_ids=[],host_injection='unverified',model_use='unverified')
            candidate=dict(id=pid,session_id=sid,payload=dump(data),created=now)
            if cancel.is_set():raise TurnRejected('cancelled','本次规划已取消。')
            if args['action']=='reply':self.check(candidate,cancel,cooldown=True)
            state={'reply':'READY','wait':'WAITING','silence':'SILENT'}[args['action']]
            with self.access.connect() as db:
                db.execute("UPDATE chat_turn_plans SET state='CANCELLED',reason='replanned' WHERE session_id=? AND state IN ('READY','WAITING')",(sid,))
                db.execute('INSERT INTO chat_turn_plans(id,session_id,request_key,signature,payload,state,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                    (pid,sid,args['idempotency_key'],signature,dump(data),state,now,now))
                # Retain uncertainty indefinitely; ordinary closed turn text is
                # bounded independently of immutable native operation receipts.
                db.execute("DELETE FROM chat_turn_plans WHERE session_id=? AND state IN ('SILENT','CANCELLED','SUCCEEDED','INTERRUPTED') AND id NOT IN (SELECT id FROM chat_turn_plans WHERE session_id=? ORDER BY created DESC LIMIT 200)",(sid,sid))
            return self.public(self.load(pid,sid))
        except TurnRejected as exc:return self.rejected(exc,sid)
        finally:lock.release()

    def before_dispatch(self):
        current=getattr(self.dispatch,'current',None)
        if current:self.check(current[0],current[1])

    @timing.stage('turn.submit')
    def submit(self,grant,args,cancel):
        sid=args['session_id'];self.chat.validate(sid,grant['id'])
        if not args.get('plan_id'):return self.rejected(TurnRejected('plan_required','先用最新 input_batch 调用 plan_chat_reply；不能跳过 Planner 直接发送。'),sid)
        lock=self.lock(sid)
        if not lock.acquire(False):return self.rejected(TurnRejected('queue_busy','同一会话已有发送队列；稍后以原计划查询，勿换编号重发。'),sid)
        try:
            p=self.load(args['plan_id'],sid);data=json.loads(p['payload']);bubbles=args['bubbles']
            submitted=dict(bubbles=bubbles,expression_ids=args['expression_ids']) if 'expression_ids' in args else bubbles
            signature=hashlib.sha256(dump(submitted).encode()).hexdigest()
            if p['queue_signature']:
                if p['queue_signature']!=signature:raise TurnRejected('idempotency_conflict','该计划已经提交了另一组表达，不能替换或重发。')
                return self.public(p)
            if p['state']!='READY':raise TurnRejected('plan_not_ready','该计划不可发送，请读取结果或补读重规划。')
            if not isinstance(bubbles,list) or not 1<=len(bubbles)<=3:raise TurnRejected('invalid_bubbles','一次提交 1–3 个语义气泡。')
            for i,b in enumerate(bubbles):
                kind=b['kind'];allowed={'text':{'kind','text','quote','mention_user_ids'},'sticker':{'kind','sticker_id'},'reaction':{'kind','event_id','reaction_id','operation'},
                    'voice':{'kind','asset_id'},'video':{'kind','asset_id'},'file':{'kind','asset_id','folder_id'},
                    'ai_voice':{'kind','text','character_id'},'qq_action':{'kind','tool','action','params'}}[kind]
                if set(b)-allowed or kind not in data['expressions']:raise TurnRejected('expression_mismatch','表达类型或字段与计划不一致。')
                required={'text':{'text'},'sticker':{'sticker_id'},'reaction':{'event_id','reaction_id','operation'},
                    'voice':{'asset_id'},'video':{'asset_id'},'file':{'asset_id'},'ai_voice':{'text','character_id'},'qq_action':{'tool','action','params'}}[kind]
                if required-set(b):raise TurnRejected('expression_mismatch','表达缺少必要内容。')
                if kind=='reaction' and b['event_id']!=data['target_event_id']:raise TurnRejected('target_mismatch','回应目标与计划不一致。')
                if kind=='text' and i and (b.get('quote') or b.get('mention_user_ids')):raise TurnRejected('repeated_addressing','同一轮仅首条按需引用或 @；改变目标需重新规划。')
                if kind=='qq_action':
                    from .mcp_qq_catalog import CATALOG
                    from .mcp_qq import OUTBOUND
                    import jsonschema
                    spec=CATALOG.get(b['tool'],{});entry=spec.get('actions',{}).get(b['action'])
                    if not entry or b['action'] not in OUTBOUND or not grant['scope'].get(spec.get('permission')):
                        raise TurnRejected('qq_action_invalid','聊天队列只接受已授权的发送或互动动作。')
                    if not jsonschema.Draft202012Validator(entry['params']).is_valid(b['params']):
                        raise TurnRejected('qq_action_invalid','动作参数格式无效，请按对应工具的参数定义修正。')
                if kind in ('voice','video','file'):self.chat.qq.media.get(grant,b['asset_id'],kind)
                if kind=='ai_voice' and ':direct:' in self.chat.row(sid)['conversation_id']:
                    raise TurnRejected('ai_voice_group_only','QQ AI 语音只支持群聊。')
            self.check(p,cancel,cooldown=True)
            if 'expression_ids' in args:
                if not any(b['kind']=='text' for b in bubbles):
                    if args['expression_ids']:
                        raise TurnRejected('expression_mismatch','纯媒体表达不选择文字参考。')
                else:
                    patch=self.chat.apps.plan_context(p)
                    context=patch['context'] if patch else {}
                    if context.get('expression_decision',{}).get('state') in ('not_applicable','unavailable'):
                        # Paused/unavailable learning must not block ordinary
                        # chat when a host consistently submits an empty list.
                        if args['expression_ids']:raise TurnRejected('invalid_selection','本轮没有可选择的表达参考。')
                    else:
                        with timing.span('learning.inline_selection'):
                            refusal=self.chat.apps.select(grant,p,args['expression_ids'])
                        if refusal:raise TurnRejected(*refusal)
                    p=self.load(p['id'],sid)
                    data=json.loads(p['payload'])
            if any(b['kind']=='text' for b in bubbles):
                patch=self.chat.apps.plan_context(p)
                if patch and patch['context'].get('selection_required'):
                    raise TurnRejected('expression_selection_required','send_chat_reply 同时提交 expression_ids（[] 明确不用）和 bubbles；也兼容先调用选择工具。尚未发送。',
                        next_call=patch['next_call'],learned_language=patch['context'])
            elif 'text' in data['expressions']:
                # A mixed plan may ultimately use only a sticker/reaction. Keep
                # the evidence of material already offered, but do not demand a
                # redundant selection or leave a successful turn marked pending.
                data=json.loads(p['payload'])
                if 'expression_selection' in data:
                    data['expression_selection'].update(state='not_applicable',reason='non_text_output')
                    p['payload']=dump(data)
                    with self.access.connect() as db:db.execute('UPDATE chat_turn_plans SET payload=? WHERE id=?',(p['payload'],p['id']))
            queue=[dict(index=i,expression=b,state='PENDING') for i,b in enumerate(bubbles)]
            with self.access.connect() as db:db.execute('UPDATE chat_turn_plans SET queue_signature=? WHERE id=?',(signature,p['id']))
            p=self.save(p,'SENDING',queue)
            self.dispatch.current=(p,cancel)
            try:
                for i,item in enumerate(queue):
                    try:
                        if i:
                            cfg=self.policy();previous=bubbles[i-1].get('text','')
                            delay=min(cfg['bubble_max_seconds'],cfg['bubble_min_seconds']+len(previous)*cfg['bubble_seconds_per_char'])
                            until=time.monotonic()+delay
                            with timing.span('send.bubble_interval'):
                                while time.monotonic()<until:
                                    self.check(p,cancel)
                                    cancel.wait(min(.1,max(0,until-time.monotonic())))
                        self.check(p,cancel)
                        call=self.access.begin_call(grant['id'],'send_chat_sticker' if item['expression']['kind']=='sticker' else 'chat_expression')
                        b=item['expression'];key='turn-'+p['id']+'-'+str(i)
                        item['state']='DISPATCHING';self.save(p,'SENDING',queue)
                        expression_started=time.monotonic()
                        try:
                            if b['kind']=='text':
                                params=dict(session_id=sid,idempotency_key=key,**{k:v for k,v in b.items() if k!='kind'})
                                if data['target_event_id']:params['reply_to_event_id']=data['target_event_id']
                                result=self.chat.send(grant,params,cancel)
                            elif b['kind']=='sticker':
                                result,_=self.chat.media.call(grant,'send_chat_sticker',dict(session_id=sid,idempotency_key=key,sticker_id=b['sticker_id']),cancel)
                            elif owner:=self.chat.apps.expression_owner(b['kind']):
                                # 表达型插件：一个表达对应一个写入工具，工具名取自插件注册表。
                                result=owner.dispatch(grant,next(iter(owner.tool_names)),
                                    dict(session_id=sid,idempotency_key=key,**{k:v for k,v in b.items() if k!='kind'}),cancel)
                            else:
                                result=self.chat.qq.send_bubble(grant,self.chat.row(sid),b,key,cancel)
                            item['state']=result.get('state','UNKNOWN');item['receipt']=result
                            if item['state'] not in ('SUCCEEDED','NOOP'):
                                try:self.check(p,cancel)
                                except ValueError as exc:p['reason']=getattr(exc,'code','session_invalid')
                        except TurnRejected:
                            item['state']='CANCELLED'
                            raise
                        except ValueError:
                            item['state']='FAILED'
                            raise
                        except Exception:
                            item['state']='UNKNOWN'
                            raise
                        finally:
                            self.access.finish_call(call,item['state'].lower(),time.monotonic()-expression_started,0)
                            timing.note(plan_id=p['id'], bubble_index=i, state=item['state'],
                                        expression_seconds=round(time.monotonic()-expression_started,6))
                        self.save(p,'SENDING',queue)
                        if item['state'] not in ('SUCCEEDED','NOOP'):break
                    except (TurnRejected,ValueError) as exc:
                        item['state']='CANCELLED' if item['state']=='PENDING' else item['state']
                        p['reason']=getattr(exc,'code','session_invalid');break
                    except Exception:
                        p['reason']='dispatch_unknown';break
                for item in queue:
                    if item['state']=='PENDING':item['state']='CANCELLED'
                state='SUCCEEDED' if all(x['state'] in ('SUCCEEDED','NOOP') for x in queue) else 'UNKNOWN' if any(x['state']=='UNKNOWN' for x in queue) else 'INTERRUPTED'
                return self.public(self.save(p,state,queue,p.get('reason','')))
            finally:self.dispatch.current=None
        except TurnRejected as exc:
            if 'p' in locals() and exc.code in ('stale','needs_refresh','plan_expired','target_expired'):
                self.save(p,'CANCELLED',reason=exc.code)
            return self.rejected(exc,sid)
        finally:lock.release()

    def single(self,grant,name,args,cancel):
        kinds={'send_chat_message':'text','send_chat_sticker':'sticker'}
        kinds.update({tool:owner.expression for owner in self.chat.apps.all()
                      if owner.expression for tool in owner.tool_names})
        bubble={'kind':kinds[name],**{k:v for k,v in args.items() if k not in ('session_id','plan_id','idempotency_key','reply_to_event_id','expression_ids')}}
        if args.get('reply_to_event_id') and args.get('plan_id'):
            p=self.load(args['plan_id'],args['session_id'])
            if json.loads(p['payload'])['target_event_id']!=args['reply_to_event_id']:
                return dict(state='REJECTED',error_code='target_mismatch',note='回应对象与计划不同。')
        outcome=self.submit(grant,dict(session_id=args['session_id'],plan_id=args.get('plan_id'),
                          idempotency_key=args['idempotency_key'],bubbles=[bubble],
                          **({'expression_ids':args['expression_ids']} if 'expression_ids' in args else {})),cancel)
        queue=outcome.get('bubbles',[])
        if len(queue)==1 and queue[0].get('receipt'):
            return dict(queue[0]['receipt'],chat_turn=outcome)
        return outcome
