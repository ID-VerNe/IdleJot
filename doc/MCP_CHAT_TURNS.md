# 持续群聊：先决定，再表达

此协议只作用于 MCP 持续 QQ 群聊。完整版的 MCP 与轻量版共用实现；普通问答、回复助手、记忆和历史检索不调用新模型。Planner 和 Replyer 是同一个外部 Agent 的两个工作阶段，不是两个模型服务。

## 使用顺序

1. 更新并启动实际使用目录的 Tulpa，重连 MCP，确认可发现 `plan_chat_reply`、`send_chat_reply`。原连接的账号、群范围和媒体权限继续有效，不自动扩权。
2. 调用 `list_chat_personas`。新增 `little_whale_v2` 显示为“小鲸鱼2号”；原 `little_whale` 和自定义卡仍可选。
3. 停止原群聊会话，用新开始编号 `start_chat_session(persona_preset="little_whale_v2", ...)`。返回的 `session.persona_preset` 和 `chat_prompt.persona` 应为新卡；重复使用旧开始编号仍返回旧快照，不能用它切人格。
4. `get_chat_session` / `wait_chat_messages` 返回 `input_batch`，包含批次 ID、水位、收集原因及同人短连发的 ID 分组。单条时间、来源和消息 ID 不丢失。
5. Planner 调用 `plan_chat_reply`，传批次、`wait / silence / reply`、简短沟通意图；参与时选目标、相关话题消息及表达类型。除了 `text / sticker / reaction`，独立授权后还可用 `voice / video / file / ai_voice / qq_action`，具体字段与示例以连接的 `tools/list` 为准。可附少量主题词。这里只记决定，不发送推理或规划文本。
6. `READY` 后遵循 `next_call`：同一轮选择表达并组织内容，调用 `send_chat_reply` 一次提交 `expression_ids` 和 1–3 个完整语义气泡。有候选时选 0–5 条参考，或用 `expression_ids=[]` 明确不用；漏选返回 `expression_selection_required`，不会派发。没有候选、学习关闭和纯媒体计划自动跳过选择。程序先检查，再依次发送，后续气泡有间隔。一个长解释可以仍是一条；不按标点切句。`select_chat_expressions` 保留为旧宿主兼容入口，正常流程不再单独调用。
7. 看回执，确认已处理消息水位后继续等待。沉默、只用图或小回应同样是完整参与；程序不补发说明文字。

```json
{
  "session_id": "来自开始会话",
  "batch_id": "来自 input_batch.id",
  "idempotency_key": "turn-plan-001",
  "action": "reply",
  "target_event_id": 123,
  "expressions": ["text"],
  "intent": "接住对方刚说完的玩笑"
}
```

```json
{
  "session_id": "同一会话",
  "plan_id": "来自 READY 计划",
  "idempotency_key": "turn-send-001",
  "expression_ids": [],
  "bubbles": [
    {"kind": "text", "text": "第一句完整的话"},
    {"kind": "text", "text": "确实需要时才补充第二句"}
  ]
}
```

单条兼容工具 `send_chat_message`、`send_chat_sticker`、`react_to_chat_message` 保留原参数，新增 `plan_id`；缺少计划返回 `plan_required`。一份计划只提交一个完整队列，组合表达应一次提交。文字首条可选 `quote` / `mention_user_ids`；回应目标来自计划，后续不重复提醒。图片使用既有 `sticker_id` 和已看原图校验。小回应使用既有 `event_id / reaction_id / operation`，原生协议与授权不变。不能通过普通 `send_qq_message` 绕开正在运行的持续聊天队列。

## 引用、转发与恢复

正常与恢复等待模板统一使用 `timeout_seconds=150`，给 Antigravity 的三分钟工具上限留余量；显式参数允许范围不变。换代理先 `list_chat_sessions`，同目标同人格用 compact 接续；用户明确换人格才停止旧会话并以新编号开始。

wait 已交付材料、无需消息翻页且 `decision_required=true` 时，顶层 `next_call` 直接复用 `input_batch.plan_call` 及其必填参数；由外部模型选择 reply、wait 或 silence，不默认确认这批。分页、idle、已选择短等、学习阶段和处理完成仍沿各自原调用继续，读取本身不确认消息。

