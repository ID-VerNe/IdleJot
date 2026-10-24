"""Explicit opt-in permissions for the SL extension tools (never auto-migrated)."""

QQ_FLAGS = {
    'chat_voice': '实时语音读取、转写和发送（含 QQ AI 语音）',
    'chat_video': '实时视频读取和发送',
    'chat_files': '实时文件读取、发送及授权群的文件管理',
    'qq_messages': '授权会话的撤回、转发、卡片及已读操作',
    'qq_interactions': '授权会话的戳一戳、资料点赞、群签到',
    'qq_friends': '当前账号的好友目录、申请、备注、分组及删除',
    'qq_profile': '当前账号的个人资料、头像、签名及在线状态',
    'qq_qzone': '当前账号的 QQ 空间读取、发表、删除、点赞及评论',
    'qq_albums': '授权群的相册浏览、上传、评论、点赞及删除',
}

ACCOUNT_FLAGS = ('qq_friends', 'qq_profile', 'qq_qzone')
MEDIA_FLAGS = ('chat_voice', 'chat_video', 'chat_files')


def live_grant(body):
    return bool(body.get('chat') and body.get('send') or any(body.get(k) for k in QQ_FLAGS))
