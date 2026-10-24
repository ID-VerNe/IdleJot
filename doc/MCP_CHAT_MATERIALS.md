# 公共 MCP 群聊材料准备

材料准备不改变静默、规划/发送两阶段、相关性判断、默认 3 条引用建议、幂等或 UNKNOWN 保护。

## 公共交付路径

`wait_chat_messages`、`get_chat_session` 和未发送的 stale/needs_refresh `updated_chat` 在公共工具返回层调用同一个 `mcp_chat_materials.prepare`。引用/转发仍复用 `ChatDetails.enrich`，图片复用 `ChatMedia.event_image`、缓存、目录校验、下载和 `pixels` 编码，不包含客户端识别、Hook、私有协议或宿主文件路径。

`read_chat_forward` 的直接转发与引用内分页也复用该图片出口，仅准备返回页中已有的图片索引，预算允许时正文和像素一次交付。显式读取已交付事件不受“只预取未确认消息”过滤；自动 wait/get/updated_chat 仍跳过已确认历史。父 `event_id` 和 `image_index` 不变，失败保留正文及对应图片补读调用，`read_chat_image` 仍可主动调用。

完整批次的程序路径是：准备材料 → 模型规划 `plan_chat_reply` → 模型选择表达、组织内容并 `send_chat_reply(expression_ids,bubbles)`。计划已有的表达候选、引用建议和发送模板继续一次返回。没有完整材料时明确保留额外读取/分页，不能借“两阶段”声称每个场景都只有两次模型请求。

图片在标准 MCP `content` 数组中返回；第 0 块仍为文字/结构化结果，后续为实际 `ImageContent`。`materials.images` 与图片段的 `pixels` 标注 `event_id`、稳定的 `image_index` 和 `content_indexes`，动图另有 `frame_indices`。引用与转发内图片使用父事件编号，不混淆节点作者。

实时取图还会复用聊天记录的已缓存像素：按 QQ 平台、含账号的完整会话 ID 和现有图片 MD5 查询 `message_image_ids`，在当前授权时间范围内优先读取原图，不再要求历史消息与当前图片时间相邻。候选最多 32 项、实际文件读取最多 8 次，仍检查文件边界、MD5 和可用的 SHA-256；缩略图保留标记。索引候选均不可用时继续原消息时间附近的有界读取，两次查找共享最多 8 次文件读取上限；缺少稳定哈希时不猜 native ID 与 OneBot ID 的关系。两套缓存目录保留，不迁移或复制历史文件。

## 历史图片身份索引

索引仅记录已有媒体元数据中的有效 MD5、消息 ID 和图片位置，不计算新身份、不存像素。Store 建表时只建立空表、查询索引和维护触发器；新导入、重复导入补充媒体、媒体更新及会话归属变化同步维护，消息/会话删除随消息外键级联清理。文件读取仍在数据库连接外校验，文件缺失、损坏或不满足原图要求时可走原有补取路径。

旧数据不会在启动或聊天读取时扫描建索引。需要跨时间复用已有历史图片时，由本机操作者显式运行（以下数据库路径按实际位置替换）：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/index_history_images.py --database $env:USERPROFILE\.water-group\data\chats.sqlite3 --batch-size 250 --max-batches 1
```

默认只处理一批，每批独立短事务，最多允许 1000 条。返回 JSON 的 `after_id`、`through_id` 是本批位置和本次历史上界；`has_more=true` 时用返回值继续：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/index_history_images.py --database $env:USERPROFILE\.water-group\data\chats.sqlite3 --batch-size 250 --max-batches 1 --after-id <after_id> --through-id <through_id>
```

命令可中断、接续或从头重复执行；不联系 OneBot，不读取/复制图片。已完成批次立即覆盖，后续新增/更新由触发器维护；没有有效 MD5 或可用媒体路径的旧记录不会被猜测入库，需正常补齐媒体元数据。

同一连接、会话内有可信图片哈希的多个事件入口共享既有实时文件缓存；无哈希的 URL/file 标识可能被复用，只在当前事件及其引用内复用。获取过程用固定数量的锁合并，成功结果在释放锁前记入原 assets 表；失败不缓存，文件缺失或失效可重新获取。已知缩略图继续标记 `thumbnail=true`；细节不足可用 `read_chat_image(prefer_original=true)` 跳过已知缩略图补取，接入端未明确提供原件时不声称拿到了原图。

`pixels_delivered` 表示此工具结果附了图片块；它不证明客户端已经把图片放进模型请求，也不证明模型理解。既有 `seen` 只是像素可交付的门槛；自动准备不填写理解、笔记或知识库，不自动收藏或发送。显式 `read_chat_image` 的既有功能保持兼容。

## 本地图片目录

可选配置保存在本机 `data/mcp-chat-media-policy.json`，不进入 MCP 参数或公开源码。

