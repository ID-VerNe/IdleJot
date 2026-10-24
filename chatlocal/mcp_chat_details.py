"""Bounded enrichment of references attached to delivered live events. No LLM."""
import json
import threading
import time

from . import reply_timing as timing
from .mcp_chat_media import image_ref
from .onebot import OneBotError
from .onebot_segments import at as at_segment, reply as reply_segment

MAX_NODES=100  # QQ 单条合并转发的上限，实测 get_forward_msg 一次全量返回（issue #8）
MAX_TEXT=12000


def forward_nodes(value):
    if isinstance(value,list):return value
    if isinstance(value,dict):
        # `content` 是 NapCat 解析 multiForwardMsgElement 时的键名（parseMultMsg 打开后
        # 才填，见 issue #8）；`messages` / `nodes` 分别是适配器返回与本地持久化形态。
        for key in ('content','messages','nodes'):
            if isinstance(value.get(key),list):return value[key]
    return None


def sanitize_nodes(value,depth=0,budget=None):
    """Whitelist only meaningful message fields before persisting adapter data."""
    budget=budget if budget is not None else dict(nodes=MAX_NODES,text=MAX_TEXT)
    result=[]
    if depth>=2:return result
    source=forward_nodes(value) or []
    for node in source[:MAX_NODES]:
        if not isinstance(node,dict) or budget['nodes']<=0:break
        budget['nodes']-=1
        sender=node.get('sender',{})
        if not isinstance(sender,dict):sender={}
        content=node.get('message',node.get('content',[]))  # event / inline node
        if isinstance(content,str):content=[dict(type='text',data=dict(text=content))]
        segments=[];truncated=isinstance(content,list) and len(content)>100
        for seg in content[:100] if isinstance(content,list) else []:
            if not isinstance(seg,dict):continue
            kind=seg.get('type');d=seg.get('data',{})
            if not isinstance(d,dict):continue
            if kind=='text':
                original=str(d.get('text',''));text=original[:max(0,budget['text'])];budget['text']-=len(text)
                truncated=truncated or len(text)<len(original)
                segments.append(dict(type='text',text=text))
            elif kind in ('image','mface'):segments.append(dict(type='image',image=image_ref(d)))
            elif kind=='reply':segments.append(reply_segment(d.get('id')))
            elif kind=='at':segments.append(at_segment(d.get('qq')))
            elif kind=='forward':
                children=sanitize_nodes(d,depth+1,budget);raw_children=forward_nodes(d)
                ref=dict(type='forward',truncated=depth+1>=2 or raw_children is None or len(children)<len(raw_children))
                if raw_children is not None:ref['nodes']=children
                if d.get('id'):ref['id']=str(d['id'])[:200]
                segments.append(ref)
            else:segments.append(dict(type=str(kind)[:24]))
        result.append(dict(sender_id=str(sender.get('user_id',node.get('user_id','')))[:30],
                           sender=str(sender.get('card') or sender.get('nickname') or '')[:200],
                           # 节点自己的 message_id 是真实可用的（实测同一批节点各不相同），
                           # 保留它才能对转发内单条再 get_msg（issue #8）。
                           message_id=str(node.get('message_id') or node.get('real_id') or '')[:30],
                           timestamp=node.get('time') if type(node.get('time')) in (int,float) else None,
                           segments=segments,truncated=truncated))
    if result and len(result)<len(source):result[-1]['truncated']=True
    return result


