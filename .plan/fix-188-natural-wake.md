# fix #188：按群选择 natural / mention_only 触发模式

## 现象与根因

`collector.py:143` 把 `wait_chat_messages` 的请求体写死成 `"mention_only": True`。
服务端 `chatlocal/mcp_chat_wait.py:54` 用 `bool(args.get('mention_only') or
row['participation']=='mention_only')` 合并决定 mention_only，因此服务端即便存了
`participation='natural'`，被这个硬编码 True 覆盖，普通闲聊不投递给监听器。

第二层漏在 `wg_up_wake.wake_reason`（`wg_up_wake.py:266`）：只认 @ / 关键词 / 合并转发，
普通消息返回空串。`watch.py:251` 的 `fresh` 过滤、`reply.list_wakes`、`reply.unanswered`、
`up.report` 全部 `and up.wake_reason(e)`，所以即便采集层放行，监听器读侧也把它再滤掉一次。

> 多群隔离（#164）的前提没有变：一个会话只管一个群。所以这里的「按群」读的是**本会话
> 的群**，由 `wg_isolation.group_id()` 给出——不是按每条消息的 conversation_id 在 collector
> 里分群切换 mention_only（collector 拿到的本就是本群那一个会话的批次）。

## 设计

把硬编码 True 换成「读本会话群的 trigger_mode，natural 给 False、其余（含缺省）给 True」，
并在读侧 `wake_reason` 对 natural 群放行普通消息。两处合起来才打通——只改采集层会被读侧
再滤掉，只改读侧会被采集层先丢掉。

### 1. 读取口：`wg_up_wake.trigger_mode(group_id=None) -> str`

新增一个读口，与 `self_account` / `wake_words` 同一层（`wg_up_wake.py`）。语义：

- 不传 `group_id` 时取本会话群：`wg_isolation.group_id()`。这覆盖 collector / watch /
  reply 三处的默认用法（它们都不知道群号、只认「本会话钉在哪个群」）。
- 显式传群号时读 `config.groups.<gid>.trigger_mode`，缺省回退 `default_trigger_mode`，
  再缺省 `mention_only`。
- 返回值是字符串（`natural` / `mention_only` / …），调用方自己 `!= "natural"` 判断。
  保留字符串是为了 `--status` 行照实印、`bootstrap` 的 `mention_only_bool` 仍可派生。

为什么放在 `wg_up_wake` 而不是 `wg_config`：触发模式是「唤醒判定」的输入（决定 mention_only
与 wake_reason 的口径），与 `self_account` / `wake_words` 同属一层；放 `wg_config` 会把
唤醒语义再往配置层漏一份（`wg_config` 现在只管文件读取与健康度，不掺唤醒判断）。
`wg_isolation` 已经有 `group_id()`，读口复用它，不重复解析群身份。

### 2. 采集器：`collector.py:143`

`"mention_only": True` 改成 `"mention_only": trigger_mode() != "natural"`。
collector 是单会话常驻，群身份在 import 时由 `wg_isolation.resolve()` 钉死（同 `session.GROUP`），
所以这里不需要逐批读，但配置是热文件——为避免「改了 config 要等 collector 重启」，每批现读。
现读的开销是一次 `json.loads`（`wg_config.config()` 已是 file-read，未缓存），与 `wake_words()`
每次调用现读同一口径，可接受。

### 3. 读侧：`wg_up_wake.wake_reason`

在现有判定末尾（@ / 关键词 / 合并转发都没命中时）加一个 natural 放行：

```python
# natural 群的普通消息也算唤醒——叫醒决策交给 Agent，不强制每条必回（#188）。
# mention_only 群走到这里返回空串，维持「闲聊不唤醒」。
if trigger_mode() == "natural":
    return "natural"
return ""
```