- `local_roots` 按机器人账号设置可读取适配器路径的可信根目录。
- `hash_cache_roots` 按机器人账号设置额外 QQ 缓存根目录，仅按当前图片 MD5 在固定 `Pic/月/Ori`、`Thumb` 布局中查找，不授权任意路径读取。
- 默认同时检查机器人账号自己的标准 QQ 缓存。原图校验内容 MD5；缩略图校验内容与尺寸，并通过 `thumbnail`、`representation` 标明来源。
- 本地内容不可用时保留原有网络回退；不自动扫描 QQ 历史数据库，不修改代理或 TUN 设置。

## 限制与恢复

- 每个父事件仍最多 8 个图片入口；一次交付最多自动尝试 8 张，复用现有媒体两个并发槽、60 次/小时图片读取额度及媒体分钟额度。媒体额度耗尽不消耗普通查询额度；当前文字批次继续返回。
- 详情补取沿用整批 3 秒预算；自动图片阶段复用该预算值，在每次读取开始前检查。已经开始的图片 I/O 仍受现有单次超时约束，不能保证立即中断系统 DNS 或在 3 秒整点抢占。未开始项明确标为 budget_exhausted，原有取消检查保留。
- 复用原单图 6 MiB、像素/帧数、最多 3 个采样帧及缓存容量限制；原图/缩略图来源、目录边界和账号/授权检查保留。聊天读取不扫描历史数据库、不增加配置项。
- 自动展开和显式 read_chat_forward 默认都交付最多 100 个保留节点；显式 limit=1–100 可选择小页。仍遵守 2 层、12,000 字符及图片预算，节点/段/文字超限明确 truncated，图片超限明确 image_limit；超过总处理上限不保证可分页恢复全部原内容。
- materials.incomplete 仅汇总实际缺失，不等于必须补读：有 has_more 和有效 next_call 继续保留页，有 read_call 仅补对应失败项；仅硬截断、循环、缺编号则按已见材料判断。硬截断与分页可同时存在，混合项目分别处理。失败状态包括 unavailable、permission_denied、image_limit、budget_exhausted、media_busy、rate_limited、cancelled；转发 missing_resource_id 不提供无效重读入口，不会把失败图当成已看图。
- 网络和编码不在聊天全局锁、发送锁或长写事务内。媒体合并仍用短事务，返回前再次核对授权、父事件及引用撤回。材料准备不会推进 cursor；offered/delivered 沿用原消息交付语义，确认仍由处理后的 ACK 完成。
- full 保留完整诊断回执；get 的 full/compact 都按待处理消息分页交付，避免 full 只取最近 20 条却隐藏未确认消息。compact 保留必要前文，已附像素的重复读图模板会移除；失败项不移除。

## 验证边界

start_chat_session 默认使用现有 compact 格式完整交付人格/规则一次，session 不再重复人格正文；显式 response_format=full 和 get_chat_session(full) 保留完整结构。图片自动交付、显式读取和原图升级共用说明：像素返回不等于模型看懂；清晰度不足才补原图，完全看不到已返回图片时说明客户端接入限制，不建议原样反复读图。真实失败重试和读取其他图片仍允许。

扩展现有 `check_mcp_chat_enrichment.py`，使用真实 MCP HTTP 和隔离假 OneBot。检查文字、单图/多图颜色与 content 索引、引用图、25 节点普通转发及末尾图片、补话直接返回像素、取消/撤回/权限不足/坏图/预算与额度耗尽/9 图/超大转发，以及无隐式 ACK、收藏或发送。受控宿主收到完整材料后只调用 plan → send；这只证明工具协议，不是模型请求次数证据。

可选真实 Codex CLI 验证入口：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/check_mcp_chat_materials_clients.py reports/private/mcp-materials-client-check
```

此脚本会调用已配置的外部模型。使用临时数据库、假 OneBot、临时 CODEX_HOME 和临时 MCP 参数，初始材料先经真实 MCP HTTP 取回；图片从返回的 ImageContent 经 CLI 原生图片输入附到第一次生成。读取工具在最终验收中仍可用，不能靠隐藏工具宣称客户端没有额外步骤。测试结束撤销隔离连接并关闭服务，正式 MCP/代理配置不改动。

模型请求计数基于 Codex rollout 的完成响应累计 token_count 用量通知去重，另存精简事件证据，不由 MCP 工具次数推断；包含最终完成回执后的非工具生成。此入口验证 CLI 的原生图片输入与 MCP 规划/发送，未验证 Codex 桌面直接呈现 MCP 图片的路径。结果文件是否落盘、客户端是否自行追加工具说明读取或收尾生成，仍由客户端控制。

客户端实际请求次数、文件读取和图像输入应从其原生轨迹单独验证；服务端工具调用次数不能代替模型请求次数。测试轨迹、模型回答和本机验收记录只保存在忽略目录，不作为公共协议文档的一部分。