def reference_refs(segments,stored):
    """这条消息实际携带的引用，而不只是入库时记下来的那些。

    `stored`（`chat_inbox.details` 列）只对「已经解析出的内容」权威。有没有引用
    是消息段自己的属性：历史行、入库后才补上的引用段都有段没记录，只信 `stored`
    会让 `referenced_messages` 整个消失，Agent 分不清「没引用」和「解析失败」
    （issue #94）。段上已经带显式 state 的（第 9 条引用、超额的合并转发）保持原样，
    那个 state 本身就是答案。
    """
    found=[seg for seg in segments or [] if seg.get('type') in ('reply','forward') and not seg.get('state')]
    stored=[ref for ref in stored or [] if isinstance(ref,dict)]
    if not found:return stored
    refs=[]
    for position,seg in enumerate(found):
        ref=stored[position] if position<len(stored) else None
        if not ref or ref.get('type')!=seg['type']:
            # 合成一条：正常解析路径会填内容，解析不出来就是显式的 unavailable。
            ref=(dict(type='reply',id=str(seg.get('onebot_message_id') or '')) if seg['type']=='reply'
                 else dict(type='forward',id=str(seg.get('id') or '')[:200]))
        refs.append(ref)
    return refs+stored[len(found):]


def unresolved_refs(segments):
    """入库行已经不在了：引用照样要出现，只是状态明说不可得。"""
    refs=[]
    for seg in segments or []:
        if seg.get('type')=='reply':
            refs.append(dict(type='reply',onebot_message_id=str(seg.get('onebot_message_id') or ''),state='unavailable'))
        elif seg.get('type')=='forward':
            refs.append(dict(type='forward',state='unavailable',nodes=[],total_nodes=0,truncated=False,has_more=False))
    return refs


