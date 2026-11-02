"""MCP wire-contract schema rows for the chat tools (issue #11, section PC).

The hand-written schema dictionary used to live inside ``mcp_chat.py``, mixing
the protocol contract with the chat engine. Shared property fragments come from
``mcp_schemas`` (the DRY-3 canonical factories); this module only assembles
rows and delegates to the turn schema module. Plugin-owned tools (reactions,
language learning, member dossiers) are not listed here: ``mcp_tools`` fans
them out through the app plugin registry after these rows.
"""
from .mcp_chat_limits import WAIT_TIMEOUT_SECONDS
from .mcp_chat_turns import TURN_MAX_WAIT_SECONDS, schema as turn_schemas
from .mcp_schemas import SESSION_ID_DESCRIPTION, event_id, idempotency_key, session_id


def schemas(tool):
    # 转发节点上限只认 MAX_NODES：常量在 PA 提到 100 后 schema 仍写 50，
    # 显式重读被压成两页、next_call 的 limit=100 还会被自己的 schema 拒绝（issue #8 分节 PD）。
    from .mcp_chat_details import MAX_NODES
    sid = dict(session_id=session_id(SESSION_ID_DESCRIPTION))
    key = dict(idempotency_key=idempotency_key())
    rows = [
        tool('list_chat_personas', '实时扫描持续群聊人格目录，返回当前可用人格数量 count、名称、id、全文及读取问题。每次新开群聊前调用，文件新增、修改、删除立即生效，无需重启。将选中的 id 传给 start_chat_session.persona_preset；不会开启聊天。', {}, []),
        tool('list_chat_groups', '从 OneBot 列出当前连接允许持续聊天的 QQ 群，返回准确 conversation_id；无需导入历史聊天。',
             dict(offset=dict(type='integer',minimum=0,default=0)), []),
        tool('list_chat_friends', '列出本连接授权的已核对 QQ 好友会话，使用 ACCOUNT:direct:QQ 开始私聊；不包含临时会话。',
             dict(offset=dict(type='integer',minimum=0,default=0)), []),
        tool('start_chat_session', '用户要求在指定授权 QQ 群或已核对好友私聊持续聊天时开启并自动边聊边学，仅当前账号和群，沿用此前暂停偏好，不读历史。先 list_chat_sessions 查同目标；同人格用 get_chat_session(compact) 接续。明确换人格时停止旧会话再用新编号开始。没有可接续会话时 list_chat_personas 查询最新人格，再用其 id 作为 persona_preset；也可填写自定义 persona。新会话保存人格快照，同连接同会话同人格已运行时接续。返回后直接持续执行 next_call/wait；silence 不结束循环，learning_ready 推进一个阶段后按 next_call 先聊天或回 wait，无需用户另给学习指令。需要持续聊天和发送权限。',
             dict(conversation_id=dict(type='string', maxLength=120), persona=dict(type='string', maxLength=6000),
                  response_format=dict(type='string',enum=['compact','full'],default='compact',description='默认完整交付人格/规则一次；full 保留完整排查结构。'),
                  persona_preset=dict(type='string', maxLength=255, description='list_chat_personas 返回的 id，即 Markdown 文件名去掉 .md；支持中文。不要凭旧列表猜测。'),
                  participation=dict(type='string', enum=['quiet','natural','active','mention_only'], default='natural'), **key),
             ['conversation_id','idempotency_key']),
        tool('get_chat_session', '读取持续聊天上下文和进度；恢复时沿用 recovery 参数直接获取精简结果，full 可读取完整人格和协议。不会启动模型或自动发送。',
             dict(**sid,response_format=dict(type='string',enum=['compact','full'],default='full'),
                  known_prompt_version=dict(type='string',maxLength=120)), ['session_id']),
        tool('list_chat_sessions', '列出本连接的持续聊天会话，用于接续或停止。', {}, []),
        tool('read_chat_forward', '分页读取当前已读事件的直接转发或引用原文内转发。优先使用已有内容；仅未完成项沿 read_call/next_call 原样读取，引用位置使用返回的 reply_index。节点内图片用父 event_id 和 image_index；unrecoverable_truncation 表示处理上限之外无法继续。',
             dict(**sid,event_id=dict(type='integer',minimum=1),forward_index=dict(type='integer',minimum=0,maximum=99,default=0),
                  reply_index=dict(type='integer',minimum=0,maximum=7,description='仅引用内转发填写；沿返回调用复制引用位置，forward_index 为引用原文中的段位置。'),
                  offset=dict(type='integer',minimum=0,maximum=MAX_NODES,default=0),limit=dict(type='integer',minimum=1,maximum=MAX_NODES,default=MAX_NODES)),
             ['session_id','event_id']),
        tool('wait_chat_messages', '统一等待群消息或可执行的 learning_ready。消息优先；潜水/silence 也要确认上批并继续等待。learning_ready 时领取一个阶段；claim 返回 task 时独立模型调用后沿 task.submit_call 提交。claim/submit 附 updated_chat 时沿 next_call 先聊天；未领取任务或已提交且无附带聊天时回本工具。idle/冷却/预算暂停继续等待，不结束、不忙轮询。确认上批用 acknowledge_through_id。无需用户手动领取任务。',
             dict(**sid, acknowledge_through_id=dict(type='integer', minimum=0), note=dict(type='string', maxLength=1500),
                  known_prompt_version=dict(type='string', maxLength=120),
                  response_format=dict(type='string',enum=['compact','full'],default='compact',description='默认只返回动态参与信息；完整人格和规则由 start/get 提供。排查时可选 full。'),
                  timeout_seconds=dict(type='number', minimum=1, maximum=180, default=WAIT_TIMEOUT_SECONDS),
                  minimum_wait_seconds=dict(type='number',minimum=0,maximum=TURN_MAX_WAIT_SECONDS,default=0,description='本次调用的等待下限，通常沿用 recovery.next_call；重试前在程序内等待，避免旧批次立即返回造成忙等。上限与冷却同源（见 TURN_MAX_WAIT_SECONDS）：recovery 按冷却给出的等待秒数可达 60，schema 只接 30 会让冷却配大时指导值被静默压小（issue #221）。可被停止或取消打断，不超过 timeout_seconds。'),
                  quiet_seconds=dict(type='number', minimum=0, maximum=5, default=2),
                  limit=dict(type='integer', minimum=1, maximum=50, default=30),
                  mention_only=dict(type='boolean', default=False, description='设为 true 时，仅在收到明确 @ 机器人账号或引用机器人的消息时才唤醒返回；普通群友闲聊在后台保持静默等待并维护上下文窗口，极大节省 Token。'),
                  auto_images=dict(type='string', enum=['candidates','all','none'], default='candidates',
                      description='图片自动投递策略：默认 candidates 只预热 @ 我那条消息自身图与其引用图（每批最多2张），其余给元数据用 read_chat_image 补读；all 沿用旧的全量（每批最多8张）；none 完全不自动交付像素。'),
                  followup_turns=dict(type='integer', minimum=0, maximum=10, default=3, description='被 @ 唤醒后（本机发出回复时重新起算），允许在没有再次显式 @ 的情况下连贯接续互动的消息轮数上限，默认 3 轮。窗口随会话落库，进程重启后仍有效。兼顾对话连贯性与 Token 消耗控制。'),
                  followup_seconds=dict(type='number', minimum=5, maximum=180, default=60, description='被 @ 唤醒后（本机发出回复时重新起算）保持活跃对话窗口的最长静默超时秒数，默认 60 秒。超过此时间未有人继续说话则自动休眠。')), ['session_id']),
        tool('send_chat_message', '在持续群聊中发言，默认普通文本。可分别指定回应事件、显示原生引用和原生 @；近距离且对象明确时不挂标记，话题交错时按需引用，确需点名才 @。分成多条通常仅首条挂标记，后续省略。每次核对授权、目标、停止状态和幂等编号；已授权后直接发送。',
             dict(**sid, **key, text=dict(type='string', minLength=1, maxLength=4000, description='实际正文，CQ 样式文字保持普通文本，不要手写 CQ 或 @昵称冒充消息段。'),
                  reply_to_event_id=event_id('get_chat_session.context / wait_chat_messages.messages 中已读取消息的 id；不是 M 编号、onebot_message_id 或 read_through_id。仅选择回应目标，默认不显示引用。'),
                  quote=dict(type='boolean', default=False, description='需要消除指代歧义时才设 true，且必须指定 reply_to_event_id；不会自动 @ 原作者。'),
                  mention_user_ids=dict(type='array', maxItems=5, uniqueItems=True, default=[],
                                        items=dict(type='string', pattern='^[1-9][0-9]{0,19}$'),
                                        description='确实需要点名的群成员 QQ 号，如消息 sender_id；发送前核对成员身份。不接受昵称或 all，和引用独立。')),
             ['session_id','text','idempotency_key']),
        tool('stop_chat_session', '立即停止本连接的持续聊天并唤醒等待者，取消尚未派发的回复；不撤销其他工具权限。已经交给 QQ 的消息无法撤回。重复停止安全。', sid, ['session_id']),
    ]
    next(s for s in rows if s['name']=='send_chat_message')['parameters']['properties']['plan_id']=dict(type='string',minLength=32,maxLength=32,description='plan_chat_reply 返回的 READY 计划；缺少时拒绝发送。')
    next(s for s in rows if s['name']=='send_chat_message')['parameters']['properties']['expression_ids']=dict(type='array',maxItems=5,uniqueItems=True,
        items=dict(type='string',minLength=32,maxLength=32),description='可随正文一起提交表达选择；[] 明确不用，兼容单独选择工具。')
    return rows + turn_schemas(tool)
