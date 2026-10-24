"""The blocking wait loop for persistent chat sessions (issue #11, section PC).

Split out of ``mcp_chat.py``: admission control, explicit cursor ACK, collection
policy, deferred turn plans, learning wake-up and page/timeout boundaries --
the whole ``wait`` concern, provided as a mixin so ``MCPChat`` keeps one flat
public surface. ``ChatStopped`` and the water-group config loader stay on
``mcp_chat`` (raised and used by the session lifecycle that remains there) and
are imported per call -- the same cycle break this package already uses for
``ChatMedia``.
"""
import json
import threading
import time

from . import reply_timing as timing
from . import mcp_chat_prompts as prompts
from .mcp_chat_limits import WAIT_TIMEOUT_SECONDS


class ChatWaitMixin:
    def wait(self, grant, args, cancel, *, allow_learning=True):
        from .mcp_chat import ChatStopped, load_water_group_config
        sid=args['session_id'];gid=grant['id']
        grant,row=self.validate(sid,gid,active=False)
        if not row['active']:return dict(event='stopped',session=self.public(row),messages=[])
        self.receiver.start()
        with self.lock:
            if sid in self.waiters:raise ValueError('此会话已有 Agent 在等待；请接续原会话，不要并行回复同一个群。')
            if len(self.waiters)>=8:raise ValueError('同时等待的群已达8个；普通工具仍可使用。')
            event=threading.Event();self.events[sid]=event;self.waiters.add(sid)
        try:
            grant,row=self.validate(sid,gid)
            ack=args.get('acknowledge_through_id')
            if ack is not None:
                if not 0<=ack<=row['offered']:raise ValueError('只能确认已返回的消息进度，请使用 read_through_id。')
                if ack>=row['cursor']:
                    with self.access.connect() as db:
                        # A deferred decision is not an ACK, including when an
                        # older client reuses the batch's original continuation.
                        db.execute('BEGIN IMMEDIATE')
                        if db.execute("SELECT 1 FROM chat_turn_plans WHERE session_id=? AND state='WAITING'",(sid,)).fetchone():
                            ack=row['cursor']
                        db.execute('UPDATE chat_sessions SET cursor=?,note=?,updated=? WHERE id=? AND active=1',
                                   (ack,args.get('note',row['note']),time.time(),sid))
                row=self.row(sid,gid)
            elif 'note' in args:raise ValueError('保存话题说明时请同时确认已处理的消息进度。')
            end=time.monotonic()+args.get('timeout_seconds',WAIT_TIMEOUT_SECONDS)
            planned_wait=self.turns.remaining_wait(sid)
            if planned_wait:end=min(end,time.monotonic()+planned_wait)
            earliest=min(end,time.monotonic()+args.get('minimum_wait_seconds',0))
            quiet=args.get('quiet_seconds',self.turns.policy()['quiet_seconds']);last_max=None;changed_at=time.monotonic();heartbeat=0
            collecting_since=time.monotonic()
            limit=args.get('limit',30);echoes=0
            mention_only=bool(args.get('mention_only') or row.get('participation')=='mention_only')
            account=row['conversation_id'].split(':')[0]
            followup_turns=args.get('followup_turns', 3)
            followup_seconds=args.get('followup_seconds', 60)
            dialogue_window=self.dialogue_windows.setdefault(sid, {'active_until': 0, 'remaining_followups': 0})
            with self.access.connect() as db:
                last_turn=db.execute("SELECT payload FROM chat_turn_plans WHERE session_id=? ORDER BY created DESC LIMIT 1",(sid,)).fetchone()
                if last_turn:
                    try:
                        if json.loads(last_turn['payload']).get('action')=='silence':
                            dialogue_window['remaining_followups']=0
                    except Exception:pass
            while True:
                if cancel.is_set():return dict(event='cancelled',messages=[],note='仅取消此次等待；会话未停止，原进度可接续。')
                try:grant,row=self.validate(sid,gid)
                except ChatStopped:return dict(event='stopped' if not self.row(sid)['active'] else 'service_offline',session=self.public(self.row(sid)),messages=[])
                if time.monotonic()-heartbeat>=10:
                    self.touch(sid);heartbeat=time.monotonic()
                with self.access.connect() as db:
                    pending=db.execute('SELECT * FROM chat_inbox WHERE session_id=? AND id>? ORDER BY id LIMIT ?',
                                       (sid,row['cursor'],limit+1)).fetchall()
                consumed=self.consume_sent_echoes(row,pending)
                if consumed:
                    echoes+=consumed
                    continue
                newest=pending[-1]['id'] if pending else row['cursor']
                wake_kind,notices=self.apps.wake(row)
                newest=(newest,notices[-1]['notice_id'] if notices else 0)
                if newest!=last_max:last_max=newest;changed_at=time.monotonic()
                now=time.monotonic()

                has_mention=False
                is_in_dialogue=False
                if mention_only:
                    allow_learning=False
                    if pending:
                        with self.access.connect() as db:
                            own_rows=db.execute("SELECT message_id FROM chat_inbox WHERE session_id=? AND coalesce(json_extract(payload,'$.is_self'),0)=1 ORDER BY id DESC LIMIT 50",(sid,)).fetchall()
                            own_ids={str(r[0]) for r in own_rows}
                        cfg = load_water_group_config()
                        gid_str = row['conversation_id'].split(':')[-1]
                        group_cfg = cfg.get('groups', {}).get(gid_str, {})
                        names = []
                        cfg_names = cfg.get('self_names')
                        if isinstance(cfg_names, list):
                            names.extend(str(n) for n in cfg_names if n)
                        grp_names = group_cfg.get('self_names') or group_cfg.get('wake_words')
                        if isinstance(grp_names, list):
                            names.extend(str(n) for n in grp_names if n)
                        p_name = row.get('persona_name')
                        if p_name and p_name != '自定义人格':
                            names.append(p_name)
                            for part in p_name.split():
                                if part:
                                    names.append(part)
                        keywords = {k.casefold() for k in names if k}
                        if account:
                            keywords.add(account.casefold())
                        cfg_account = str(cfg.get('self_account') or '')
                        if cfg_account and not cfg_account.startswith('YOUR_'):
                            keywords.add(cfg_account.casefold())
                        kw_list = [k for k in keywords if k]
                        for r in pending:
                            payload = json.loads(r['payload'])
                            if payload.get('is_self'):
                                continue
                            segs = payload.get('segments', [])
                            if any(s.get('type') == 'at' and str(s.get('qq', '')) in (account, cfg_account, 'all') for s in segs):
                                has_mention = True
                                break
                            if any(s.get('type') == 'reply' and str(s.get('onebot_message_id', '')) in own_ids for s in segs):
                                has_mention = True
                                break
                            txt = (payload.get('content', '') or payload.get('text', '')) + " " + " ".join(s.get('text', '') for s in segs if s.get('type') == 'text')
                            txt_lower = txt.casefold()
                            if any(kw in txt_lower for kw in kw_list):
                                has_mention = True
                                break

                    if has_mention:
                        dialogue_window['remaining_followups'] = followup_turns
                        dialogue_window['active_until'] = time.monotonic() + followup_seconds

                    is_in_dialogue = bool(dialogue_window.get('remaining_followups', 0) > 0 and time.monotonic() < dialogue_window.get('active_until', 0))

                    if not has_mention and not is_in_dialogue and now < end:
                        if len(pending) > 15:
                            trim_through = pending[-10]['id']
                            with self.access.connect() as db:
                                db.execute('UPDATE chat_sessions SET cursor=? WHERE id=? AND active=1', (trim_through, sid))
                            continue

                collection = self.turns.collection(row, pending, quiet, collecting_since)
                waiting = self.turns.waiting(sid)
                new_input = False
                if waiting:
                    with self.access.connect() as db:
                        live = db.execute("SELECT 1 FROM chat_inbox WHERE session_id=? AND id>? AND id<=? AND coalesce(json_extract(payload,'$.recalled'),0)=0 LIMIT 1",
                            (sid, row['cursor'], waiting['watermark'])).fetchone()
                        new_input = bool(db.execute('SELECT 1 FROM chat_inbox WHERE session_id=? AND id>? LIMIT 1', (sid, waiting['watermark'])).fetchone())
                if waiting and not live:
                    with self.access.connect() as db:
                        db.execute("UPDATE chat_turn_plans SET state='CANCELLED',reason='input_unavailable' WHERE session_id=? AND state='WAITING'", (sid,))
                    waiting = None
                deferred = bool(waiting and time.time() < waiting['not_before'] and not new_input)
                signal = self.apps.wake_signal(grant, row) if allow_learning and not pending and not notices and now >= earliest else None

                ready_to_return = False
                if mention_only:
                    if (has_mention or is_in_dialogue) and (len(pending) > limit or collection['ready']):
                        ready_to_return = True
                else:
                    if (len(pending) > limit or collection['ready']) or (notices and now - changed_at >= quiet) or (signal is not None and signal['ready']):
                        ready_to_return = True

                if (now >= earliest and pending and not deferred and ready_to_return) or now >= end:
                    if mention_only:
                        if not has_mention and not is_in_dialogue and now >= end:
                            if pending:
                                through = pending[-1]['id']
                                with self.access.connect() as db:
                                    db.execute('UPDATE chat_sessions SET cursor=?,offered=max(offered,?) WHERE id=? AND active=1', (through, through, sid))
                                    db.executemany('UPDATE chat_inbox SET delivered=1 WHERE session_id=? AND id=?', [(sid, m['id']) for m in pending])
                                pending = []
                        elif not has_mention and is_in_dialogue and ready_to_return:
                            dialogue_window['remaining_followups'] -= 1
                            dialogue_window['active_until'] = time.monotonic() + followup_seconds
                    items=self.details.enrich(grant,row,self.messages(pending[:limit] if not deferred else []),cancel)
                    through=items[-1]['id'] if items else max(row['cursor'],row['dropped_through'])
                    with self.lock:
                        self.validate(sid,gid)
                        if cancel.is_set():return dict(event='cancelled',messages=[])
                        with self.access.connect() as db:
                            if waiting and items and not deferred:
                                # Expiry or new input ends the timer, not the
                                # pending input. Only later page ACKs advance it.
                                db.execute("UPDATE chat_turn_plans SET state='CANCELLED',reason=? WHERE session_id=? AND state='WAITING' AND json_extract(payload,'$.batch_id')=?",
                                    ('new_input' if new_input else 'wait_elapsed',sid,waiting['batch_id']))
                            db.execute('UPDATE chat_sessions SET offered=max(offered,?) WHERE id=? AND active=1',(through,sid))
                            db.executemany('UPDATE chat_inbox SET delivered=1 WHERE session_id=? AND id=?',[(sid,m['id']) for m in items])
                    status=self.receiver.status()
                    ready=status['state']=='connected' and status['account']==row['conversation_id'].split(':')[0]
                    kind='messages' if items else wake_kind if notices and ready else signal['kind'] if signal is not None and signal['ready'] and ready else 'idle' if ready else 'source_unavailable'
                    if kind == 'idle':
                        try:
                            from water_group.memory_engine import get_memory_engine
                            with self.access.connect() as _db:
                                _recent_rows = _db.execute('SELECT payload FROM chat_inbox WHERE session_id=? ORDER BY id DESC LIMIT 20', (sid,)).fetchall()
                            if _recent_rows:
                                _recent_payloads = [json.loads(_r['payload']) for _r in reversed(_recent_rows)]
                                get_memory_engine().digest_idle_batch(row['conversation_id'].split(':')[-1], _recent_payloads)
                        except Exception:
                            pass
                    result=dict(event=kind,session_id=sid,messages=items,read_through_id=through,
                                has_more=not deferred and len(pending)>len(items),continue_waiting=True,acknowledged_sent_echoes=echoes,
                                receiver=status,gap_count=row['gap_count'],gap_note='接收曾中断或缓存溢出，不保证消息完整。' if row['gap_count'] else '',
                                note='处理后以 read_through_id 确认；idle 继续等待，不表示整个聊天结束。')
                    if len(pending)>limit:collection['reason']='page_limit'
                    elif now>=end:collection['reason']='timeout'
                    result['input_batch']=self.turns.batch(row,items,collection)
                    changed=bool(args.get('known_prompt_version') and args['known_prompt_version']!=prompts.version(row))
                    result['chat_guidance']=self.guidance(grant,row,items,through=through,
                        event=kind if ready else 'source_unavailable',has_more=result['has_more'])
                    result['decision_required']=bool(ready and (any(not m.get('recalled') for m in items) or result['chat_guidance']['reaction_notices'].get('new_items')))
                    result['next_call']=result['chat_guidance']['continue_with']
                    if result['decision_required'] and not result['has_more']:
                        result['next_call']=result['input_batch']['plan_call']
                    if deferred:result['deferred_until']=waiting['not_before']
                    if signal is not None and kind==signal['kind']:
                        result[signal['kind']]=signal['payload']
                        result['next_call']=signal['next_call']
                        result['note']=signal['note']
                    if not result['decision_required']:
                        result['input_batch']['phase']='WAITING'
                        result['input_batch']['next']='本轮没有待处理输入；直接 next_call，不需要 plan_chat_reply。'
                    if changed:
                        result['chat_prompt']=dict(result['chat_guidance'],behavior=prompts.BEHAVIOR,persona=row['persona'])
                        with self.access.connect() as db:
                            db.execute('UPDATE chat_sessions SET prompt_delivered_version=? WHERE id=?',(prompts.version(row),sid))
                    result['prompt_changed']=changed
                    return result
                with timing.span('wait.event_sleep'):
                    event.wait(min(.4,max(.01,end-now)))
                event.clear()
        finally:
            with self.lock:self.waiters.discard(sid);self.events.pop(sid,None)
