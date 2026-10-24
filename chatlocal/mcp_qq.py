"""Scoped SL extension adapter with the same durable no-retry operation ledger."""
import hashlib
import json
import threading
import time
import uuid
from urllib.parse import urlsplit

import jsonschema

from .mcp_actions import dump, _execution
from .mcp_access import AccessDenied
from .mcp_qq_catalog import CATALOG, schemas as action_schemas
from .mcp_qq_media import QQMedia, TOOLS as MEDIA_TOOLS, schemas as media_schemas, FLAGS
from .mcp_qq_permissions import QQ_FLAGS
from .onebot import OneBotError, OneBotUncertain
from .message_sender import SendError, SendUncertain, qq_address

TOOLS = set(CATALOG) | MEDIA_TOOLS | {'get_qq_capabilities'}
WRITES = set(CATALOG) | {'import_qq_media'}  # Mixed tools advertise possible writes.
OUTBOUND = {'forward','forward_many','send_tuwen_ark','send_poke','send_like','set_group_sign'}


def schemas(tool, scope):
    return action_schemas(tool,scope)+media_schemas(tool,scope) if 'qq' in scope.get('platforms',[]) else []


def clean_result(value, depth=0):
    """Bound upstream data and omit credential/local-path fields from receipts."""
    if depth>10:return '[嵌套内容省略]'
    if isinstance(value,dict):
        return {str(k)[:100]:clean_result(v,depth+1) for k,v in list(value.items())[:100]
                if str(k).lower() not in {'token','authorization','cookie','cookies','clientkey','bkn','skey','p_skey','base64','local_path','path'}}
    if isinstance(value,list):return [clean_result(v,depth+1) for v in value[:100]]
    if isinstance(value,str):
        if value.startswith(('base64://','file://')):return '[媒体引用已隐藏]'
        return value[:12000]
    return value if value is None or isinstance(value,(bool,int,float)) else None


def paginate(value, offset, limit):
    pages={}
    def page(items,name):
        end=offset+limit;more=end<len(items)
        pages[name]=dict(offset=offset,has_more=more,next_offset=end if more else None,available_count=len(items))
        return items[offset:end]
    if isinstance(value,list):value=page(value,'items')
    elif isinstance(value,dict):value={k:page(v,k) if isinstance(v,list) else v for k,v in value.items()}
    return value,pages


