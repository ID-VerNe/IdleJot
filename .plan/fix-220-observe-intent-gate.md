# fix #220：observe_message 的意图门——撞前缀的家常话不进小本本

## 现象（已复现）

`parse_command('记在小本本上 摸鱼才是正经事', lambda:[], lambda c:None)` 返回
`{'is_command': True, 'target': '摸鱼才', 'fact': '是正经事'}`。

`BOT_PREFIX`（`memory_commands.py:13`）是 `(?:…)?` **可选**组，`ACTION_PREFIX`
命中即 `is_command=True`。于是群友随口一句以 `记在小本本上 / 记小本本上 / 快记在`
开头的家常话，都被 `observe_message`（`memory_observe.py:406-408`）当成显式指令：

- 凭空建档 `member:摸鱼才`（target 取句首两三字，`memory_commands.py:136-138` 的
  `content.split(maxsplit=1)` 兜底）；
- 把无关内容挂到发言者自己档案的 `key_memories`（`小本本记录: …`），每条发言都
  注入 L1 画像卡。

根因不是解析层（解析层只认语法，命中前缀就算指令是合理的），而是**观察层没有
意图门**：`observe_message` 取 `payload["content"]` 走 `parse_explicit_command`，
全程不看 `segments` 里有没有 @ 本机、不看 reply 是不是指向本机——判据输入
（`segments`）明明在 payload 里，却从不被用上。issue 称之为"意图判据在 TONE
那条兜底上从不生效"。

## 设计：意图门加在 observe_message，不动 parse_command

`parse_command`（解析层）保持不变：文本命中 `ACTION_PREFIX` 仍 `is_command=True`。
这是语法判定，`check_group_memory.py` 的 c1–c8（直接调 `parse_explicit_command`）
不受影响。

意图门加在 `observe_message`（观察层）：`parse_explicit_command` 返回 `is_command`
后，**再判这条消息是不是"在叫机器人"**。判据（与 `followup.py:mention_info`、
`mcp_chat_prompts.py:social_state` 同口径）：

- `segments` 里有 `at` 段 `qq == self_account`（最权威，content 里的 @ 已被显示名
  替换，段里才是原始 qq），**或**
- 文本里显式带了固定称呼 `@机器人 / @bot / @AI / @<self_account>`（segments 被
  清空时的兜底——`mcp_chat.py:482` 超 20KB 会 `payload['segments']=[]`，但 content
  仍在）。

意图不满足 → **降级**走 `extract_interaction`（普通文本），不进 `handle_explicit_command`，
不写 `小本本记录:`、不凭空建档。降级路径对"记在小本本上 摸鱼才是正经事"这类：
`is_direct=False`（无 @ 本机），文本不匹配 interest/trait 正则 → 不记 event、不建
`member:摸鱼才`；发言者若新则按 `sender_name` 建空档（正常的发言者建档，不带假
事实）。

### reply 判据为什么这轮不做

issue 候选 1 列了 reply 指向本机。但 reply 判据要查 `chat_inbox` 里本机 own 回执
（`mcp_chat_wait.py:135` 的 `own_ids`），而 `chat_inbox` 在 `chatlocal/mcp_access`
的 sqlite（mcp_chat 库），`memory_engine` 持有的是 `group_memories.sqlite3`——
**跨数据库，观察层够不到**。传 `own_ids` 进 `observe_message` 会污染签名、且
`receive()` 热路径每条消息加一次 DB 查询，违反最小实现。issue 的三个复现案例
**都不带 reply**，@ 判据足以修复它们。reply 分支留作 follow-up（见 issue 追评）。

### is_direct 一并收紧

`observe_message` 现在 `is_direct = any(s.type in ('at','reply'))`——**任何** at
或 reply 都算 direct，包括 @ 别人、回复别人（issue 点出的"extract_interaction
有 is_direct 参数，observe_message 却只在指令分支之后才算 is_direct"）。收紧到
`is_direct = addressed`（@ 本机），与意图门同源，不再把"@别人"误判为直接互动。

## 改动

### 1. `water-group/memory_observe.py`

新增三个私有辅助 + 改 `observe_message`：

- `_self_account(payload)`：从 `payload["conversation_id"]`（`{account}:{kind}:{group}`）
  split 取 account。取不到回 `""`（`addressed` 恒假，安全降级）。
- `_addressed_to_bot(payload, account)`：遍历 `segments`，`at` 段 `qq == account`
  即 True。reply 分支留注释说明为什么观察层不做。
- `_text_addresses_bot(text, account)`：文本兜底，认 `@机器人 / @bot / @AI /
  @<account>`（固定称呼，与 `memory_commands.BOT_PREFIX` 同源但要求**实际出现**，
  不是可选组）。**不**读 config 的 `self_names`——那是 wake 层（`wg_up_wake` /
  `collector`）的职责，记忆观察层不反向依赖 config。
- `observe_message`：指令命中后过意图门；`is_direct = addressed`。

### 2. `scripts/check_group_memory.py`

新增 `test_observe_intent_gate`，钉 issue 的三段复发：

1. `记在小本本上 摸鱼才是正经事`（`segments` 无 @）→ `observe_message` 不写
   `小本本记录:`、不建 `member:摸鱼才`（断言 `find_profile("摸鱼才")` 为 None）。
2. 同一句带上 `at` 段指向 `self_account` → 写档（断言 `key_memories` 含记录）。
3. `记在小本本上 哈哈哈哈`（`segments` 无 @）→ 不产生 target（断言无 `member:` 凭空
   档、`member_events` 无 `explicit_command` 来源行）。

payload 用 `conversation_id="999:group:g1"` 让 `_self_account` 取到 `999`。

## 不在范围（显式 follow-up，写入 issue 追评）

- reply 指向本机的意图判据（跨数据库，需先统一观察层能否拿到 own_ids）。
- `digest_idle_batch` 同样有撞前缀问题，但其调用方传参不统一（`mcp_chat_wait.py:263`
  传完整 payload 有 segments；`dossier.py:565` 传精简 batch 无 segments、且无
  account），需先统一 digest 入参形态，单开。
- issue 候选 3（误记小本本的审计/清理入口）与 `#34` 的 memory_pollution 合流，单开。

## 验证

- 新增 `test_observe_intent_gate` 过。
- 现有 14 项（尤其 `test_explicit_commands` c1–c8、`test_idle_digest`、
  `test_interaction_extraction`）不回归——它们直接调 `parse_explicit_command` /
  `handle_explicit_command` / `digest_idle_batch`，不经过 `observe_message` 意图门。
- `check_at_segment_name.py`、`check_mcp_chat_targets.py` 不受影响（不改 mcp_chat
  入库段构造）。