class ChatDetails:
    def __init__(self,chat):
        self.chat=chat;self.access=chat.access
        with self.access.connect() as db:
            if 'details' not in {r['name'] for r in db.execute('PRAGMA table_info(chat_inbox)')}:
                db.execute("ALTER TABLE chat_inbox ADD COLUMN details TEXT NOT NULL DEFAULT '[]'")
            db.execute('''CREATE TABLE IF NOT EXISTS chat_detail_cache(
                session_id TEXT NOT NULL, cache_key TEXT NOT NULL, body TEXT NOT NULL, at REAL NOT NULL,
                PRIMARY KEY(session_id,cache_key))''')
            db.execute("DELETE FROM chat_detail_cache WHERE cache_key LIKE 'reply:%' AND json_extract(body,'$.state')='available' AND json_extract(body,'$.raw_segments') IS NOT NULL")
            db.execute('DELETE FROM chat_detail_cache WHERE session_id NOT IN (SELECT id FROM chat_sessions WHERE active=1)')

    def cached(self,sid,key):
        with self.access.connect() as db:
            found=db.execute('SELECT body,at FROM chat_detail_cache WHERE session_id=? AND cache_key=?',(sid,key)).fetchone()
        if found:
            body=json.loads(found['body'])
            if body.get('state')=='recalled' or time.time()-found['at']<300:return body

    def store(self,sid,key,body):
        with self.access.connect() as db:
            db.execute('''INSERT INTO chat_detail_cache VALUES(?,?,?,?)
                ON CONFLICT(session_id,cache_key) DO UPDATE SET body=excluded.body,at=excluded.at
                WHERE json_extract(chat_detail_cache.body,'$.state')!='recalled' ''',
                (sid,key,json.dumps(body,ensure_ascii=False),time.time()))
            db.execute("DELETE FROM chat_detail_cache WHERE session_id=? AND json_extract(body,'$.state')!='recalled' AND cache_key NOT IN (SELECT cache_key FROM chat_detail_cache WHERE session_id=? ORDER BY at DESC LIMIT 1000)",(sid,sid))

    def query(self,row,action,args,deadline,cancel):
        if cancel.is_set() or time.monotonic()>=deadline:return None
        try:
            client=self.chat.actions.client_factory()
            client.timeout=1.
            client.deadline=deadline
            if client.login()!=row['conversation_id'].split(':')[0]:return None
            if cancel.is_set() or time.monotonic()>=deadline:return None
            with timing.span('chat.details.'+action):result=client.call(action,args)
            return result if not cancel.is_set() else None
        except OneBotError:return None

    def reply(self,grant,row,mid,deadline,cancel):
        with self.access.connect() as db:
            found=db.execute('SELECT * FROM chat_inbox WHERE session_id=? AND message_id=?',(row['id'],mid)).fetchone()
        if found:
            message=json.loads(found['payload']);media=json.loads(found['media'])
            if message.get('recalled'):return dict(state='recalled')
            refs=json.loads(found['details']);segments=[]
            for s in message['segments']:
                if s.get('type')=='image' and 'image_index' in s:s=dict(s,image=media[s['image_index']])
                elif s.get('type')=='forward' and s.get('forward_index',-1) in range(len(refs)):
                    s=dict(refs[s['forward_index']])
                segments.append(s)
            return dict(state='available',source='inbox',sender=message['sender'],sender_id=message['sender_id'],
                        timestamp=message['timestamp'],segments=segments,truncated=message.get('truncated',False))
        key='reply:'+mid;cached=self.cached(row['id'],key)
        if cached:return cached
        result=self.query(row,'get_msg',dict(message_id=mid),deadline,cancel)
        if not isinstance(result,dict):
            return dict(state='cancelled' if cancel.is_set() else 'budget_exhausted' if time.monotonic()>=deadline else 'unavailable')
        account,category,peer=row['conversation_id'].split(':')
        if category=='direct':
            try:
                self.chat.qq.verify_message(grant,dict(account=account,kind=category,peer=peer),result,int(mid))
            except ValueError:return dict(state='unverified')
        elif (str(result.get('message_id'))!=mid or str(result.get('group_id'))!=peer or
              result.get('message_type','group')!='group' or str(result.get('self_id',account))!=account):
            return dict(state='unverified')
        if any(result.get(k) for k in ('recalled','is_recalled','deleted','is_deleted')):return dict(state='recalled')
        stamp=result.get('time');plan=self.access.plan(grant)
        if type(stamp) not in (int,float) or stamp<=0:return dict(state='unverified')
        if ((plan.start is not None and stamp*1000<plan.start) or
            (plan.end is not None and stamp*1000>=plan.end)):return dict(state='outside_scope')
        sender=result.get('sender') if isinstance(result.get('sender'),dict) else {}
        uid=str(result.get('user_id',sender.get('user_id','')))
        if not uid.isdecimal():return dict(state='unverified')
        nodes=sanitize_nodes([dict(sender=sender,content=result.get('message',[]),time=stamp)])
        if not nodes:return dict(state='unavailable')
        answer=dict(state='available',source='onebot',sender=str(sender.get('card') or sender.get('nickname') or uid)[:200],
                    sender_id=uid,timestamp=int(stamp*1000),segments=nodes[0]['segments'],truncated=nodes[0]['truncated'])
        self.store(row['id'],key,answer)
        return self.cached(row['id'],key)

    def forward(self,row,ref,deadline,cancel,*,depth=0,budget=None,memo=None,ancestors=()):
        budget=budget if budget is not None else dict(nodes=MAX_NODES,text=MAX_TEXT)
        memo=memo if memo is not None else {};ident=ref.get('id')
        if cancel.is_set():return dict(state='cancelled',nodes=[],truncated=False)
        if ident and ident in ancestors:return dict(state='cycle',nodes=[],truncated=True)
        if depth>=2 or budget['nodes']<=0 or budget['text']<=0:
            return dict(state='limit_reached',nodes=[],truncated=True)
        if 'nodes' in ref and (ref['nodes'] or not ref.get('truncated')):
            answer=dict(state='available',nodes=ref['nodes'],truncated=ref.get('truncated',False))
        elif not isinstance(ident,str) or not ident.strip() or len(ident)>200:
            return dict(state='unavailable',nodes=[],truncated=True,limit_reason='missing_resource_id')
        else:
            key='forward:'+ident
            answer=memo.get(key)
            if answer is None:
                if time.monotonic()>=deadline:return dict(state='budget_exhausted',nodes=[],truncated=False)
                answer=self.cached(row['id'],key)
            if answer is None:
                result=self.query(row,'get_forward_msg',dict(id=ident),deadline,cancel)
                nodes=forward_nodes(result)
                answer=(dict(state='available',nodes=sanitize_nodes(nodes),truncated=len(nodes)>MAX_NODES) if nodes is not None else
                        dict(state='cancelled' if cancel.is_set() else 'budget_exhausted' if time.monotonic()>=deadline else 'unavailable',nodes=[],truncated=False))
                if nodes is not None:self.store(row['id'],key,answer)
            memo[key]=answer
        # Cache the existing sanitized resource, not a partially expanded tree:
        # later reads may have a different remaining budget or retry a child.
        nodes=[];clipped=answer.get('truncated',False)
        for node in answer.get('nodes',[]):
            if budget['nodes']<=0:clipped=True;break
            budget['nodes']-=1;segments=[];cut=node.get('truncated',False)
            for seg in node['segments']:
                if 'data' in seg:
                    data=seg['data'];seg=dict(type=seg['type'],**(dict(image=data) if seg['type']=='image' else data))
                if seg.get('type')=='forward':
                    child=self.forward(row,seg,deadline,cancel,depth=depth+1,budget=budget,memo=memo,
                                       ancestors=ancestors+((ident,) if ident else ()))
                    segments.append(dict(type='forward',**child))
                elif seg.get('type')=='text':
                    text=seg.get('text','');kept=text[:max(0,budget['text'])];budget['text']-=len(kept)
                    cut=cut or len(kept)<len(text);segments.append(dict(seg,text=kept))
                else:segments.append(seg)
            nodes.append(dict(node,segments=segments,truncated=cut))
        return dict(answer,nodes=nodes,truncated=clipped)

    def render_forward(self,forward,media,stamp,allow_images,quote_message_id,location=None,read=None):
        nodes=self.render_nodes(forward.get('nodes',[]),media,stamp,allow_images,quote_message_id)
        selected=read and location and all(read.get(k)==location.get(k) for k in ('forward_index','reply_index'))
        offset,limit=(read['offset'],read['limit']) if selected else (0,MAX_NODES)
        page=nodes[offset:offset+limit];more=offset+len(page)<len(nodes)
        # Pagination only promises retained nodes; hard sanitization limits
        # cannot be recovered by repeatedly asking for the same empty page.
        def limits(value):
            reasons=({value['limit_reason']} if value.get('limit_reason') else
                     {'cycle'} if value.get('state')=='cycle' else {'processing_limit'} if value.get('truncated') else set())
            for k in ('nodes','segments'):
                for child in value.get(k,[]):reasons.update(limits(child))
            return reasons
        reasons=limits(forward);truncated=bool(reasons)
        state=forward.get('state','available' if 'nodes' in forward else 'unavailable')
        detail=dict(type='forward',state=state,nodes=page,total_nodes=len(nodes),truncated=truncated,has_more=more)
        if location:
            detail['forward_index']=location['forward_index']
            if 'reply_index' in location:detail['reply_index']=location['reply_index']
            call=dict(tool='read_chat_forward',arguments=dict(location,offset=offset,limit=limit))
            retryable=[s for n in page for s in n['segments'] if s.get('type')=='forward' and s.get('state') in ('unavailable','budget_exhausted') and not s.get('unrecoverable_truncation')]
            for segment in retryable:segment.update(read_call=call,continuation_supported=True)
            if (state in ('unavailable','budget_exhausted') and not forward.get('limit_reason')) or retryable:detail['read_call']=call
            if more:detail['next_call']=dict(tool='read_chat_forward',arguments=dict(location,offset=offset+len(page),limit=limit))
        detail['continuation_supported']=bool(location and (more or 'read_call' in detail))
        if truncated:
            detail.update(unrecoverable_truncation=True,limit_reason=sorted(reasons)[0])
            if len(reasons)>1:detail['incomplete_reasons']=sorted(reasons)
        return detail

    def render(self,segments,media,stamp,allow_images=True,quote_message_id=None,location=None,read=None):
        result=[];parts=[]
        for index,seg in enumerate(segments):
            # Old persisted forward nodes used OneBot-shaped segments.
            if 'data' in seg:
                data=seg['data'];seg=dict(type=seg['type'],**(dict(image=data) if seg['type']=='image' else data))
            kind=seg.get('type')
            if kind=='text':
                text=str(seg.get('text',''))[:MAX_TEXT];parts.append(text);result.append(dict(type='text',text=text))
            elif kind=='image':
                if not allow_images or 'image' not in seg:
                    parts.append('[图片]');result.append(dict(type='image',readable=False,state=seg.get('state','permission_denied' if not allow_images else 'unavailable')));continue
                ref=dict(seg['image']);ref.setdefault('timestamp',stamp)
                if quote_message_id:ref['quote_message_id']=quote_message_id
                if ref in media:index=media.index(ref)
                elif len(media)<8:index=len(media);media.append(ref)
                else:
                    parts.append('[图片，超过本批图片上限]');result.append(dict(type='image',readable=False,state='image_limit'));continue
                parts.append('[图片]');result.append(dict(type='image',image_index=index))
            elif kind=='forward':
                target=dict(location,forward_index=index) if location else None
                parts.append('[嵌套转发]');result.append(self.render_forward(seg,media,stamp,allow_images,quote_message_id,target,read))
            elif kind=='at':
                parts.append('@'+str(seg.get('qq','')));result.append(dict(type='at',qq=str(seg.get('qq',''))))
            else:
                label={'reply':'引用','record':'语音','video':'视频','file':'文件','face':'表情','at':'@'}.get(kind,'非文本消息')
                parts.append('['+label+']');result.append(dict(type=label))
        return dict(content=''.join(parts)[:MAX_TEXT],segments=result)

    def render_nodes(self,nodes,media,stamp,allow_images=True,quote_message_id=None):
        # message_id 一起透出：Agent 可以拿它对该条再 get_msg（issue #8）。
        return [dict({k:n[k] for k in ('sender_id','sender','message_id','timestamp','truncated') if k in n},
                     **self.render(n['segments'],media,n['timestamp']*1000 if n.get('timestamp') else stamp,allow_images,quote_message_id)) for n in nodes]

    @timing.stage('chat.details.enrich')
    def enrich(self,grant,row,items,cancel=None,read=None):
        cancel=cancel or threading.Event();deadline=time.monotonic()+self.chat.turns.policy()['detail_budget_seconds']
        output=[];memo={}
        for item in items:
            if cancel.is_set():break
            if not item.get('truncated') and not any(s.get('type') in ('reply','forward') for s in item.get('segments',[])):
                output.append(item);continue
            with self.access.connect() as db:
                event=db.execute('SELECT * FROM chat_inbox WHERE id=? AND session_id=?',(item['id'],row['id'])).fetchone()
            if not event:
                # 这一行可能在翻页和本轮之间被缓存挤掉。消息已经交给 Agent 了，
                # 它的引用就不能变成「字段不存在」（issue #94）。
                refs=unresolved_refs(item.get('segments'))
                output.append(dict(item,referenced_messages=refs) if refs else item);continue
            if item.get('recalled'):output.append(item);continue
            refs=reference_refs(item.get('segments'),json.loads(event['details']))
            resolved=[]
            for ref in refs:
                # 段自带的显式状态（第 9 条引用）不再去解析，直接原样交付。
                if ref.get('state'):resolved.append(dict(state=str(ref['state'])))
                elif ref['type']=='reply':resolved.append(self.reply(grant,row,ref['id'],deadline,cancel))
                else:resolved.append(self.forward(row,ref,deadline,cancel,memo=memo))
            # A verified quote may itself carry a forward resource. Resolve it
            # before entering the short media-index merge transaction.
            for ref,detail in zip(refs,resolved):
                if ref['type']=='reply' and detail.get('state')=='available':
                    detail['segments']=[dict(s,**self.forward(row,s,deadline,cancel,memo=memo)) if s.get('type')=='forward' else s
                                        for s in detail.get('segments',[])]
            grant,row=self.chat.validate(row['id'],grant['id'])
            if cancel.is_set():break
            with self.access.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current=db.execute('SELECT media,payload FROM chat_inbox WHERE id=? AND session_id=?',(item['id'],row['id'])).fetchone()
                if not current:
                    # 同上：行没了也要把消息交出去，引用标不可得而不是消失。
                    fallback=unresolved_refs(item.get('segments'))
                    output.append(dict(item,referenced_messages=fallback) if fallback else item);continue
                if json.loads(current['payload']).get('recalled'):
                    output.append(dict(item,recalled=True));continue
                media=json.loads(current['media']);details=[]
                for index,ref in enumerate(refs):
                    if ref['type']=='reply':
                        recalled=db.execute('SELECT body FROM chat_detail_cache WHERE session_id=? AND cache_key=?',
                                            (row['id'],'reply:'+ref['id'])).fetchone()
                        detail=dict(state='recalled') if recalled and json.loads(recalled['body']).get('state')=='recalled' else dict(resolved[index])
                        raw=detail.pop('segments',None)
                        if raw is not None:
                            detail.update(self.render(raw,media,detail.get('timestamp') or item['timestamp'],bool(grant['scope'].get('chat_images')),ref['id'],
                                dict(session_id=row['id'],event_id=item['id'],reply_index=index),read))
                        details.append(dict(type='reply',onebot_message_id=ref['id'],**detail))
                    else:
                        forward=resolved[index]
                        detail=self.render_forward(forward,media,item['timestamp'],bool(grant['scope'].get('chat_images')),None,
                            dict(session_id=row['id'],event_id=item['id'],forward_index=index),read)
                        details.append(detail)
                db.execute('UPDATE chat_inbox SET media=? WHERE id=? AND session_id=?',(json.dumps(media),item['id'],row['id']))
                output.append(dict(item,referenced_messages=details) if details else item)
        return output

    def read(self,grant,args,cancel):
        grant,row=self.chat.validate(args['session_id'],grant['id'])
        with self.access.connect() as db:
            found=db.execute('SELECT * FROM chat_inbox WHERE id=? AND session_id=? AND delivered=1',(args['event_id'],row['id'])).fetchone()
        if not found or json.loads(found['payload']).get('recalled'):raise ValueError('转发不属于已读取的当前会话或已撤回。')
        payload=json.loads(found['payload'])
        # 与 enrich 同一份引用清单，否则这里的位置和 Agent 看到的对不上。
        refs=reference_refs(payload.get('segments'),json.loads(found['details']))
        index=args.get('forward_index',0);reply_index=args.get('reply_index')
        target=index if reply_index is None else reply_index
        if target>=len(refs) or refs[target]['type']!=('forward' if reply_index is None else 'reply'):
            raise ValueError('该事件没有这个合并转发或引用位置。')
        item=dict(payload,id=found['id'])
        result=self.enrich(grant,row,[item],cancel,dict(offset=0,limit=MAX_NODES)|dict(args,forward_index=index))
        if not result:raise ValueError('转发读取已取消。')
        if result[0].get('recalled'):raise ValueError('转发消息已撤回，未交付内容。')
        detail=result[0]['referenced_messages'][target]
        if reply_index is not None:
            if detail['state']!='available':return dict(event_id=found['id'],type='forward',state=detail['state'],nodes=[],has_more=False)
            segments=detail.get('segments',[])
            if index>=len(segments) or segments[index]['type']!='forward':raise ValueError('引用内没有这个合并转发位置。')
            detail=segments[index]
        return dict(event_id=found['id'],**detail)