class QQExtensions:
    def __init__(self, chat):
        self.chat=chat;self.access=chat.access;self.actions=chat.actions
        self.slots=threading.BoundedSemaphore(2)
        self.media=QQMedia(self)
        with self.access.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS qq_friend_requests(
                id TEXT PRIMARY KEY,account TEXT NOT NULL,user_id TEXT NOT NULL,
                flag TEXT NOT NULL,comment TEXT NOT NULL,created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS qq_friend_request_account ON qq_friend_requests(account,created);''')

    @staticmethod
    def check_cancel(cancel):
        if cancel.is_set():raise ValueError('调用已取消，未继续操作。')

    def identity(self, grant, permission=None):
        current=self.access.ensure_revision(grant)
        scope=current['scope'];account=scope.get('chat_account') or json.loads(current['accounts']).get('qq')
        if 'qq' not in scope['platforms'] or not account:raise AccessDenied('此连接没有绑定 QQ 账号。')
        if permission and not scope.get(permission):raise AccessDenied('此连接没有该 SL 扩展权限，请通过 Tulpa 授权。')
        plan=self.access.plan(current);now=time.time()*1000
        if plan.start is not None and now<plan.start or plan.end is not None and now>=plan.end:
            raise AccessDenied('当前时间不在连接授权日期内。')
        return current,account

    def client(self, grant):
        _,account=self.identity(grant)
        client=self.actions.client_factory(timeout=45)
        if client.login()!=account:raise AccessDenied('SL 登录账号与本连接绑定账号不一致。')
        return client

    def target(self, grant, cid, client=None):
        current,account=self.identity(grant)
        target=dict(platform='qq',conversation_id=cid,conversation=cid)
        actual,kind,peer=qq_address(target)
        scope=current['scope']
        if actual!=account or scope['conversations'] and ['qq',cid] not in [json.loads(p) for p in scope['conversations']]:
            raise AccessDenied('目标不在本连接的 QQ 会话授权中。')
        if client:
            if client.login()!=account:raise AccessDenied('SL 登录账号已经变化。')
            if kind=='group':
                info=client.call('get_group_info',dict(group_id=int(peer),no_cache=True))
                if not isinstance(info,dict) or str(info.get('group_id'))!=peer:raise ValueError('无法核对目标群。')
            else:
                rows=client.call('get_friend_list',{})
                if not isinstance(rows,list) or not any(isinstance(r,dict) and str(r.get('user_id'))==peer for r in rows):
                    raise ValueError('目标不是当前账号已核对的好友，不发送临时会话。')
        return dict(account=account,kind=kind,peer=peer,conversation_id=cid)

    def message(self, grant, cid, mid, client):
        target=self.target(grant,cid,client)
        data=client.call('get_msg',dict(message_id=mid))
        return self.verify_message(grant,target,data,mid)

    def verify_message(self, grant, target, data, mid):
        sender=data.get('sender') if isinstance(data,dict) and isinstance(data.get('sender'),dict) else {}
        if not isinstance(data,dict) or type(mid) is not int or not mid or str(data.get('message_id'))!=str(mid):raise ValueError('无法核对原消息。')
        if data.get('self_id') is not None and str(data['self_id'])!=target['account']:raise ValueError('消息账号不匹配。')
        if target['kind']=='group':valid=data.get('message_type')=='group' and str(data.get('group_id'))==target['peer']
        else:
            peer=data.get('target_id') if str(sender.get('user_id'))==target['account'] else data.get('user_id')
            valid=data.get('message_type')=='private' and str(peer)==target['peer'] and not data.get('group_id')
        stamp=data.get('time');plan=self.access.plan(grant)
        if (not valid or type(stamp) not in (int,float) or stamp<=0 or
                plan.start is not None and stamp*1000<plan.start or plan.end is not None and stamp*1000>=plan.end or
                any(data.get(k) for k in ('recalled','is_recalled','deleted','is_deleted'))):
            raise ValueError('消息不在授权会话/日期内，或已撤回。')
        return data

    def member(self, client, target, uid):
        if target['kind']=='direct':
            if str(uid)!=target['peer']:raise ValueError('对象必须是当前私聊好友。')
            return {}
        data=client.call('get_group_member_info',dict(group_id=int(target['peer']),user_id=int(uid),no_cache=True))
        if not isinstance(data,dict) or str(data.get('group_id'))!=target['peer'] or str(data.get('user_id'))!=str(uid):
            raise ValueError('无法核对对象属于当前群。')
        return data

    def admin(self, client, target, *, owner=False):
        me=self.member(client,target,target['account'])
        if me.get('role') not in (('owner',) if owner else ('owner','admin')):raise ValueError('当前账号没有所需群管理权限。')
        return me

    def receive(self, event):
        if event.get('post_type')!='request' or event.get('request_type')!='friend':return
        account,uid,flag=str(event.get('self_id','')),str(event.get('user_id','')),event.get('flag')
        if not account.isascii() or not account.isdigit() or not uid.isascii() or not uid.isdigit() or not isinstance(flag,str) or not 1<=len(flag)<=800:return
        with self.access.connect() as db:
            if not db.execute('SELECT enabled FROM settings WHERE id=1').fetchone()[0]:return
            scopes=[json.loads(r[0]) for r in db.execute('SELECT scope FROM grants WHERE revoked=0')]
            now=time.time()
            for scope in scopes:
                if not scope.get('qq_friends') or scope.get('chat_account')!=account:continue
                plan=self.access.plan({'scope':scope})
                if (plan.start is None or now*1000>=plan.start) and (plan.end is None or now*1000<plan.end):break
            else:return
            rid=hashlib.sha256((account+'\0'+flag).encode()).hexdigest()[:32]
            db.execute('INSERT OR IGNORE INTO qq_friend_requests VALUES(?,?,?,?,?,?)',(rid,account,uid,flag,str(event.get('comment',''))[:1000],now))
            db.execute('DELETE FROM qq_friend_requests WHERE created<?',(now-7*86400,))
            db.execute('DELETE FROM qq_friend_requests WHERE id NOT IN (SELECT id FROM qq_friend_requests ORDER BY created DESC LIMIT 500)')

    def guard(self, grant, spec, args, cancel, *, queued=False):
        current,account=self.identity(grant,spec['permission']);self.check_cancel(cancel)
        cid=args.get('conversation_id','')
        if cid:self.target(current,cid)
        if args['action'] in OUTBOUND:
            if not current['scope'].get('send'):raise AccessDenied('发送类操作还需要发送权限。')
            with self.access.connect() as db:
                active=db.execute('SELECT id,grant_id FROM chat_sessions WHERE conversation_id=? AND active=1',(cid,)).fetchone()
            if active and not queued:raise ValueError('该会话正在持续聊天，请使用 plan_chat_reply(expressions=["qq_action"]) → send_chat_reply 的 qq_action 气泡。')
            if queued:
                if not active or active['grant_id']!=grant['id']:raise ValueError('发送队列的会话授权已变化。')
                self.chat.validate(active['id'],grant['id']);self.chat.turns.before_dispatch()
        return current,account

    def write(self, grant, label, args, cancel, prepare, guard, *, cid=''):
        """No raw binary in the ledger; unknown attempts block changed-key retries."""
        signature=hashlib.sha256(dump([label,{k:v for k,v in args.items() if k!='idempotency_key'}]).encode()).hexdigest()
        key='qq:'+hashlib.sha256((label+'\0'+args['idempotency_key']).encode()).hexdigest()
        with _execution:
            guard()
            with self.access.connect() as db:
                old=db.execute('SELECT * FROM operations WHERE grant_id=? AND request_key=?',(grant['id'],key)).fetchone()
                unknown=db.execute("SELECT * FROM operations WHERE grant_id=? AND signature=? AND state IN ('UNKNOWN','EXECUTING')",(grant['id'],signature)).fetchone()
            if old:
                if old['signature']!=signature:raise ValueError('幂等编号已用于另一组参数。')
                return self.actions.public(old)
            if unknown:return self.actions.public(unknown)
            client,api,payload=prepare()
            guard()
            oid=uuid.uuid4().hex;now=time.time()
            with self.access.connect() as db:
                db.execute('INSERT INTO operations(id,grant_id,request_key,signature,kind,conversation_id,params,target,summary,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (oid,grant['id'],key,signature,label,cid,dump(args),'{}',label+(' → '+cid if cid else '（账号操作）'),'EXECUTING',now,now))
                db.execute('INSERT INTO operation_events(operation_id,state,at) VALUES(?,?,?)',(oid,'EXECUTING',now))
            dispatched=False
            try:
                guard()
                _,account=self.identity(grant)
                if client.login()!=account:raise AccessDenied('SL 账号已改变，未派发操作。')
                guard();dispatched=True
                result=client.call(api,payload,approved=True)
                if api in ('send_group_msg','send_private_msg','send_group_ai_record','forward_group_single_msg','forward_friend_single_msg','send_group_forward_msg','send_private_forward_msg'):
                    if not isinstance(result,dict) or type(result.get('message_id')) is not int or not result['message_id']:raise OneBotUncertain('缺少消息回执。')
                if api in ('upload_group_file','upload_private_file') and (not isinstance(result,dict) or not result.get('file_id')):raise OneBotUncertain('缺少文件回执。')
                result=dict(data=clean_result(result),api=api,conversation_id=cid,source='onebot_receipt')
                state='SUCCEEDED'
            except (OneBotUncertain,SendUncertain):state,result='UNKNOWN',dict(note='未取得确定回执；请核对 QQ，禁止换编号重发。')
            except (ValueError,OneBotError,SendError) as exc:state,result='FAILED',dict(note=str(exc))
            except Exception:state,result=('UNKNOWN' if dispatched else 'FAILED'),dict(note='操作中断；请用原编号查询回执。')
            with self.access.connect() as db:self.actions.transition(db,oid,state,result)
            return self.actions.get(oid,grant['id'])

    def prepare(self, grant, name, args):
        spec=CATALOG[name];api=args['action'];params=dict(args['params']);client=self.client(grant)
        _,account=self.identity(grant,spec['permission'])
        cid=args.get('conversation_id');target=self.target(grant,cid,client) if cid else None
        if spec['scope']=='group' and target['kind']!='group':raise ValueError('此操作只适用于授权 QQ 群。')
        if target and spec['scope']=='group':params['group_id']=int(target['peer'])
        if name=='qq_interact':
            if api=='set_group_sign':
                if target['kind']!='group':raise ValueError('签到仅用于群。')
                params={'group_id':int(target['peer'])}
            else:
                uid=params.get('user_id') or (target['peer'] if target['kind']=='direct' else None)
                if not uid:raise ValueError('群内互动需要明确 user_id。')
                self.member(client,target,uid);params['user_id']=int(uid)
                if api=='send_poke' and target['kind']=='group':params['group_id']=int(target['peer'])
        elif name=='qq_message_action':
            if api=='history':
                if params.get('message_id'):self.message(grant,cid,params['message_id'],client)
                api='get_group_msg_history' if target['kind']=='group' else 'get_friend_msg_history'
                params['group_id' if target['kind']=='group' else 'user_id']=int(target['peer'])
                params.setdefault('count',50)
            elif api in ('forward','forward_many'):
                source=params.pop('source_conversation_id');mids=params.pop('message_ids',None) or [params['message_id']]
                for mid in mids:self.message(grant,source,mid,client)
                if api=='forward':
                    api='forward_group_single_msg' if target['kind']=='group' else 'forward_friend_single_msg'
                    params={'message_id':mids[0]}
                else:
                    api='send_group_forward_msg' if target['kind']=='group' else 'send_private_forward_msg'
                    params={'messages':[dict(type='node',data=dict(id=str(mid))) for mid in mids]}
                params['group_id' if target['kind']=='group' else 'user_id']=int(target['peer'])
            elif api=='send_tuwen_ark':
                for key in ('jump_url','preview_url'):
                    if key in params:
                        parsed=urlsplit(params[key])
                        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:raise ValueError('分享链接必须是不含登录凭据的 HTTP(S) 地址。')
                params['group_id' if target['kind']=='group' else 'user_id']=int(target['peer'])
            else:
                message=self.message(grant,cid,params['message_id'],client)
                if api=='get_forward_msg':
                    parts=message.get('message',[])
                    found=[p['data'].get('id') for p in parts if isinstance(p,dict) and p.get('type')=='forward' and isinstance(p.get('data'),dict)] if isinstance(parts,list) else []
                    if len(found)!=1 or not isinstance(found[0],str):raise ValueError('原消息没有唯一可核对的合并转发。')
                    params={'id':found[0]}
                if api=='delete_msg' and str((message.get('sender') or {}).get('user_id'))!=account:
                    if target['kind']!='group':raise ValueError('只能撤回自己发出的私聊消息。')
                    actor=self.admin(client,target);other=self.member(client,target,str((message.get('sender') or {}).get('user_id')))
                    if {'member':0,'admin':1,'owner':2}.get(other.get('role'),3)>={'member':0,'admin':1,'owner':2}[actor['role']]:raise ValueError('不能撤回权限不低于自己的成员消息。')
        elif name=='qq_group_action':
            if not spec['actions'][api]['read'] and api!='set_group_leave':
                if api!='set_group_card' or params.get('user_id')!=account:
                    self.admin(client,target,owner=api in ('set_group_admin','set_group_special_title'))
            if 'user_id' in params:
                self.member(client,target,params['user_id']);params['user_id']=int(params['user_id'])
            if 'message_id' in params:
                self.message(grant,cid,params['message_id'],client);params.pop('group_id',None)
            if api=='set_group_leave':params.pop('confirm',None)
        elif name=='qq_friend_action':
            if api in ('requests','approve_request'):
                self.chat.receiver.start()
                if api=='requests':return client,api,dict(account=account)
                with self.access.connect() as db:
                    request=db.execute('SELECT * FROM qq_friend_requests WHERE account=? AND id=? AND created>?',(account,params['request_id'],time.time()-7*86400)).fetchone()
                if not request:raise ValueError('好友申请不在本账号的近期实时申请列表中。')
                api='set_friend_add_request';params=dict(flag=request['flag'],approve=params['approve'])
            elif 'user_id' in params:
                friends=client.call('get_friend_list',{})
                if not any(isinstance(f,dict) and str(f.get('user_id'))==params['user_id'] for f in friends):raise ValueError('目标不是已核对的好友。')
                params['user_id']=int(params['user_id'])
                if api=='set_friends_category':params=dict(uin=params['user_id'],categoryId=params['category_id'])
        elif name=='qq_profile_action':
            if api=='get_profile':api='get_stranger_info';params={'user_id':int(account)}
            if api=='set_qq_profile' and not params:raise ValueError('至少提供一项要修改的个人资料。')
        elif name=='qq_qzone_action':
            if params.get('ugc_right') in (16,128) and not params.get('target_uins'):raise ValueError('部分好友可见/不可见需要明确 target_uins。')
            for key in ('target_uin',):
                if key in params:params[key]=int(params[key])
            if 'target_uins' in params:params['target_uins']=[int(x) for x in params['target_uins']]
            if 'asset_ids' in params:params['images']=[self.media.encoded(grant,aid,'image') for aid in params.pop('asset_ids')]
        if 'asset_id' in params:
            kind='video' if api=='upload_video_to_qun_album' else 'image'
            params['image' if api=='_send_group_notice' else 'file']=self.media.encoded(grant,params.pop('asset_id'),kind)
        return client,api,params

    def action(self, grant, name, args, cancel, *, queued=False):
        spec=CATALOG[name];entry=spec['actions'][args['action']]
        if not jsonschema.Draft202012Validator(entry['params']).is_valid(args['params']):raise ValueError('操作参数不符合所选动作格式。')
        current,_=self.guard(grant,spec,args,cancel,queued=queued)
        if entry['read']:
            client,api,payload=self.prepare(current,name,args)
            self.check_cancel(cancel)
            if api=='requests':
                with self.access.connect() as db:
                    data=[dict(r) for r in db.execute('SELECT id request_id,user_id,comment,created FROM qq_friend_requests WHERE account=? AND created>? ORDER BY created DESC LIMIT 100',(payload['account'],time.time()-7*86400))]
            elif args['action']=='get_msg':data=self.message(current,args['conversation_id'],args['params']['message_id'],client)
            else:data=client.call(api,payload)
            if args['action']=='history':
                rows=data.get('messages',[]) if isinstance(data,dict) else []
                # Validate the bounded page without an N+1 network query loop.
                verified=[]
                target=self.target(current,args['conversation_id'])
                for item in rows[:100]:
                    self.check_cancel(cancel)
                    if not isinstance(item,dict) or type(item.get('message_id')) is not int:continue
                    try:verified.append(self.verify_message(current,target,item,item['message_id']))
                    except ValueError:continue
                data=dict(messages=verified,requested=payload['count'],coverage='SL 返回的有限页；不保证完整历史',
                          next_message_id=verified[0 if payload.get('reverse_order',True) else -1]['message_id'] if verified else None,
                          paging_note='message_id 是 SL 消息锚点，reverse_order=true 查更旧消息；锚点可能重复，调用方按 message_id 去重。')
            self.guard(current,spec,args,cancel,queued=queued)
            if client.login()!=self.identity(current)[1]:raise AccessDenied('读取期间 SL 账号已改变。')
            data,pages=paginate(data,args.get('offset',0),args.get('limit',50))
            result=clean_result(data)
            if len(dump(result))>180000:raise ValueError('返回内容过大，请缩小分页数量。')
            return dict(data=result,source='snowluma',action=args['action'],pagination=pages,
                        nested_content_bounded=True,note='目录是当前查询结果，分页不是快照；嵌套内容最多100项，单段文本最多12000字。')
        return self.write(current,name,args,cancel,lambda:self.prepare(current,name,args),
                          lambda:self.guard(current,spec,args,cancel,queued=queued),cid=args.get('conversation_id',''))

    def send_bubble(self, grant, row, bubble, key, cancel):
        kind=bubble['kind'];sid=row['id'];cid=row['conversation_id']
        if kind=='qq_action':
            name=bubble['tool']
            if name not in ('qq_interact','qq_message_action'):raise ValueError('聊天队列不支持此类账号/管理操作。')
            args=dict(action=bubble['action'],params=bubble['params'],conversation_id=cid,idempotency_key=key)
            return self.action(grant,name,args,cancel,queued=True)
        flag='chat_voice' if kind=='ai_voice' else FLAGS.get(kind)
        if not flag:raise ValueError('未知附件发送类型。')
        def guard():
            self.chat.validate(sid,grant['id']);self.identity(grant,flag);self.check_cancel(cancel)
            self.chat.turns.before_dispatch()
            state=self.chat.receiver.status()
            if state['state']!='connected' or state['account']!=cid.split(':')[0]:raise ValueError('实时接收未连接或账号已变化，未发送。')
        def prepare():
            client=self.client(grant);target=self.target(grant,cid,client)
            field='group_id' if target['kind']=='group' else 'user_id';payload={field:int(target['peer'])}
            if kind=='ai_voice':
                if target['kind']!='group':raise ValueError('QQ AI 语音目前只支持群聊。')
                payload.update(character=bubble['character_id'],text=bubble['text'])
                return client,'send_group_ai_record',payload
            asset,raw=self.media.get(grant,bubble['asset_id'],kind)
            import base64
            encoded='base64://'+base64.b64encode(raw).decode('ascii')
            if kind=='file':
                payload.update(file=encoded,name=asset['filename'])
                if target['kind']=='group':payload['folder']=bubble.get('folder_id','/')
                return client,'upload_group_file' if target['kind']=='group' else 'upload_private_file',payload
            payload['message']=[dict(type='record' if kind=='voice' else 'video',data=dict(file=encoded))]
            return client,'send_group_msg' if target['kind']=='group' else 'send_private_msg',payload
        return self.write(grant,'chat_'+kind,dict(bubble,session_id=sid,idempotency_key=key),cancel,prepare,guard,cid=cid)

    def call(self, grant, name, args, cancel):
        if name=='get_qq_capabilities':
            return dict(permissions={k:dict(enabled=bool(grant['scope'].get(k)),description=v) for k,v in QQ_FLAGS.items()},
                        actions={name:list(s['actions']) for name,s in CATALOG.items() if grant['scope'].get(s['permission'])},
                        note='接口接入状态，不代表每个 QQ 服务均已实测或当前账号有腾讯侧权限。'),[]
        if name in MEDIA_TOOLS:return self.media.call(grant,name,args,cancel)
        return self.action(grant,name,args,cancel),[]
