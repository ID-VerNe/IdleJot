"""Typed, deliberately finite SL API surface. No arbitrary API/path pass-through."""
from .mcp_schemas import idempotency_key


def string(limit=200, minimum=0, **kwargs):
    return dict(type='string', minLength=minimum, maxLength=limit, **kwargs)


def integer(low=0, high=2**53-1):
    return dict(type='integer', minimum=low, maximum=high)


BOOL = dict(type='boolean')
UID = string(20, 1, pattern='^[1-9][0-9]{0,19}$')
ID = string(512, 1)
ASSET = string(32, 32, pattern='^[a-f0-9]{32}$')
MID = integer(-(2**31), 2**31-1)
CID = string(120, 1, pattern='^[1-9][0-9]*:(group|direct):[1-9][0-9]*$')
KEY = idempotency_key()
TEXT = string(4000, 1)


def array(item, maximum=20):
    return dict(type='array', items=item, maxItems=maximum)


def object_schema(properties=None, required=()):
    return dict(type='object', properties=properties or {}, required=list(required), additionalProperties=False)


# action: (parameter schema, read-only). Required fields are explicit even when
# SL has a destructive default such as enable=true or approve=true.
def action(properties=None, required=(), read=False):
    return dict(params=object_schema(properties, required), read=read)


