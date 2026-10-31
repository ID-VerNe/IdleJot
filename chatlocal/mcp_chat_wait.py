"""The blocking wait loop for persistent chat sessions (issue #11, section PC).

Split out of ``mcp_chat.py``: admission control, explicit cursor ACK, collection
policy, deferred turn plans, learning wake-up and page/timeout boundaries --
the whole ``wait`` concern, provided as a mixin so ``MCPChat`` keeps one flat
public surface. ``ChatStopped`` and the water-group config loader stay on
``mcp_chat`` (raised and used by the session lifecycle that remains there) and
are imported per call -- the same cycle break this package already uses for
``ChatMedia``.

``mention_only`` groups keep a **dialogue window** (``chat_sessions.dialogue_until``
/ ``dialogue_turns``, issue #217): while it is open, chatter without a fresh @ is
still delivered. The window is durable on purpose -- it survives the process, so
the collector reconnecting or the server restarting does not lose the "reply then
keep listening" tail. It is opened by a mention and re-anchored by **our own send
receipt**, because the agent usually takes longer to compose a reply than
``followup_seconds``.

Do not confuse it with ``water-group/daemon/followup.py``: that is a separate,
daemon-side window that reads ``chat_inbox`` after ``reply.py`` sends and wakes
``watch.py``. Neither module depends on the other; only the word "followup" is
shared, and this one never gates what ``--context`` can see.
"""
import json
import threading
import time
from typing import NamedTuple

from . import reply_timing as timing
from . import mcp_chat_prompts as prompts
from .mcp_chat_limits import WAIT_TIMEOUT_SECONDS


class MentionState(NamedTuple):
    """issue #278：``wait()`` 内 mention_only 检测块的返回值（逐字搬运自原 128-200）。"""

    has_mention: bool
    is_in_dialogue: bool
    allow_learning: bool
    window_anchor: int
    dialogue_turns: int
    dialogue_until: float
    trimmed: bool


