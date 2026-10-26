# fix #117 — 追问窗口在 Agent 处理回复期间静默丢弃新消息

worktree: `fix+117-followup-pre-reply-visible`（从 slim 55d4711 起）

## 根因（已核对代码）

`followup.since()`（water-group/daemon/followup.py:180-182）把 `not_before` 当 SQL
过滤条件（`AND at>=?`），被过滤的行不返回给 `consume()`。`consume()` 只推进它看得见
的行的游标（`after_id = max(r["id"] for r in rows)`）——看不见的 pre-reply 行
（Agent 处理期间群友发的消息，`at < not_before`）既不唤醒、也不推进游标，下一轮
`since(after_id, not_before)` 仍在 SQL 层把它们挡掉。它们永久落在过滤条件之外：
`chat_inbox` 里还在，追问窗口再也看不见。issue 的时间线、证据链与代码一致。

## 方案 = issue 的 Plan A

`not_before` 从「可见性过滤」降级为「预算标记」。

## 改动（water-group/daemon/followup.py）

1. `since()`：删 `not_before` 的 SQL 过滤分支。SELECT 加 `at` 列、行字典补 `"at"`，
   让 `consume()` 能逐行判 pre/post-reply。`not_before` 参数保留（不改接口），不再
   用于过滤。
2. `consume()`：`woke` 照常按 self/synthetic/`#`指令过滤后**全部返回**（pre-reply 也
   唤醒——Agent 才看得见处理期间漏掉的消息含图片）。预算只扣 post-reply：
   `chargeable = [r for r in woke if r["at"] >= not_before]`，`budget -= len(chargeable)`。
   游标照常越过整批。
3. `open()` docstring：`not_before` 语义从「窗口只覆盖发送开始之后的发言」改为
   「预算标记：pre-reply 行唤醒但不占额度」。

## 对 #29 的影响（语义变更，明确说出）

#29 当前把 pre-reply 积压做成**不可见**（SQL 过滤）。Plan A 改成**可见但免费**
（唤醒、不占 budget）。#29 的核心不变量——积压不清零预算、不开窗即收窗——保留；
丢的是「不可见」这层，正是 #117 要修的。

`check_followup.py::check_backlog` 的断言要跟着翻面（见下），不是假装没变。

## 回归测试（scripts/）

- `check_followup.py::check_backlog`：断言翻面——积压从 `rows==[]`/`woke==[]`/
  `after_id==anchor` 翻成「照常返回、照常唤醒、游标推过、预算仍 10」。预算不变量
  （`budget==10`）不动。
- 新增 `check_pre_reply_visible`（#117 专项）：开窗 `not_before=now`，开窗前灌 3 条
  `at<not_before`（模拟处理期间群友发言），断言 `since()` 返回、`consume()` 列入
  `woke`、预算不减；再灌 1 条 `at>=not_before`，断言占预算。
- `check_image_visibility.py`：fixture 的 `chat_inbox` 缺 `at` 列（实机表有），
  `since()` 现在 SELECT `at`，补 `at REAL NOT NULL DEFAULT 0` 对齐实机 schema。

## 影响面

只动追问窗口（`followup` + 其插件 + 测试）。`@`/关键词唤醒、`reply.py` 发送链路、
`watch.py` 渲染都不变；`reply.py` 传 `not_before=send_started` 不变（现在只是预算
标记输入）。

## 流程

1. 修 `followup.py`（since/consume/open docstring）。
2. 更新 `check_followup.py::check_backlog` + 新增 `check_pre_reply_visible`。
3. 修 `check_image_visibility.py` fixture schema。
4. 跑 `check_followup.py` + `check_image_visibility.py` 全绿。
5. PR → 无冲突合并。