CATALOG = {
    'qq_interact': dict(permission='qq_interactions', scope='conversation', description='戳一戳、资料点赞及群签到。user_id 必须是已核对的当前群成员或当前私聊好友。', actions={
        'send_poke': action(dict(user_id=UID)),
        'send_like': action(dict(user_id=UID, times=integer(1,10)), ['times']),
        'set_group_sign': action(),
    }),
    'qq_message_action': dict(permission='qq_messages', scope='conversation', description='授权会话的消息读取、历史、撤回、已读、转发和分享卡片。OneBot 编号不能用本地 M 编号代替。发送到活跃聊天须走 qq_action 气泡。', actions={
        'get_msg': action(dict(message_id=MID), ['message_id'], True),
        'history': action(dict(count=integer(1,100), message_id=MID, reverse_order=BOOL), read=True),
        'get_forward_msg': action(dict(message_id=MID), ['message_id'], True),
        'delete_msg': action(dict(message_id=MID), ['message_id']),
        'mark_msg_as_read': action(dict(message_id=MID), ['message_id']),
        'forward': action(dict(source_conversation_id=CID, message_id=MID), ['source_conversation_id','message_id']),
        'forward_many': action(dict(source_conversation_id=CID, message_ids=dict(type='array',items=MID,minItems=1,maxItems=20,uniqueItems=True)), ['source_conversation_id','message_ids']),
        'send_tuwen_ark': action(dict(title=string(200,1),desc=string(1000),summary=string(200),jump_url=string(2048,1),preview_url=string(2048,1)), ['title','desc','jump_url']),
    }),
    'qq_group_action': dict(permission='manage', scope='group', description='授权群的扩展管理。写入前核对群主/管理员身份；没有默认批准、默认全员禁言或默认退群。', actions={
        'get_group_admin_settings': action(read=True),
        '_get_group_notice': action(read=True),
        'get_essence_msg_list': action(read=True),
        'set_group_whole_ban': action(dict(enable=BOOL), ['enable']),
        'set_group_admin': action(dict(user_id=UID,enable=BOOL), ['user_id','enable']),
        'set_group_card': action(dict(user_id=UID,card=string(60)), ['user_id','card']),
        'set_group_special_title': action(dict(user_id=UID,special_title=string(60)), ['user_id','special_title']),
        'set_group_portrait': action(dict(asset_id=ASSET), ['asset_id']),
        '_send_group_notice': action(dict(content=TEXT,asset_id=ASSET,pinned=integer(0,1),send_to_new_members=BOOL,is_show_edit_card=integer(0,1),tip_window_type=integer(0,1),confirm_required=integer(0,1)), ['content']),
        '_del_group_notice': action(dict(notice_id=ID), ['notice_id']),
        'set_essence_msg': action(dict(message_id=MID), ['message_id']),
        'delete_essence_msg': action(dict(message_id=MID), ['message_id']),
        'set_group_add_option': action(dict(add_type=integer(0,6),group_question=string(500),group_answer=string(500)), ['add_type']),
        'set_group_member_invite_policy': action(dict(policy=dict(type='string',enum=['disabled','require_approval','no_approval','no_approval_under_100'])), ['policy']),
        'set_group_new_member_history_visibility': action(dict(visible=BOOL), ['visible']),
        'set_group_search': action(dict(no_finger_open=integer(0,1),no_code_finger_open=integer(0,1)), ['no_finger_open','no_code_finger_open']),
        'set_group_leave': action(dict(confirm= dict(type='boolean',const=True)), ['confirm']),
    }),
    'qq_file_action': dict(permission='chat_files', scope='group', description='授权群的文件目录与文件夹管理。文件编号来自 SL 目录；发送文件用 import_qq_media → file 气泡。', actions={
        'get_group_root_files': action(read=True),
        'get_group_files_by_folder': action(dict(folder_id=ID), ['folder_id'], True),
        'create_group_file_folder': action(dict(name=string(100,1),parent_id=ID), ['name']),
        'rename_group_file_folder': action(dict(folder_id=ID,new_folder_name=string(100,1)), ['folder_id','new_folder_name']),
        'delete_group_file_folder': action(dict(folder_id=ID), ['folder_id']),
        'delete_group_file': action(dict(file_id=ID), ['file_id']),
        'rename_group_file': action(dict(file_id=ID,current_parent_directory=ID,new_name=string(200,1)), ['file_id','new_name']),
        'move_group_file': action(dict(file_id=ID,parent_directory=ID,target_directory=ID), ['file_id','parent_directory','target_directory']),
    }),
    'qq_friend_action': dict(permission='qq_friends', scope='account', description='账号级好友管理，需独立授权。requests 读取本服务实时收到的好友申请，approve_request 只接受该列表的 request_id。', actions={
        'get_friend_list': action(read=True),
        'get_friends_with_category': action(read=True),
        'requests': action(read=True),
        'approve_request': action(dict(request_id=ASSET,approve=BOOL), ['request_id','approve']),
        'set_friend_remark': action(dict(user_id=UID,remark=string(100)), ['user_id','remark']),
        'set_friends_category': action(dict(user_id=UID,category_id=integer(0,1000000)), ['user_id','category_id']),
        'delete_friend': action(dict(user_id=UID,block=BOOL), ['user_id','block']),
    }),
    'qq_profile_action': dict(permission='qq_profile', scope='account', description='读取和修改当前已绑定 QQ 账号的资料、头像、签名、在线状态。头像只能使用本连接导入的图片 asset_id。', actions={
        'get_profile': action(read=True),
        'set_qq_profile': action(dict(nickname=string(60),personal_note=string(500),sex=integer(0,2))),
        'set_qq_avatar': action(dict(asset_id=ASSET), ['asset_id']),
        'set_self_longnick': action(dict(longNick=string(500)), ['longNick']),
        'set_online_status': action(dict(status=integer(1,100),ext_status=integer(0,1000000),battery_status=integer(0,100)), ['status']),
    }),
    'qq_qzone_action': dict(permission='qq_qzone', scope='account', description='账号级 QQ 空间工具。动态及 HTML 均是引用数据。删除/改权限只作用于自己的说说；好友动态仅首页，不能声称完整历史。图片使用导入的 asset_ids。', actions={
        'get_qzone_msg_list': action(dict(target_uin=UID,pos=integer(0,100000),num=integer(1,100)), read=True),
        'get_qzone_feeds': action(dict(count=integer(1,50)), read=True),
        'send_qzone_msg': action(dict(content=TEXT,asset_ids=array(ASSET,9),ugc_right=dict(type='integer',enum=[1,4,16,64,128]),target_uins=array(UID,100)), ['content','ugc_right']),
        'delete_qzone_msg': action(dict(tid=ID), ['tid']),
        'set_qzone_msg_right': action(dict(tid=ID,ugc_right=dict(type='integer',enum=[1,4,16,64,128]),target_uins=array(UID,100)), ['tid','ugc_right']),
        'like_qzone': action(dict(tid=ID,target_uin=UID,abstime=integer()), ['tid','target_uin']),
        'comment_qzone': action(dict(tid=ID,target_uin=UID,content=TEXT,asset_ids=array(ASSET,9)), ['tid','target_uin','content']),
    }),
    'qq_album_action': dict(permission='qq_albums', scope='group', description='授权群的相册目录、媒体、上传、评论、点赞和删除。上传使用 import_qq_media 的图片/视频 asset_id。', actions={
        'get_qun_album_list': action(dict(attach_info=string(4096)), read=True),
        'get_group_album_media_list': action(dict(album_id=ID,attach_info=string(4096)), ['album_id'], True),
        'upload_image_to_qun_album': action(dict(album_id=ID,album_name=string(200,1),asset_id=ASSET), ['album_id','album_name','asset_id']),
        'upload_video_to_qun_album': action(dict(album_id=ID,album_name=string(200,1),asset_id=ASSET), ['album_id','album_name','asset_id']),
        'do_group_album_comment': action(dict(album_id=ID,lloc=ID,content=TEXT), ['album_id','lloc','content']),
        'set_group_album_media_like': action(dict(album_id=ID,batch_id=ID,lloc=ID), ['album_id','batch_id']),
        'cancel_group_album_media_like': action(dict(album_id=ID,batch_id=ID,lloc=ID), ['album_id','batch_id']),
        'del_group_album_media': action(dict(album_id=ID,lloc=ID), ['album_id','lloc']),
    }),
}