Planner 的 `WAITING` 表示暂缓决定，不是本批已处理；其 `next_call` 不确认本批，旧客户端附上原水位也不会越过待处理输入。到期前无新输入时继续程序内等待，较短调用超时可返回带 `deferred_until` 的 idle；到期后重新交付原输入和新建的可规划批次，不复用过期 batch。新输入按原收集规则交付并取代旧等待，分页可正常推进。成功回复或沉默后仍正常确认；撤回清空待决输入、停止或撤权不会恢复旧等待，取消仅结束当前调用、保留未处理进度供显式接续。没有后台模型或新调度器。

READY 结果的 `quote_guidance` 提供目标之后已读的他人消息数量，默认 `quote_threshold=3`：达到阈值建议首条文字引用，由同一次 Replyer 生成覆盖或采纳并填写 `quote`，不新增模型请求。自身、系统、撤回和重复事件不计数；不会等凑够条数。实际引用发送仍保留已有的 QQ 目标核验。

接收的引用原文及合并转发在父消息的 `referenced_messages` 中。程序优先使用本地事件或内嵌节点，缺失时在整批 `detail_budget_seconds=3` 的预算下调用内部 OneBot 接口。只展开一层引用；自动交付和显式 `read_chat_forward` 统一默认最多 100 节点，仍遵守 2 层、12,000 字符等原预算；显式 limit 可选 1–100，小页与后续分页保持生效。超限明确标记，不能把转发作者当作当前发言者。

引用原文内的转发复用相同展开路径；补读或分页时原样沿 `read_call` / `next_call`，其中 `reply_index` 定位父事件的引用，`forward_index` 定位引用原文中的转发段。直接转发原调用方式不变，图片仍属于父 `event_id`。现有两层内，内层有完整内联正文就复用，仅有资源编号则通过现有接口补取并使用会话缓存；外层和子转发共用 100 节点、12,000 字符及本批时间预算，重复编号不重复查询，循环停止。失败或时间不足的子项沿所附父事件 `read_call` 重读；不开放任意资源编号查询。`has_more` 仅表示保留节点还有下一页；`unrecoverable_truncation` / `limit_reason` 标明处理上限或循环，最后一页不返回空转续页。超出两层、总量限制和已丢失编号的历史缓存仍不能完整恢复。

等待、会话读取和 `updated_chat` 共用公共 MCP 材料准备：只为当前未确认批次自动取图，以标准 `ImageContent` 随文字返回。`materials.images` 给出父事件 `event_id`、`image_index` 和本次 `content_indexes`（从 0 开始；第 0 块是文字），动图保留 `frame_indices`；消息图片段也携带对应状态。`pixels_delivered` 仅指工具结果附了像素，不证明宿主实际注入或模型看懂。已完整附带的图片/转发无需重复读取；未完成项保留 `read_call` 或分页入口。图片仍优先 Tulpa 缓存、可信本地内容，网络兜底；缩略图明确标记。本地目录配置、具体限制与客户端验收边界见 [材料准备](MCP_CHAT_MATERIALS.md)。

恢复模板使用 `get_chat_session(response_format="compact", known_prompt_version=...)`：直接返回可规划的消息/批次，去掉未变化的静态说明和重复人格；版本变化仍返回完整刷新。优先交付未确认消息，超出一页按 next_call 继续。旧成功操作/回复只保留编号、状态、目标/水位与简短结束摘要，不重复完整回执或表达材料；UNKNOWN、进行中队列、游标与发送审计保留；显式 `response_format="full"` 可查完整结果。新增对外读工具需要外部客户端重连 MCP 才能刷新工具清单。

`expression_selection` 随计划和 `get_chat_session.recent_turns` 返回：区分等待选择、选择若干、明确不用、无需选择和学习不可用，并记录候选/选定条目的 ID、参考载荷哈希与准备时间。它关联既有队列回执，沿用最近 200 个普通结束计划的保留边界；不会额外记录聊天正文。`delivery=tool_result_prepared` 只证明 Tulpa 准备了该工具返回，不能证明宿主将其放入下一次模型请求或模型实际采用了其措辞；这两项明确为 `unverified`。

## 等待与新鲜度

