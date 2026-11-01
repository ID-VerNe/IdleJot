"""Bounded enrichment of references attached to delivered live events. No LLM."""
import json
import threading
import time

from . import reply_timing as timing
from .mcp_chat_media import image_ref
from .onebot import OneBotError
from .onebot_segments import at as at_segment, card_text, kind_of, link_fields, render_content, reply as reply_segment, unsupported
from .onebot_segments import UNSUPPORTED_TOTAL_CHARS
from .text_limits import clip

MAX_NODES=100  # QQ 单条合并转发的上限，实测 get_forward_msg 一次全量返回（issue #8）
MAX_TEXT=12000
# 嵌套转发展开到第几层（issue #351）。**条数与字符数才是主防线**（两者跨层共享
# 预算），深度只是「不许无界递归」的最后一环——外面一层套一层，结构本身就可以
# 无限长，条数预算却要等到真的读到节点才递减。
MAX_FORWARD_DEPTH=3
# 嵌套层单节点文本额度：一条 25 条的嵌套转发最多吃掉 MAX_TEXT // 25 × 25 ≈ 12000
# 字——与原来「所有节点共用一个 MAX_TEXT」同一量级，不会因为多了递归就把整条消息
# 的文本预算翻几倍。逐节点 480 而非再细分，是因为「百来字」的节点在省流场景里常常
# 正好被切在半句上。
NESTED_TEXT_CHARS=MAX_TEXT//MAX_NODES
# 转发占位里正文的可见字数（issue #351）：群友 @ 机器人要「省流」时，Agent 拿到的
# 不能只有一句 `[合并转发]`。每个子节点留一句，深度/条数/字符三条预算照旧兜底。
NESTED_PREVIEW_CHARS=120
# 一条消息里所有转发占位正文的合计上限，防止几十条「[转发:…]」把 content 顶爆。
NESTED_PREVIEW_TOTAL=2000


def forward_nodes(value):
    if isinstance(value,list):return value
    if isinstance(value,dict):
        # `content` 是 NapCat 解析 multiForwardMsgElement 时的键名（parseMultMsg 打开后
        # 才填，见 issue #8）；`messages` / `nodes` 分别是适配器返回与本地持久化形态。
        for key in ('content','messages','nodes'):
            if isinstance(value.get(key),list):return value[key]
    return None


def forward_children(ref):
    """一个转发段下面「已经落盘」的子节点（issue #351）。

    两种形态都要认：外层引用把子节点挂在 `nodes`（历史行同款），节点内的嵌套转发
    段同样用 `nodes`——但 `get_forward_msg` 返回的原始段是 `{type:'forward',data:…}`，
    `forward_nodes()` 不认外面这层 `data`。以前 `sanitize_nodes` 里各写一份判断、
    `ChatDetails.forward()` 里再写一份 `'nodes' in ref`，三处对「有没有子节点」的口径
    不一致时，同一批数据在入库 / 交付两条路径上会给出两个答案。
    """
    if not isinstance(ref,dict):return None
    for key in ('nodes','children'):
        if isinstance(ref.get(key),list):return ref[key]
    return None


def _forward_ref(kind_data,children,truncated,reason=None):
    """嵌套转发的落盘形态与它的截断理由（issue #351）。

    ``reason`` 写在 ``limit_reason`` 上就够渲染侧认出来，但**不能**只有渲染侧认得：
    同一段落库后被 `mcp_chat.reply()`、`unresolved_refs()` 原样交出去时没人补这一栏，
    读者拿到的就是一句 `truncated=True` 却说不出为什么。``state`` 与 ``limit_reason``
    一起写，两种读者（渲染路径 / 直接透传路径）看到的是同一句话。
    """
    ref=dict(type='forward',truncated=bool(truncated))
    if reason:ref.update(state='limit_reached',limit_reason=reason)
    if children is not None:ref['nodes']=children
    if kind_data.get('id'):ref['id']=str(kind_data['id'])[:200]
    return ref