class ChatWaitMixin:
    def set_dialogue_window(self,sid,until,turns):
        """把 mention_only 的对话窗口写回会话行（issue #217）。

        窗口以前只活在 ``MCPChat.dialogue_windows`` 的内存字典里，服务重启就没了；
        落库之后 collector 重连、reload.ps1、整栈重启都接得上。``until`` 用墙钟，
        跨进程才有意义；写入只发生在开窗 / 续期 / 扣轮次三处，轮询不碰库。
        """
        with self.access.connect() as db:
            db.execute('UPDATE chat_sessions SET dialogue_until=?,dialogue_turns=? WHERE id=? AND active=1',
                       (float(until),int(turns),sid))

    def _detect_mention_and_dialogue(self, sid, row, pending, account,
                                     mention_only, followup_turns, followup_seconds,
                                     now, end,
                                     window_anchor, dialogue_turns, dialogue_until):
        """issue #278：从 ``wait()`` 抽出的 mention_only self-name/对话窗口检测块。

        逐字搬运自原 ``wait()`` 128-200，行为字节级一致（#169 PF-3 逐字搬运）。
        只读 ``self.access``/``self.set_dialogue_window``，写 5 个返回字段
        （has_mention/is_in_dialogue/allow_learning/window_anchor/
        dialogue_turns/dialogue_until）+ 一次 ``self.set_dialogue_window``。

        ``trimmed=True`` 表示块内的 trim-through 短路命中，调用方应 ``continue``
        外层 ``while True``（块内原本直接 ``continue``，helper 不能跨帧跳循环，
        改由返回标志驱动，行为不变）。
        """
        from .mcp_chat import load_water_group_config
        has_mention=False
        is_in_dialogue=False
        allow_learning=True
        trimmed=False
        if mention_only:
            allow_learning=False
            own_sent=False
            if pending:
                with self.access.connect() as db:
                    own_rows=db.execute("SELECT message_id FROM chat_inbox WHERE session_id=? AND coalesce(json_extract(payload,'$.is_self'),0)=1 ORDER BY id DESC LIMIT 50",(sid,)).fetchall()
                    own_ids={str(r[0]) for r in own_rows}
                cfg = load_water_group_config()
                gid_str = row['conversation_id'].split(':')[-1]
                p_name = row.get('persona_name')
                # issue #279：唤醒词合并（顶层 + per-group + persona + account）
                # 单一出处在 wg_wake_words，与 daemon wg_up_wake.wake_words 同源——
                # 改前两套口径，per-group wake_words 配了 daemon 不认。延迟 import
                # 与 wg_text.connect_ro 门面同款：daemon 目录不保证在 chatlocal
                # 运行时的 sys.path（server_wg.py 从 repo root 起），wait 这条
                # 路径每轮现取、自己补 sys.path，不污染模块级。
                import sys as _sys
                from pathlib import Path as _Path
                _daemon = str(_Path(__file__).resolve().parents[1] / 'water-group' / 'daemon')
                if _daemon not in _sys.path:
                    _sys.path.insert(0, _daemon)
                import wg_wake_words
                cfg_account = str(cfg.get('self_account') or '')
                keywords = wg_wake_words.wake_keywords(
                    cfg=cfg, group_id=gid_str, persona_name=p_name, account=account)
                kw_list = [k for k in keywords if k]
                for r in pending:
                    payload = json.loads(r['payload'])
                    if payload.get('is_self'):
                        # 本机发出的回复：窗口要按「回复之后」重算起算点
                        # （issue #217）。reply.py 直发的回执没有
                        # send_request.operation_id，consume_sent_echoes
                        # 不认，只能在这里认。
                        if not payload.get('recalled'):
                            own_sent = True
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

            # 开窗 / 续期只在「出现新的 @ 或本机新发出的话」时做一次，别在
            # 0.4 秒一轮的轮询里反复写库；anchor 用批尾 id 认这一批。
            anchor=pending[-1]['id'] if pending else 0
            if (has_mention or own_sent) and anchor!=window_anchor:
                window_anchor=anchor
                dialogue_turns=followup_turns
                dialogue_until=time.time()+followup_seconds
                self.set_dialogue_window(sid,dialogue_until,dialogue_turns)

            is_in_dialogue=bool(dialogue_turns>0 and time.time()<dialogue_until)

            if not has_mention and not is_in_dialogue and now < end:
                if len(pending) > 15:
                    trim_through = pending[-10]['id']
                    with self.access.connect() as db:
                        db.execute('UPDATE chat_sessions SET cursor=? WHERE id=? AND active=1', (trim_through, sid))
                    trimmed=True

        return MentionState(has_mention, is_in_dialogue, allow_learning,
                            window_anchor, dialogue_turns, dialogue_until, trimmed)

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
            # issue #217：窗口从行里读，不再挂在 MCPChat 的内存字典上——服务重启
            # 后 collector 接回来，窗口还在；时间基准必须是墙钟，monotonic 跨进程
            # 不可比。写入只发生在下面三处（开窗 / 续期 / 扣轮次），别在轮询里刷。
            dialogue_until=float(row.get('dialogue_until') or 0)
            dialogue_turns=int(row.get('dialogue_turns') or 0)
            window_anchor=0
            with self.access.connect() as db:
                last_turn=db.execute("SELECT payload FROM chat_turn_plans WHERE session_id=? ORDER BY created DESC LIMIT 1",(sid,)).fetchone()
                if last_turn and dialogue_turns:
                    try:
                        # Only `silence` closes the window ("this round is over").
                        # `wait` deliberately leaves it alone: it means "not
                        # deciding yet", not "nobody is talking to me". `ask`
                        # fills it — the agent just opened its mouth and a peer's
                        # next message, even without an @, is the answer it must
                        # see. Filling happens once per plan, before the
                        # decrement below, so the budget still runs out normally.
                        # issue #217：窗口落在会话行上（墙钟），不再用内存字典。
                        action = json.loads(last_turn['payload']).get('action')
                        if action=='silence':
                            dialogue_turns=0
                            self.set_dialogue_window(sid,dialogue_until,dialogue_turns)
                        elif action=='ask':
                            dialogue_turns=followup_turns
                            dialogue_until=time.time()+followup_seconds
                            self.set_dialogue_window(sid,dialogue_until,dialogue_turns)
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
                now = time.monotonic()

                # issue #278：mention_only 的 self-name/对话窗口检测抽到
                # _detect_mention_and_dialogue（逐字搬运原 128-200，#169 PF-3）。
                # trimmed=True 时块内 trim-through 短路命中，照旧 continue 外层
                # while——块内原本直接 continue，helper 跨不了帧，改由返回标志驱动。
                # issue #279：helper 内的唤醒词合并已改委派 wg_wake_words（顶层 +
                # per-group + persona + account 的单一出处），与 daemon 同源。
                _ms = self._detect_mention_and_dialogue(
                    sid, row, pending, account,
                    mention_only, followup_turns, followup_seconds,
                    now, end,
                    window_anchor=window_anchor,
                    dialogue_turns=dialogue_turns,
                    dialogue_until=dialogue_until)
                has_mention=_ms.has_mention
                is_in_dialogue=_ms.is_in_dialogue
                allow_learning=_ms.allow_learning
                window_anchor=_ms.window_anchor
                dialogue_turns=_ms.dialogue_turns
                dialogue_until=_ms.dialogue_until
                if _ms.trimmed:
                    continue

                collection = self.turns.collection(row, pending, quiet, collecting_since)
                waiting = self.turns.waiting(sid)
                new_input = False
                if waiting:
                    with self.access.connect() as db:
                        live = db.execute("SELECT 1 FROM chat_inbox WHERE session_id=? AND id>? AND id<=? AND coalesce(json_extract(payload,'$.recalled'),0)=0 LIMIT 1",
                            (sid, row['cursor'], waiting['watermark'])).fetchone()
                        new_input = bool(db.execute('SELECT 1 FROM chat_inbox WHERE session_id=? AND id>? LIMIT 1', (sid, waiting['watermark'])).fetchone())
                if waiting and not live and waiting['watermark'] > row['cursor']:
                    # A non-empty batch with no live input past the cursor is
                    # a real input_unavailable: ids were promised but none is
                    # deliverable now (e.g. all recalled). An empty-batch wait
                    # has watermark == cursor by construction (turns.batch),
                    # so the live probe is geometrically empty — that plan has
                    # nothing to promise in the first place and must live to
                    # its own not_before, where wait_elapsed closes it. The old
                    # guard cancelled it here and starved the wait_elapsed
                    # branch below (issue #260).
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
                                    # 只推游标，不置 delivered（issue #217）：这批一条
                                    # 都没交给过 Agent，`delivered` 不能替它签字。上面
                                    # 的 trim 分支早就是这个口径，--context 与追问窗口
                                    # 都只认游标，三处因此一致。
                                    db.execute('UPDATE chat_sessions SET cursor=?,offered=max(offered,?) WHERE id=? AND active=1', (through, through, sid))
                                pending = []
                        elif not has_mention and is_in_dialogue and ready_to_return:
                            dialogue_turns -= 1
                            dialogue_until = time.time() + followup_seconds
                            self.set_dialogue_window(sid, dialogue_until, dialogue_turns)
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
                    result['input_batch']=self.turns.batch(row,items,collection)
                    changed=bool(args.get('known_prompt_version') and args['known_prompt_version']!=prompts.version(row))
                    result['chat_guidance']=self.guidance(grant,row,items,through=through,
                        event=kind if ready else 'source_unavailable',has_more=result['has_more'])
                    result['decision_required']=bool(ready and (any(not m.get('recalled') for m in items) or result['chat_guidance']['reaction_notices'].get('new_items')))
                    if len(pending)>limit:collection['reason']='page_limit'
                    elif now>=end:collection['reason']='timeout'
                    # A `wait` that reached its own deadline with no input to
                    # hand back is the agent's "接话窗口到了" event (issue #222).
                    # The plan stays WAITING until the agent decides, the input
                    # is untouched, and the packet must say so out loud — else
                    # the round ends on a plain idle that reads like "no
                    # traffic". When the wait was holding input, that input is
                    # returned as `messages` instead: same deadline, but the
                    # packet already carries something to decide on.
                    # `not_before` is wall-clock (turns.plan stores `now+wait`),
                    # while `now` above is monotonic: compare like with like.
                    wait_elapsed=(not items and waiting and not deferred and waiting.get('action')=='wait'
                                  and time.time()>=waiting['not_before'])
                    if wait_elapsed:
                        # `turns.waiting()` returns the plan payload only, so the
                        # id has to be read next to it: a null plan_id would send
                        # the agent looking for a plan it cannot name.
                        with self.access.connect() as db:
                            pid=db.execute("SELECT id FROM chat_turn_plans WHERE session_id=? AND state='WAITING' ORDER BY created DESC LIMIT 1",(sid,)).fetchone()
                        result['event']=kind='wait_elapsed'
                        result['wait_plan']=dict(plan_id=pid[0] if pid else None,action='wait',
                                                 not_before=waiting['not_before'])
                        result['decision_required']=True
                        result['note']=(f'第 {waiting["batch_id"]} 批暂缓的等待已到期；原输入未变，'
                                       '现在决定接话（reply/ask）或明确收场（silence），不要无声地再等一次。')
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