Planner 的 target_event_id 和 topic_event_ids 只能使用所选批次的事件；reply 必须选非空 expressions，wait/silence 省略或 []。这两类可纠正参数拒绝返回原批次有效编号与 recovery.next_call，无需先补读；纠错期间新消息仍使旧批次的回复失效。批次/计划过期则沿 updated_chat 恢复，不自动改写决定或放宽检查。

`wait_chat_messages` 默认 `response_format="compact"`：首次启动/完整读取提供人格与完整规则，后续等待只带动态提示、必要状态和近期目标索引，省去每轮重复的回应目录及长说明。消息正文、消息 ID、图片引用、分页水位和恢复调用模板完整保留；版本变更时仍返回完整更新提示。需要诊断时显式传 `response_format="full"`，或使用 `get_chat_session`。较大的消息批次仍可能被外部宿主保存为文件，Tulpa 不截断群聊内容来强求内联。

普通文字回复目标是两个模型决策阶段：读等待结果并调用 `plan_chat_reply` → 从计划候选中选表达、组织文字并调用 `send_chat_reply`。Tulpa 不额外请求模型。是否额外读取结果文件由外部宿主决定，需要通过宿主计时验证实际模型请求数。

沿用尾部静默，默认 2 秒。等待的首条未处理群友消息作为本次收集锚点；同一话题的连续补充、引用或 @ 机器人会延长它。该发言者转去引用其他话题或 @ 别人后，旁边的对话及其无标记续句不再延长当前等待。最多收集 8 秒，分页仍最多 50 条。超时/达到页上限只返回材料重新评估，不授权抢答；Planner 会按相同的指向规则重新核对所选目标的静默。

相关性从所选目标与话题消息开始，按事件顺序跟踪原生引用链、明确 @ 本人、Agent 提供的少量主题词及参与者的连续补充。明确转向别人的消息优先于“同一发言者”判断；再次引用原话题、@ 本人或命中主题词时仍会触发补读。没有明确转向时，较晚的无标记更正仍保守地视为相关，不按经过时间直接丢弃。未指定目标的主动发言把全部新群友输入视为相关。第二条前还会识别别人引用第一条的插话。这不是完美语义理解：无标记切换话题可能误判；换说话者或转向别人后，没有引用、@ 或共同关键词的含蓄纠正可能漏判，Agent 仍应读上下文并谨慎选目标。普通 get 不绕过静默。

补读 `get_chat_session` 优先交付未确认消息，每页最多 50 条并保留必要前文。处理后沿返回模板确认到已交付末尾；有更多消息时继续分页。读取本身不确认处理进度，不能跳过较早未处理消息。

`stale` / `needs_refresh` 在未发送的拒绝返回中附 `updated_chat`，直接使用其 `context` 和 `input_batch`，沿 `recovery.next_call` 重新规划；`has_more` 时先分页。其他过期结果或未附更新时才按恢复模板补读；`needs_wait` 表示继续等。发送中的相关插话、停止、撤权会取消尚未发送的尾部；已发内容保留。返回每个气泡的 `SUCCEEDED / NOOP / FAILED / UNKNOWN / CANCELLED` 和原操作回执。`UNKNOWN` 不自动重试；重复提交同一计划不会再次发送成功气泡，也不能更改其队列。

不能强制取消所有外部宿主正在生成的文字。可保证的是进入发送接口及每次派发前的检查，不能撤销已交给 QQ 的请求；网络派发与收到新消息仍存在不可消除的极短竞争窗口。服务重启使旧读取批次和未发送计划失效，旧队列不会在后台续发。中断时已派发但没取得回执的气泡保留 UNKNOWN。

已确认 `known_prompt_version` 未变时，compact 等待/恢复另省去已交付的人格名和能力目录，保留版本与动态状态；未知或变化版本仍返回相应完整提示。正常 `send_chat_reply` 的成功文字回执省去重复固定说明，正文、编号、时间、原生消息段和下一步调用不变；失败、UNKNOWN、中断和其他动作的回执保留。`get_chat_session(full)` 仍含原始完整记录。

## 本地配置与状态

可选文件 `data/mcp-chat-policy.json`，默认不需要创建。保存后下次检查生效；非法字段或数值阻止本功能，不静默放宽限制。现有 wait 的 `quiet_seconds` 只控制读取返回节奏，发送至少遵守本地策略。