def _node_text(node,depth=0):
    """一个转发子节点里「能读的那几段」（issue #351）。

    同一个「子节点」在两条路上形态不同：`render_nodes()` 产出的是
    ``{'sender','content'}``（content 已经是渲染好的字符串），`sanitize_nodes()`
    落盘的是 ``{'sender','segments'}``（段列表）。只看 ``content`` 的话，入库侧
    永远拿到空串，`forward_preview()` 会退化成一句纯 ``[转发]``——「能展但不吭声」，
    比完全不展开更难发现（#106 那种排除型假绿的同一个坑）。

    节点里如果还套了转发，往里再走一层：占位是**给省流看的**，只写
    「外层说了一句话」而把整段嵌套内容藏起来，读者照样得再调一次工具。
    """
    if isinstance(node.get('content'),str):
        return node['content']
    pieces=[]
    for seg in node.get('segments') or []:
        if not isinstance(seg,dict):continue
        kind=seg.get('type')
        if kind=='text':pieces.append(str(seg.get('text') or ''))
        elif kind=='image':pieces.append('[图片]')
        elif kind=='at':pieces.append('@'+str(seg.get('name') or seg.get('qq') or ''))
        elif kind=='unsupported':pieces.append('['+str(seg.get('kind') or '非文本')+']')
        elif kind=='forward':
            inner=' '.join(filter(None,(_node_text(child,depth+1) for child in seg.get('nodes') or [] if isinstance(child,dict))))
            if inner:pieces.append(inner)
    return ' '.join(pieces)


def forward_preview(ref,limit=NESTED_PREVIEW_CHARS):
    """转发段在 content 里的那一句：``[转发: 甲: … / 乙: [图片]]``（issue #351）。

    ``ref`` 既可以是已经渲染过的节点列表（``{'nodes':[{'sender','content'}]}``），
    也可以是 `sanitize_nodes()` 落盘的节点（``{'nodes':[{'sender','segments'}]}``），
    还可以是节点内的嵌套转发段——两种节点形态由 `_node_text()` 抹平。
    没有可读子节点时回一句纯占位，绝不返回空串——「这条消息里有个转发」本身就是
    要传达的信息，空串会让它凭空消失。

    内层占位里的方括号换成空格：唤醒行的 `strip_placeholders` 按
    ``\\[[^\\]]*\\]`` 剥占位，``[转发: 甲: [图片]]`` 会在第一个 ``]`` 处停下，
    把后半截留在正文里占额度（同 #165 卡片摘要的处理）。
    """
    nodes=forward_children(ref) or []
    pieces=[]
    for node in nodes:
        if not isinstance(node,dict):continue
        body=' '.join(_node_text(node).split())
        if not body:continue
        body=' '.join(body.replace('[',' ').replace(']',' ').split())
        if not body:continue
        sender=' '.join(str(node.get('sender') or '').replace('[',' ').replace(']',' ').split())
        pieces.append((sender or '无名')+': '+clip(body,limit).text)
    return '[转发: '+' / '.join(pieces)+']' if pieces else '[转发]'


