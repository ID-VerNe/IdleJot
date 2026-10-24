# 本地回复分段计时

源码服务加载后，MCP 持续聊天自动在 `data/diagnostics/reply-timing.jsonl` 写入计时回执。
每个完成的工具调用写一行 JSON；收消息的回调另有回执。正在运行的长等待在返回后才写入。
文件达到 8 MiB 后轮转，最多保留当前文件和两份旧文件。不记录聊天正文、Token、QQ 账号、图片 URL 或图片像素。
关联字段仅使用本地会话、批次、计划、事件、操作和工具调用编号。诊断目录仍应作为本机私人数据处理。

在项目根目录生成报告：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/reply_timing_report.py --last 10
```

绘制可切换回复记录的交互时间轴：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/reply_timing_report.py --timeline .tmp/reply-timeline.html
```

时间轴片段可由 Codex 在对话中显示。优先选用新增详细日志，旧回复使用历史调用记录重建并明确标识；
横轴从可读批次生成开始，显示工具调用、宿主处理空档、发送和内部主动等待。展开后查看内部阶段。

若群聊宿主是 Antigravity，可只读关联最新的 Tulpa 群聊对话（无需改动客户端或重启服务）：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/reply_timing_report.py --timeline .tmp/reply-timeline.html --antigravity
```

`--antigravity` 也接受一个明确的本机 conversation DB 路径。自动选择在最近八个对话中寻找含 Tulpa 群聊发送的对话，避免把其他活跃对话当成群聊。
只解析步骤时间和生成计时，不输出聊天、提示词、文件路径、工具正文或凭据。
会话编号和发送时间同时匹配后，才拆分原时间轴中的宿主空档，保留原 Tulpa 测量区间不变。

- `模型请求 · 等待首 token`：Antigravity 记录的 TTFT，包含请求网络、远端排队、输入处理及可能的隐藏推理；不能称为纯推理耗时。
- `模型输出流（含工具参数）`：首 token 后的流式输出时间。
- `MCP 派发／结果返回`：宿主工具执行区间中位于 Tulpa 工具测量之外的部分，包含本机传输及适配器处理。
- `读取工具结果文件`：宿主读取 `.system_generated/steps/.../output.txt` 的工具执行区间。文件操作可能很快，但决定读取和读取后决策各占一次模型调用。
- `宿主调度／未归类`：上述已测区间以外的残余，不能全部认定为调度成本。

适配器字段按本机 Antigravity 2.23.1 内嵌 cortex.proto 核对，且每次校验 TTFT + streaming 与生成步骤结束时间是否一致；
字段缺失或时间不一致的生成记录不归类为模型等待。升级导致无法解析时，应重新核对，不能猜测。
`scripts/check_antigravity_reply_timing.py` 使用合成 protobuf/SQLite 检查解析、只读、时间不匹配拒绝及分段总时长守恒。

主要阶段：

| 阶段 | 标识 |
| --- | --- |
| 入站消息处理、入库时间及事件编号 | `chat.receive`、`received_at` |
| 等待、实际休眠、反复权限校验 | `chat.wait`、`polling.wait.event_sleep`、`polling.chat.validate` |
| 批次创建、上下文及参考材料准备 | `turn.batch`、`chat.guidance`、`learning.plan_context` |
| 规划、队列及发送前新鲜度检查 | `turn.plan`、`turn.submit`、`turn.freshness_check` |
| 气泡间主动等待 | `send.bubble_interval` |
| 全局操作锁排队 | `operation_lock_wait_seconds` |
| 发送准备、目标/引用检查及派发保护 | `operation.prepare`、`send.check_target`、`send.check_reply_target`、`send.dispatch_guard` |
| OneBot 接口、客户端创建、HTTP 往返 | `onebot.<接口名>`、`onebot.client_create`、`onebot.http_roundtrip` |
| 图片缓存、DNS、TCP、TLS、响应头、正文下载、解码/编码 | `image.fetch_or_cache`、`image_cache_hit`、`image.dns`、`image.tcp_connect`、`image.tls`、`image.response_headers`、`image.download_body`、`image.decode_encode` |
| 返回 MCP 结果的构造与 JSON 序列化 | `mcp.serialize_result` |
| 随发送提交表达选择 | `learning.inline_selection`（包含在 `turn.submit` 内） |
| 历史图片缓存与 QQ 本地查找 | `image.history_lookup`、`image.local_lookup`、`image.adapter_local_read` |
| 配置 DNS 与 Fake-IP 后备公网查询 | `image.dns.system_lookup`、`image.dns.tls_query` |

`image.dns` 是解析总时间。现在使用 dnspython 直接查询系统配置的 DNS，查询期限最多 0.7 秒，不调用无期限的 `socket.getaddrinfo`；总 DNS 预算最多 2 秒，并受原下载总期限约束。无回答、超时或全为 `198.18.0.0/15` Fake-IP 时，剩余预算用于同一公共解析服务的 DNS-over-TLS：固定公共 DNS 引导地址 `223.5.5.5`，证书核验名称 `dns.alidns.com`，不另查备用域名、不固定 QQ CDN 地址。TCP、TLS 握手和读取共用绝对期限；公网地址、重定向、下载大小和图片 TLS 校验不放宽。

该备用路径需要能访问公共 DNS 的 TCP 853 端口；不可达时在预算内失败，未修改 Clash/TUN 设置，也不保证任意网络都能取图。没有新增 DNS 缓存或线程池。`dns_fake_ip_detected`、`dns_system_record_count` 和 `dns_tls_record_count` 区分路径；不记录主机名、图片 URL 或查询凭据。接口的 lifetime 与 TLS 期限依据 [dnspython 文档](https://dnspython.readthedocs.io/en/stable/query.html)，引导地址及服务名称见 [AliDNS](https://www.alidns.com/)。

工具根记录的 `result_chars` / `result_bytes` 是紧凑 JSON 返回的字符数 / UTF-8 字节数，可用于检查宿主是否仍把结果落成文件。它们不包含传输包装和图片，不能等同于模型 token 数。普通回复的两阶段协议及 `response_format` 说明见 [持续群聊协议](MCP_CHAT_TURNS.md)。

`spans` 记录开始时刻、单调时钟计算的耗时和父级编号，失败只记录异常类型；规划拒绝另记 `error_code`。
长轮询的高频检查汇总到 `polling`，避免挤掉返回及发送阶段。其他 span 最多 512 个，超出会标记 `truncated`。
**嵌套阶段互相包含，不能直接求和。**

报告会关联批次和发送计划，提供批次到发送调用、计划到发送调用、发送调用到首条 HTTP 回执、
以及材料可关联时的批次最后一条入站消息到首条回执。不同会话分别计算相邻工具的宿主空档。
部分日志轮转后，跨文件的关联材料可能已不存在，报告会省略缺失的指标。

计时边界：

- 入站时间从 Tulpa 收消息回调开始，不涵盖 QQ/SnowLuma 之前的传输。
- 回执表示 OneBot 接口成功返回，不表示接收方客户端已经显示。
- 工具之间的空档包含外部宿主调度、模型推理、传输及其他工作，不能称为纯模型耗时；Tulpa 无法测量外部模型首 token 时间。
- `mcp.serialize_result` 不包含宿主收到数据和图片后的解码、模型处理或网络传输时间。
- 诊断写入失败不改变工具结果，不触发发送重试。

验证：`scripts/check_reply_timing.py` 检查并发隔离、敏感内容排除、异常透传、轮转和不可写目录；
原 `check_mcp_chat_turns.py` 与 `check_mcp_chat_media.py` 验证真实 MCP 协议及隔离 OneBot，不向真实 QQ 发消息。
