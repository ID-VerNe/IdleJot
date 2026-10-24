"""Prepare pending live-chat pixels at the public MCP boundary, outside chat locks."""
import time
from .mcp_access import RateLimited
from .mcp_chat_media import IMAGE_DELIVERY_NOTE


def image_segments(value):
    for segment in value.get('segments',[]):
        if segment.get('type')=='image':yield segment
        for node in segment.get('nodes',[]):yield from image_segments(node)
    for detail in value.get('referenced_messages',[]):
        yield from image_segments(detail)
    for node in value.get('nodes',[]):yield from image_segments(node)


def incomplete(value):
    return bool(value.get('truncated') or value.get('has_more') or
        value.get('state') not in (None,'available') or
        any(incomplete(child) for key in ('referenced_messages','nodes','segments') for child in value.get(key,[])))


def mentions_self(value, account):
    """消息是否明确 @ 了本账号，或引用的是本账号发言。

    OneBot 入站@段是 {type:'at', qq:...}，出站/兼容段也可能是 data.qq。
    """
    if not account:
        return False
    for segment in value.get('segments',[]):
        if segment.get('type')=='at':
            qq=str(segment.get('qq') or (segment.get('data') or {}).get('qq') or '')
            if qq==account:return True
        if any(mentions_self(node,account) for node in segment.get('nodes',[])):return True
    for detail in value.get('referenced_messages',[]):
        # 对方引用我的发言，同样算这次互动指向我。
        if str(detail.get('sender_id') or '')==account:return True
        if mentions_self(detail,account):return True
    for node in value.get('nodes',[]):
        if mentions_self(node,account):return True
    return False


def prepare(chat,grant,data,cancel,*,forward_session_id=None,auto_images='candidates'):
    packet=data.get('updated_chat',data)
    explicit=forward_session_id is not None
    items=[dict(packet,id=packet['event_id'])] if explicit else packet.get('messages',packet.get('context',{}).get('messages',[]))
    if not items:return data,[]
    sid=forward_session_id or packet.get('session_id') or packet.get('session',{}).get('id')
    if not sid:return data,[]
    grant,row=chat.validate(sid,grant['id'],active=False)
    if not row['active']:return data,[]
    # 默认只给 @ 我那条消息（含其引用）预热像素，其余只给元数据按需补读。
    account=row['conversation_id'].split(':')[0]
    candidates=auto_images=='candidates'
    # 显式转发（read_chat_forward）沿用旧的 8 张上限；普通等待默认只预热 2 张。
    limit=0 if auto_images=='none' else 8 if (explicit or auto_images=='all') else 2
    images=[];entries=[];attempts=0;prepared=[]
    deadline=time.monotonic()+chat.turns.policy()['detail_budget_seconds']
    for item in items:
        # Necessary preceding context stays text-only; never prefetch old history.
        if (not explicit and item['id']<=row['cursor']) or item.get('recalled'):continue
        wanted=explicit or auto_images=='all' or (candidates and mentions_self(item,account))
        indexes={}
        for segment in image_segments(item):
            if 'image_index' in segment:indexes.setdefault(segment['image_index'],[]).append(segment)
            else:entries.append(dict(event_id=item['id'],state=segment.get('state','permission_denied')))
        for index,segments in indexes.items():
            entry=dict(event_id=item['id'],image_index=index,state='unavailable',
                read_call=dict(tool='read_chat_image',arguments=dict(session_id=sid,event_id=item['id'],index=index)))
            if cancel.is_set():entry['state']='cancelled'
            elif not grant['scope'].get('chat_images'):entry['state']='permission_denied'
            elif not wanted:entry['state']='deferred'
            elif attempts>=limit:entry['state']='image_limit'
            elif time.monotonic()>=deadline:entry['state']='budget_exhausted'
            elif not chat.media.slots.acquire(blocking=False):entry['state']='media_busy'
            else:
                attempts+=1;call_id=None;started=time.monotonic()
                try:
                    call_id=chat.access.begin_call(grant['id'],'read_chat_image')
                    info,frames=chat.media.event_image(grant,entry['read_call']['arguments'],cancel,remember=False)
                    # WGTools.result places one TextContent before these ImageContent blocks.
                    entry.update(state='pixels_delivered',
                        dimensions=info['dimensions'],frame_indices=info['frame_indices'],thumbnail=info['thumbnail'],representation=info['representation'])
                    prepared.append((entry,segments,frames))
                except RateLimited:entry['state']='rate_limited'
                except (ValueError,OSError):
                    chat.validate(sid,grant['id'])
                    entry['state']='cancelled' if cancel.is_set() else 'unavailable'
                finally:
                    if call_id is not None:chat.access.finish_call(call_id,entry['state'],time.monotonic()-started)
                    chat.media.slots.release()
            for segment in segments:segment['pixels']=dict(state=entry['state'])
            entries.append(entry)
    current,_=chat.validate(sid,grant['id'])
    for entry,segments,frames in prepared:
        try:
            chat.media.guard(current,sid,'read_chat_image',cancel)
            chat.media.event_reference(sid,entry['read_call']['arguments'])
        except ValueError:entry['state']='unavailable'
        else:
            entry['content_indexes']=list(range(1+len(images),1+len(images)+len(frames)))
            images.extend(frames)
        for segment in segments:segment['pixels']=dict(state=entry['state'],content_indexes=entry.get('content_indexes',[]))
    if cancel.is_set():
        for entry,segments,frames in prepared:
            entry['state']='cancelled';entry.pop('content_indexes',None)
            for segment in segments:segment['pixels']=dict(state='cancelled')
        images=[]
    for entry in entries:
        if entry['state']=='pixels_delivered':entry.pop('read_call',None)
    packet['materials']=dict(images=entries,
        incomplete=bool(packet.get('has_more') or packet.get('context',{}).get('has_more') or
            any(e['state'] not in ('pixels_delivered','deferred') for e in entries) or
            any(incomplete(item) for item in items)),
        note='有 has_more/next_call 继续保留页；有 read_call 仅补对应项，硬截断/循环/缺编号不重复读；混合项目分别处理。'+
             ('deferred 表示只给了元数据，需要看时用 read_call 补读，不重复读已交付项。' if any(e['state']=='deferred' for e in entries) else '')+
             (IMAGE_DELIVERY_NOTE if images else '本次没有交付图片块；失败状态和对应入口见各项。'))
    delivered={(e['event_id'],e.get('image_index')) for e in entries if e['state']=='pixels_delivered'}
    pending={(e['event_id'],e.get('image_index')) for e in entries}
    for key in ('chat_guidance','chat_prompt'):
        if key not in packet:continue
        guidance=packet[key]
        guidance['image_reads']=[r for r in guidance.get('image_reads',[]) if
            (r['arguments']['event_id'],r['arguments']['index']) in pending-delivered]
        if not guidance['image_reads']:guidance.pop('image_reads',None)
    return data,images