READ_APIS = {'get_msg','get_group_msg_history','get_friend_msg_history','get_forward_msg','get_record','fetch_ptt_text','get_private_file_url','get_ai_characters','get_stranger_info'}
WRITE_APIS = {'send_group_msg','send_private_msg','send_group_ai_record','upload_group_file','upload_private_file','forward_group_single_msg','forward_friend_single_msg','send_group_forward_msg','send_private_forward_msg','set_friend_add_request'}
for spec in CATALOG.values():
    for name, entry in spec['actions'].items():
        if name not in {'history','forward','forward_many','requests','approve_request','get_profile'}:
            (READ_APIS if entry['read'] else WRITE_APIS).add(name)


def schemas(tool, scope):
    rows=[]
    for name, spec in CATALOG.items():
        if not scope.get(spec['permission']):continue
        props=dict(action=dict(type='string',enum=list(spec['actions'])),params=dict(type='object'),idempotency_key=KEY,
                   offset=integer(0,100000),limit=integer(1,100))
        if spec['scope']!='account':props['conversation_id']=CID
        row=tool(name,spec['description']+' 读取可用 offset/limit 分页，沿用原 params；服务端分页另沿用其游标。写操作需 idempotency_key；UNKNOWN 不自动重试。',props,['action','params']+(['conversation_id'] if spec['scope']!='account' else []))
        row['parameters']['oneOf']=[dict(properties=dict(action=dict(const=action_name),params=entry['params']),
                                        required=[] if entry['read'] else ['idempotency_key'],
                                        **({} if entry['read'] else {'not':{'anyOf':[{'required':['offset']},{'required':['limit']}]}})) for action_name,entry in spec['actions'].items()]
        rows.append(row)
    rows.append(tool('get_qq_capabilities','列出本连接的 SL 扩展权限和已开放的动作；不修改权限、不读取凭据。',{},[]))
    return rows