| 字段 | 默认 | 允许范围 |
| --- | ---: | ---: |
| quiet_seconds | 2 | 0.1–5 |
| max_collect_seconds | 8 | 1–30 |
| reply_cooldown_seconds | 3 | 0–60 |
| batch_ttl_seconds | 120 | 10–300 |
| plan_ttl_seconds | 90 | 5–180 |
| bubble_min_seconds | 1.2 | 0.1–5 |
| bubble_max_seconds | 4 | 0.1–10，不能小于最小间隔 |
| bubble_seconds_per_char | 0.035 | 0–0.2 |

间隔为上一条字数 × 系数 + 最小间隔，封顶；只加在真实的两次发送之间。不同轮默认冷却 3 秒；小回应原有 10 秒冷却及各工具全局限流仍生效。混合队列不会绕过表情发送额度。

`session.turn_process` 显示 COLLECTING、WAITING、DECIDING、REPLYING、SENDING、STOPPED 及最近计划结果。每会话保留最多 64 个读取批次、近期 200 个普通闭合计划；未解决 UNKNOWN 和底层操作回执不按此清理。计划仅保存在本机 MCP 数据库，不形成长期记忆/群友画像，也不新增模型调用。

## 校验与回滚

工具参数、下一步调用模板和 DSH 有限重试见 [水群工具调用与重试](MCP_TOOL_RELIABILITY.md)。

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/check_mcp_chat_turns.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_mcp_language_usage.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_mcp_chat_output.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_mcp_chat.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_mcp_chat_targets.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_mcp_reactions.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_mcp_chat_media.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_mcp_sticker_library.py
```

第一项不替换规划或发送入口，贯通真实 MCP HTTP 与隔离 HTTP/WS OneBot fixture。旧聊天/引用/回应/媒体测试通过明确的测试夹具单独验证适配层，刻意测试未读、伪造目标及派发竞争；它们不代替新协议集成测试。全部自动测试无真实 QQ 写入。真实测试的时间与消息 ID 仅记录在本机私有验收目录，不能用 mock 宣称已经改善所有群的自然程度。

安装版可使用它的 `runtime/python.exe` 调用同一测试脚本，加 `--package <安装目录>`。更新是新增两张独立表，不改授权、原卡或用户数据。备份位置见当次本地更新记录；完全退出 EXE 和托盘后恢复备份程序，保留当前数据库中的兼容表和原操作回执。源码可从开发前检查点另建分支恢复，不 reset/clean 用户工作区。

人工 A/B：分别以新开始编号启动旧卡和2号，确认返回的人格名称及全文；在授权测试群分三条讲一件事，在回复间隙更正它；检查只回应、单条及两条表达是否自然。每次切换先停止旧会话。文件热发现不等于运行中角色会自动更换。

## 参考机制

阅读了 MaiBot 固定提交 `f9ec3670a3638d6a4708ee5253f7ffe2838c9415` 的 [personality 配置](https://github.com/Mai-with-u/MaiBot/blob/f9ec3670a3638d6a4708ee5253f7ffe2838c9415/src/config/official_configs.py)、[决策提示](https://github.com/Mai-with-u/MaiBot/blob/f9ec3670a3638d6a4708ee5253f7ffe2838c9415/prompts/zh-CN/maisaka_chat.prompt)、[表达提示](https://github.com/Mai-with-u/MaiBot/blob/f9ec3670a3638d6a4708ee5253f7ffe2838c9415/prompts/zh-CN/maisaka_replyer.prompt)、[reply 工具](https://github.com/Mai-with-u/MaiBot/blob/f9ec3670a3638d6a4708ee5253f7ffe2838c9415/src/maisaka/builtin_tool/reply.py)及[文本处理](https://github.com/Mai-with-u/MaiBot/blob/f9ec3670a3638d6a4708ee5253f7ffe2838c9415/src/chat/utils/utils.py)。借鉴决策、表达、人格、执行分层的机制；该版本为 GPLv3，本实现没有复制其源码或提示词。保留 Tulpa 的外部 MCP Agent 架构，不引入其第二次回复生成、人类身份或概率/标点拆句。