def sanitize_nodes(value,depth=0,budget=None,forward_depth=0,item=None):
    """Whitelist only meaningful message fields before persisting adapter data.

    ``depth`` 是**数据本身的嵌套层数**（`content` / `messages` 再套 `content`），
    上限 1 层：一个节点里的消息段不该再是「一组节点」。

    ``forward_depth`` 是**转发套转发**的层数（issue #351）。以前这里只有
    ``if depth>=2:return []`` 一条线，而 `content` 为空的嵌套转发恰好也会走进这条
    递归（``forward_nodes([])`` 返回 ``[]``，``[] or []`` 仍递归），所以**只有空**的
    嵌套转发才能被收下——内联了节点的内层转发一律只剩一个带 id 的段，子消息全部
    消失。NapCat 的 `parseMultMsg` 打开时正好把内层整包内联进来，于是「部署建议开启」
    的那条路径反而看不到内容。

    三条上限跨层共享：节点数（``budget['nodes']``）、节点文本（``budget['text']``）、
    嵌套层文本（``budget['nested_text']``）。后两条是**两笔账**：
    ``budget['text']`` 是「这一层能写多少字」，进每一层时收窄到
    ``NESTED_TEXT_CHARS``、出层还原（每层各自封顶）；``nested_text`` 才是整条消息的
    总账，只按实际花掉的字数递减。混成一笔的话，一条 25 条转发的节点会把整条消息
    的文本额度吃光，并列的第二条转发连一句都展不出来——深度上限就成了摆设。

    ``item`` 是**一个节点能拿到的文本额度**（默认 ``budget['text']``）。它存在的
    唯一理由是「转发里套转发」：外层节点的正文（`sender` 昵称、说明文字）如果能把
    整份额度吃干净，内层转发即便还剩预算也展不出来。收窄后正文与嵌套转发各自
    有把握的额度，谁也不会把谁饿死。

    任一上限耗尽都在**耗尽位置**写 ``state='limit_reached'`` + ``limit_reason``，
    不静默少东西——「被截断」与「本来就只有这些」是两件事，读者要能分开。
    """
    budget=budget if budget is not None else dict(nodes=MAX_NODES,text=MAX_TEXT)
    budget.setdefault('nested_text',MAX_TEXT)
    item=budget['text'] if item is None else item
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
        # 这个节点自己的文本额度（issue #351）。共享的 `budget['text']` 按实际花掉的
        # 字数递减，出节点还原成层额度。
        #
        # 正文与内层转发**分账**，不按先后到者得：这个节点里只要还套着转发，正文最多
        # 吃 `item` 的一半，另一半留给转发。以前是正文先吃 `share`、剩下的才轮到
        # 转发，于是一条「引用一段长文 + 转发原帖」的节点里，长文把额度吃干净后
        # 内层转发只剩 `share=0`——`item` 收了窄却什么也没保住，读者看到的仍是一句
        # 空占位。没有嵌套转发时（最常见的情况）正文独占整份，一个字都不少。
        share=min(item,budget['text'])
        nested_share=share//2 if any(isinstance(s,dict) and s.get('type')=='forward'
                                     for s in (content if isinstance(content,list) else [])) else 0
        share-=nested_share
        for seg in content[:100] if isinstance(content,list) else []:
            if not isinstance(seg,dict):continue
            kind=seg.get('type')
            # 段体内联的嵌套转发在节点里用的是 `raw`（与 `unsupported` 段同款键名），
            # `data` 是 OneBot 原生形态——两种都认，否则同一棵内联树在「入库时还在」
            # 与「补读时没了」之间来回切换（issue #351）。
            d=seg.get('data') if isinstance(seg.get('data'),dict) else seg.get('raw')
            if not isinstance(d,dict):
                if kind=='text' and isinstance(seg.get('text'),str):
                    # 已落盘的文本段是扁平的 `{type:'text',text:…}`，再读回节点树时
                    # 它没有 `data`——不认这一形态，二次读取会把正文整段丢掉。
                    d=dict(text=seg['text'])
                elif kind not in ('forward',):continue
                else:d={}
            if kind=='text':
                original=str(d.get('text',''));text=original[:max(0,share)]
                share-=len(text);budget['text']-=len(text)
                truncated=truncated or len(text)<len(original)
                segments.append(dict(type='text',text=text))
            elif kind in ('image','mface'):segments.append(dict(type='image',image=image_ref(d)))
            elif kind=='reply':segments.append(reply_segment(d.get('id')))
            elif kind=='at':segments.append(at_segment(d.get('qq'),d.get('name')))
            elif kind=='forward':
                # 预留给正文的那半份，转发的实际开销由 `_nested_forward` 记进总账，
                # 这里把本节点用不掉的那部分还回去（`share += nested_share` 在下面
                # 与节点还原一起做，不会让同一个字被算两次）。
                segments.append(_nested_forward(d,budget,forward_depth,share+nested_share))
            # issue #165：白名单外的段不再只留一个中文标签当 type。节点内同样
            # 保真降级（kind + 受限长度 raw），渲染侧才说得出「这是什么」。
            else:segments.append(unsupported(kind,d,budget))
        budget['text']+=share+nested_share
        result.append(dict(sender_id=str(sender.get('user_id',node.get('user_id','')))[:30],
                           sender=str(sender.get('card') or sender.get('nickname') or '')[:200],
                           # 节点自己的 message_id 是真实可用的（实测同一批节点各不相同），
                           # 保留它才能对转发内单条再 get_msg（issue #8）。
                           message_id=str(node.get('message_id') or node.get('real_id') or '')[:30],
                           timestamp=node.get('time') if type(node.get('time')) in (int,float) else None,
                           segments=segments,truncated=truncated))
    if result and len(result)<len(source):result[-1]['truncated']=True
    return result