放在函数最末、所有显式判定之后，确保 @ / 关键词 / 合并转发仍然优先返回它们的精确理由
（`watch.py:271` 印 `[<reason>]`、`reply.list_wakes` 印 `[{reason}]`、`up.report` 印
`[{wake_reason(e)}]`——普通消息一律印 `natural`，与 `at` / `keyword:xx` 同列）。

`is_self` 早退（函数首行）保留，自己的回显仍不唤醒；`is_command` 早退保留，`#` 指令仍归
`commands.py`，不混进唤醒流。追问窗口（`watch.py:290` `plugins.emit("wake_scan")`）的口径
不变——它是 natural 群关闭时的另一条接话路径，两路不重叠（natural 走 wg_events.jsonl，
追问窗口走 chat_inbox）。

### 4. 文档与引导对齐

- `bootstrap.py:850/870/893`：`mention_only_bool` 已经按 `mode` 派生，natural 群会印
  `mention_only: false`——这部分现状正确，不改。仅校对：把「触发唤醒模式」一行的中文描述
  与新读口一致（natural = 「普通消息可唤醒，Agent 自行决定是否接话」）。
- 多处注释提到「mention_only 根本没把它们投递出来」「collector 用 mention_only=True 只投递
  @ / 关键词」——这些在 natural 群下不再成立。逐处核对：`watch.py:287`、`repeater.py:8`、
  `wg_gap.py:7`、`wg_context.py:69`、`wg_up_probe.py:320` 等。**只改描述性注释**，不改它们
  读的文件口径（`wg_events.jsonl` 在 natural 群下会多收普通消息，这些模块按 id/状态读，不
  依赖「只有 @」的假设——`check_collector_self_echo` 仍钉住自身回显不落盘，自然模式不破坏它）。

## 回归建议（gate）

新增 `scripts/check_trigger_mode.py`，落 `reply` 分片（与 `check_multigroup_isolation` 同档，
口径同源：多群夹具 + 进程内判定）。覆盖：

1. **natural 群普通消息被唤醒**：写一条无 @ 无关键词的群友消息进 `wg_events.jsonl`，
   `trigger_mode='natural'` → `wake_reason` 返回非空、watch `--replay` 出 WAKE、`--list` 出该条。
2. **mention_only 群普通消息不唤醒**（对照组，改前行为）：同一夹具 `trigger_mode='mention_only'`
   → `wake_reason` 返回空串、watch 不出该条。
3. **@ 与关键词优先级不破坏**：natural 群里带 @ 的消息仍返回 `at`、带关键词仍返回 `keyword:xx`
   （不是 `natural`），证明放行在显式判定之后。
4. **is_self / 指令仍排除**：natural 群里自己的回显 `wake_reason` 仍空、`#ping` 仍空。
5. **两群不同配置**：群 A=natural、群 B=mention_only，钉死 A 时普通消息唤醒、钉死 B 时不唤醒——
   证明按群读取、不互相影响（口径同 `check_multigroup_isolation` 的 `conversation` env 钉死）。
6. **collector 请求值来源**：桩掉 `mcp_client.call_tool_raw`，跑 `collector.main` 的请求构造段
   （或直接调一个抽出的「构造请求」函数），断言 natural 群 `mention_only=False`、
   mention_only 群 `mention_only=True`。这一条对应 issue 的「期望输出 hardcoded_mention_only= False」。
7. **判据完整性自检**：把 `wake_reason` 的 natural 放行去掉（桩成只认 @ / 关键词），
   natural 用例必须红——证明断言抓得住，而不是「natural 群本来就什么都出」。

**已知边界**：不覆盖真实群消息发送（issue 明确「不启动采集器、不连真实群」）；
不覆盖服务端 `mcp_chat_wait.py` 的 `participation` 合并逻辑（那是后端契约，本 PR 只改采集请求值）。

## 门禁改动披露

新增 `scripts/check_trigger_mode.py`，登记进 `scripts/gates.json` 的 `ci` 数组（`reply` 分片），
`check_gate_manifest.py --write` 重生成 `AGENTS.md §5.1`。