def _nested_forward(data,budget,forward_depth,item=None):
    """节点内的一个嵌套转发段（issue #351）：能展开就展开，展不开说清为什么。

    三种「展不开」必须分开说，否则 Agent 会对着一条已经到手的转发反复重读：

    * ``depth``：层数到了 ``MAX_FORWARD_DEPTH``，再深就是无界递归；
    * ``node_limit`` / ``content_limit``：跨层共享预算用光了，``read_chat_forward``
      换个位置重读也不会多出来（同一批节点）；
    * 没有可展开的内联节点、也没有资源 id：真拿不到（``unavailable``）。

    ``item`` 是外层节点给内层留的文本额度（见 `sanitize_nodes`）；同时还要受
    ``NESTED_TEXT_CHARS`` 封顶——内层的每个节点同样不该比同一个包里的节点拿到更多。
    """
    raw=forward_nodes(data)
    budget['nested_text']=budget.get('nested_text',MAX_TEXT)
    if raw is not None:
        if forward_depth+1>=MAX_FORWARD_DEPTH:return _forward_ref(data,None,True,'depth')
        if budget['nested_text']<=0 or budget['text']<=0:return _forward_ref(data,None,True,'content_limit')
        allowance=min(x for x in (NESTED_TEXT_CHARS,budget['text'],budget['nested_text'],
                                  budget['text'] if item is None else item) if x>=0)
        before=budget['text'];before_allowance=allowance
        try:children=sanitize_nodes(raw,budget=budget,forward_depth=forward_depth+1,item=allowance)
        finally:
            spent=before-budget['text'];budget['text']=before
            budget['nested_text']=max(0,budget['nested_text']-spent)
        # 内层自己的正文被额度砍过（`spent` 顶到 `allowance`）也算一次截断：
        # 只看 `len(children)<len(raw)` 时，额度小到只够写半句的情况会「节点都在、
        # 字数却少了」却一句理由都不给——读者会以为这就是原文。
        exhausted=before_allowance>0 and spent>=before_allowance
        if len(children)<len(raw) or exhausted:
            # 究竟是「条数用光」还是「嵌套文本用光」，按收缩的那一项点名，不一律
            # 记成同一个理由——读者据此决定要不要换个位置重读。
            return _forward_ref(data,children,True,'node_limit' if budget['nodes']<=0 else 'content_limit')
        return _forward_ref(data,children,False)
    ref=_forward_ref(data,None,True,'unavailable' if not data.get('id') else None)
    return ref


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
        if depth>=MAX_FORWARD_DEPTH:
            # 第 4 个深度闸门（issue #351）：`sanitize_nodes` 的数据侧与
            # `render_forward` 的渲染侧都按 MAX_FORWARD_DEPTH 收敛，这里是**补读**
            # 那一侧——以前写死 `depth>=2`，于是一棵已经展开到第 3 层的树在 enrich
            # 路上被砍回 2 层，`processing_limit` 又没有 `limit_reason`，读者只看到
            # 「被截断」却问不出为什么。三处闸门同一常量、同一理由名。
            return dict(state='limit_reached',nodes=[],truncated=True,limit_reason='depth')
        if budget['nodes']<=0 or budget['text']<=0:
            return dict(state='limit_reached',nodes=[],truncated=True,
                        limit_reason='node_limit' if budget['nodes']<=0 else 'content_limit')
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

    def render_forward(self,forward,media,stamp,allow_images,quote_message_id,location=None,read=None,_depth=0):
        # issue #351：`_depth` 是**渲染时**的嵌套层数。数据侧（`sanitize_nodes`）已经
        # 按 MAX_FORWARD_DEPTH 收敛过，这里再判一次是因为历史行与 adapter 直接喂进来
        # 的节点树没有经过 sanitize——渲染是无界递归的最后一个入口。
        if _depth>=MAX_FORWARD_DEPTH:
            return dict(type='forward',state='limit_reached',nodes=[],total_nodes=0,truncated=True,
                        unrecoverable_truncation=True,limit_reason='depth',continuation_supported=False)
        nodes=self.render_nodes(forward.get('nodes',[]),media,stamp,allow_images,quote_message_id,_depth=_depth)
        selected=read and location and all(read.get(k)==location.get(k) for k in ('forward_index','reply_index'))
        offset,limit=(read['offset'],read['limit']) if selected else (0,MAX_NODES)
        page=nodes[offset:offset+limit];more=offset+len(page)<len(nodes)
        # Pagination only promises retained nodes; hard sanitization limits
        # cannot be recovered by repeatedly asking for the same empty page.
        def limits(value):
            # issue #351：以前「内容被砍」一律记成 `processing_limit`，读者分不出是
            # 文本用光、条数用光还是层数到顶——三者的补救动作完全不同（补读 / 换
            # 位置 / 都没有）。耗尽的层自己写清了 limit_reason，这里只补外层的总账。
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

    def render(self,segments,media,stamp,allow_images=True,quote_message_id=None,location=None,read=None,_depth=0,_budget=None):
        result=[];parts=[]
        # 一条消息共享的链接预算（issue #311）：入库侧由 `receive` 的 raw_budget 管，
        # 引用/转发节点在这里自成一条消息——不给预算的话，三十张卡片的老行回填能
        # 把 content 顶过 MAX_TEXT，占位被硬切在半路（`]` 都没了，唤醒行剥不掉）。
        link_budget={}
        # 转发占位正文的共享额度（issue #351）：每个节点都要看得见，但不许几条转发
        # 就把整条 content 撑满。
        _budget=_budget if _budget is not None else dict(preview=NESTED_PREVIEW_TOTAL)
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
                rendered=self.render_forward(seg,media,stamp,allow_images,quote_message_id,target,read,_depth=_depth)
                # issue #351：以前无论节点有没有展开都只写「[嵌套转发]」四个字，
                # 群友 @ 机器人要「省流」时 Agent 手里就是一句占位。现在有内容就
                # 摆出来（共享额度内），没内容才回纯占位——`forward_preview()` 的
                # 唯一构造点，入库侧（mcp_chat.receive）共用。
                parts.append(forward_preview(rendered,min(NESTED_PREVIEW_CHARS,max(0,_budget['preview']))))
                _budget['preview']=max(0,_budget['preview']-len(parts[-1]))
                result.append(rendered)
            elif kind=='at':
                # issue #52: the @-ed member's display name survives into the
                # rendered forward node; only name-less legacy segments fall
                # back to the bare number.
                qq=str(seg.get('qq',''));display=str(seg.get('name') or qq)
                parts.append('@'+display)
                result.append(dict(type='at',qq=qq,**({'name':str(seg['name'])} if seg.get('name') else {})))
            else:
                # issue #165：未知段说得出是什么，而不是只剩一句「非文本消息」。
                # `unsupported` 段带 kind（和可能的 raw），历史行里的中文标签
                # 归一成 unknown——那是「没记下类型」，不是一种类型。
                # issue #311：占位改由 card_text() 统一拼（标题 + 跳转地址）；
                # #311 之前落库的行没有 `url` 字段，这里从 raw 现算补出来——
                # 否则「修好之后的新卡片有链接、老卡片没有」，同一个入口两种答案。
                degraded=kind=='unsupported'
                source=seg.get('raw') if degraded else None
                if degraded:
                    stored=dict(seg)
                    if not stored.get('url'):
                        stored.update(link_fields(stored.get('kind'),source,link_budget))
                    parts.append(card_text(stored.get('kind'),source,stored))
                    result.append(stored)
                else:
                    parts.append(card_text(kind,source))
                    result.append(dict(type='unsupported',kind=kind_of(kind)))
        # issue #315：切断落在段边界上，不再把卡片占位切在半路。段清单同步裁剪，
        # 免得「content 里看不到这段」而「segments 里还留着」两种说法并存。
        content,kept=render_content(parts,result,MAX_TEXT)
        return dict(content=content,segments=kept)

    def render_nodes(self,nodes,media,stamp,allow_images=True,quote_message_id=None,_depth=0,_budget=None):
        # message_id 一起透出：Agent 可以拿它对该条再 get_msg（issue #8）。
        # issue #351：同一页节点共享一份占位额度（`_budget`），每个节点的嵌套层数
        # 在 `_depth` 上逐层加一——渲染侧的深度上限就靠它。
        _budget=_budget if _budget is not None else dict(preview=NESTED_PREVIEW_TOTAL)
        return [dict({k:n[k] for k in ('sender_id','sender','message_id','timestamp','truncated') if k in n},
                     **self.render(n['segments'],media,n['timestamp']*1000 if n.get('timestamp') else stamp,allow_images,quote_message_id,
                                   _depth=_depth+1,_budget=_budget)) for n in nodes]

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
