# 怎么开 PR（PR 操作指引）

> 本文件是仓库内所有 Agent 的 PR 操作规范，**必须遵照执行**。
> 配套：怎么发 issue 见 [ISSUE_GUIDE.md](ISSUE_GUIDE.md)；流程总览见 [AGENTS.md](../AGENTS.md) §2.7。

## 定位：PR 是最小可审查单元

- 一 issue 一 PR：PR 是 issue 的实施载体。**合并只代表改动进了 `slim`，不代表症状消失**——行为类 PR 用 `Refs #NN`，issue 由发现者回签后关闭（见下节）。
- 拆分原则：每个 PR 都能独立通过 CI、独立合并、独立回滚；拆不开说明边界没想清，回 issue 里补 plan。
- 大 issue 先拆再派，拆分与命名规则见下节——**分节用 `P+字母`，优先级用 `P+数字`**。

## 关闭方式：Closes 还是 Refs

关键词按 PR 是否改变可观察行为二选一：

| 关键词 | 适用 PR | 关闭时机 |
|---|---|---|
| `Closes #NN` | 纯文档（`doc/`、`*.md`、`.agents/` 规范）、纯重构（不改外部行为） | 合并即关闭，关的是「文档 / 结构已落地」 |
| `Refs #NN` | 行为类：`fix` / `feat` / `perf`，以及任何触及运行时代码路径的 PR | 合并只进待验收队列（`needs-live-verify`），发现者回签后才关闭 |

判据一句话：**合并后是否还需要在真实环境里再观察一次症状才能确认有效**——需要就是行为类。

- 多分节 issue 的每个 PR 都用 `Refs #NN`，全部分节合并后统一回签、一次关闭。
- 同一 PR 既改文档又改行为：按行为类走。
- 行为类 PR 合并后由修复者给 issue 贴 `needs-live-verify`，回签通过后由回签者摘标签并关 issue。
- 历史 PR 不追溯改写（`#50` 等已关闭 issue 保持原状态）。

## 拆分与命名规范

两个维度，各管一事：

| 维度 | 标识 | 语义 | 回答的问题 |
|---|---|---|---|
| 分节（步骤） | `P+字母`：`PA`/`PB`/`PC`…`PZ` | 把 issue 切成有序、可独立合并的步骤；**一个分节 = 一个 PR** | 这是大 issue 的哪一步 |
| 优先级 | `P+数字`：`P0`/`P1`/`P2` | 严重度与紧迫度：P0 阻断（安全/数据损坏/主链不通）、P1 常见路径缺陷、P2 边角与增强 | 先做哪个 |

- 正交使用：先按优先级定实施顺序（P0 先行），同优先级内按分节字母序推进。
- 分节编号：issue 内全局唯一、按序分配、**不随优先级重置**；可跳号预留未启动的分节。
- 分节内多项工作：`P+字母+数字`，如 `PE4` = 分节 PE 的第 4 项。
- 引用格式：`#<issue>-P<字母>[数字]`，如 `#41-PC`、`#41-PE4`。

落点全覆盖：

| 位置 | 形态 |
|---|---|
| issue 正文分节标题 | `### PA · <标题>（P1）`（分节 + 标题 + 该节优先级） |
| 分支 | 多分节 `type/<issue>-p<字母>[-<数字>]-<slug>`（小写），如 `feat/41-pc-stoken-renew`（对应史实 #41-C）；单分节 issue 仍用 `type/<issue>-<slug>` |
| PR 标题 / commit | `type(scope): 摘要（issue #NN 分节 PA）`；单分节 issue 不带分节后缀 |
| PR 正文验收对照 | 按分节列，每条带优先级 |

两种拆分术（命名均符合上述规范）：

- **欠账分节**：#41 把「PR #10 收尾欠账」拆成六个分节，先后开出 8 个 PR（#44–#51），每节独立可合。史实：当时记为 A–F、引用写作 `#41-C`；按现行规范应记 PA–PF、`#41-PC`。
- **方案对比 + 优先级分段**：#53 先出方案对比（P0/P1/P2），P0 一个 PR（#62）、P1 一个（#84）、P2 一个（#85）。P0/P1/P2 即现行优先级标识；每个优先级段内部的实施步骤仍按分节编号。

命名反模式：

- 裸字母分节（A/B/C、「阶段一/二」）——与优先级标识无区分度，grep 必撞车；
- `P0-R1` 式「优先级+序号」混合编号——`.plan/` 历史存档专用，新计划/新 issue 不用；
- 分节编号随优先级重置（P0 段一个 PA、P1 段又一个 PA）——编号必须全 issue 唯一；
- 把优先级当步骤用（「先 P1 再 P2」却把 P1 拆成八个无编号 PR）——优先级定顺序，分节定粒度。

史实保护：`.plan/` 与已关闭 issue/commit 的旧格式（`#41-C`/`#41-E4`/`#41-F`、`P0-R1`、`批次 A`）不回溯改写；`doc/SLASH_COMMANDS.md` 等文档中的历史引用保持原样。

## 分支

```
type/<issue 号>-<slug>          # 如 fix/106-needs-wait-window、docs/83-agent-scope、feat/wg-53-p0
type/<issue 号>-p<字母>[-<数字>]-<slug>   # 多分节时，如 feat/41-pe4-stoken-renew（分节标识见「拆分与命名规范」）
```

- 从最新 `slim` 切，**用 git worktree，不在主工作区切分支**：
  ```bash
  git worktree add .tmp/wt-<issue> -b type/<issue>-<slug> slim
  ```
- `type` 与 issue 的 type 对齐（`fix` / `feat` / `docs` / `refactor` / `chore` / `test`）。
- worktree 用完可 `git worktree remove`，别留在 `.tmp/` 里发酵。

## 标题与 commit

- PR 标题：`type(scope): 摘要（issue #NN）`——三处（分支、标题、commit）都带 issue 号，grep 任一入口都能回到 issue。
  - 范例：`fix(logging): app.log 一次启动只写一份横幅，MCP 线程 INFO 不再冒泡（issue #97）`（#102）。
  - 多分节 issue 加「分节 P<字母>」后缀：`type(scope): 摘要（issue #NN 分节 PA）`。
- commit 消息同样带 issue 号（多分节时同带分节后缀）；一个 PR 允许多个 commit（#105 两个），但每个 commit 自解释，别写 `wip` / `fix`。

## 正文分段

固定结构，**必填段缺一不可**；段名与仓库既有 PR 一致（`Closes` / `Refs` / `问题` / `改动` / `测试`（或`验证`）/ `基线验证` / `门禁改动` / `影响面` / `验收对照`）：

| 段 | 必填 | 内容 |
|---|---|---|
| `Closes #NN` / `Refs #NN` | 是 | 按上节「关闭方式」二选一，放正文末尾（或开头） |
| `## 问题` | 是 | 复述 issue 的现象 + 根因（带 `文件:行号`） |
| `## 改动` | 是 | 按文件列改动，每文件说清「为什么」；新门禁注明登记进 `scripts/gates.json` 的哪个档位，并跑过 `scripts/check_gate_manifest.py --write` |
| `## 测试` / `## 验证` | 是 | 贴 `check_pr_parity.py` 的原始输出（PASS/FAIL 行）与门禁原始输出摘要（✓ 列表）；只贴 ✓ 摘要不够——parity 证明这些 ✓ 是在 CI 等价条件下拿到的 |
| `## 基线验证` | 修红门禁时必填 | 见下 |
| `## 门禁改动` | diff 触及门禁链时必填 | 逐个文件交代改动与三种组合的红绿证据，并附不同上下文的复核结论；见「门禁改动与独立复核」 |
| `## 影响面` | 是 | 行为变化、兼容性、不动的部分 |
| `## 验收对照` | 大 issue 拆分时必填 | 按分节（`P+字母`）逐条对照、每条带优先级；未完成项显式列出并转新 issue |

### 基线验证是硬要求

修「门禁误报/缺陷」类 issue 时，必须证明回归真的钉住了缺陷：

- 同一脚本在**未打补丁的 base**（pristine worktree）上必须红，且红的签名与 issue 症状一致；
- 打补丁后转绿；
- 两者输出都贴进 PR（范例：#102 把过滤器换成 no-op 后门禁以 `{'Started server process': 2, ...}` 失败——正是 issue 里的签名）。
- 只有「打了补丁就绿」没有「不打补丁就红」= 假绿，不予合并。

### 门禁改动与独立复核

门禁是这套协作里唯一的第三方校验，**被测方却可以在同一个 PR 里改门禁**。所以 diff 触及门禁执行链时，验收标准加两条——触发范围（任一命中）：`scripts/check_*.py`、`scripts/gates.json`、`scripts/run_gates.py`、`.github/workflows/ci.yml`、`.githooks/` 下全部钩子（issue #107：pre-push / commit-msg 是门禁执行入口，改钩子与改断言等价）。改清单、执行器、workflow 或钩子都能让门禁静默失效。

**1. 正文必须有 `## 门禁改动` 段**，逐个文件交代四件事：

- 断言改了什么（改前 / 改后各一句，贴关键行）；
- 为什么必须改（原始 issue 的症状如何要求这条改动，不能是「为了变绿」）；
- 三种组合的红绿证据：未打补丁 base / 打补丁 / 只改门禁——**只改门禁必须仍红在原症状上**；
- 是否同步改了文档或清单（`AGENTS.md` §5.1、`scripts/gates.json`）。

**2. 独立复核**：由**不同上下文**的复核 agent 只读 PR diff + 本地复跑门禁，结论（`APPROVE` / `REQUEST_CHANGES`）与复核证据贴进 PR 评论。修复者不能给自己复核。

> 复核方是**用户启动的独立会话或全新上下文的验证 agent**，**不是主水群 Agent 派的 subagent**（AGENTS.md §0，issue #160）——主水群 Agent 的产出止于 issue，不承担复核派活。同一份复核要求在两处读起来必须一致，否则「不派活」与本节打架。

清单至少覆盖：

- 门禁是否仍能钉住原 issue 的症状（不是改到「怎么跑都绿」）；
- 是否删弱或跳过了断言、是否放宽了阈值 / 白名单；
- 基线是否真红（未打补丁的 base 上红，且红的签名与 issue 一致）；
- 是否漏改文档、清单或 `ci.yml`（新门禁必须登记，改清单必须 `--write`）。

机器校验由 `scripts/check_gate_change_review.py` 承担：CI 在 PR 事件读取 diff 与正文，命中门禁链却没有 `## 门禁改动` 段、或段内漏列某个被改文件，直接 fail。本机预检：

```bash
python scripts/check_gate_change_review.py --base origin/slim --body-file <PR 草稿>
```

没有 PR 上下文（本机 parity、`push` 事件）时它打印 `SKIP` 并放行——校验只在有正文的 PR 上有意义。

> 平台侧强制由 `scripts/check_repo_settings.py` 钉住（`manual` 档，合并前后本机跑，`--self-test` 离线验夹具）：可合并维护者（有 push 权限的 collaborator）只有 1 人时，断言「分支保护不要求 review、`.github/CODEOWNERS` 不覆盖门禁路径」——单人下建 CODEOWNERS 等价于自己给自己放行，没要求 review 时它更不产生强制、只制造虚假安全感；此时复核靠「不同上下文的 agent + 结论贴 PR」这一条。出现第二个维护者时该门禁转红，按它打印的命令翻转：CODEOWNERS 把 `/scripts/check_*.py`、`/scripts/gates.json`、`/scripts/run_gates.py`、`/.github/workflows/ci.yml` 与 `/.githooks/`（钩子是门禁执行入口：发布门禁与每推送 sha 的 CI 等价复跑都从那里起，改钩子同样能静默削弱门禁链）归维护者，分支保护 `required_pull_request_reviews` 至少 1 人且 `require_code_owner_reviews` 为 true——少了后者 CODEOWNERS 只是装饰，任意有合并权限者都能批门禁改动。翻转后平台强制生效，两态断言随维护者数量自动切换，设置漂移即红；`--self-test` 另校验翻转模板与磁盘门禁路径零漂移、且 protection 载荷要求 code owner 审查（issue #78）。

## 开 PR 前自查

1. 本地跑 `uv run python scripts/check_pr_parity.py`（issue #61）：在**干净 worktree + 空数据根**里复跑门禁，证明「本地绿」不是靠开发机私有状态绿出来的（CI 等价条件；`--shard static` 只跑一片）。它与 `uv run python scripts/check_publication.py --history`（发布门禁：全部文件 + commit 身份/文案，规则见 AGENTS.md §5.3）一起由 `pre-push` 钩子自动执行。**PR 标题与正文先存草稿，`--text <草稿>` 过了再发**；commit message 由 `commit-msg` 钩子自动拦。
2. **本机红线先跑基线**：同一脚本在未打补丁 base 上复跑一遍，确认是代码问题还是环境问题——环境问题不进修复（既有先例：`check_mcp_chat_turns` 本机端口/websocket 冲突必挂、CI 绿，归环境）。
3. 本机跑一次 `uv run python scripts/check_repo_settings.py`（平台侧只读探测：分支保护 / `delete_branch_on_merge` / `core.hooksPath` / 复核两态政策）。红说明仓库设置漂了，先按它打印的命令修好再推；CI 的 `GITHUB_TOKEN` 读 protection 会 403，这门禁只在 `manual` 档。
4. 确认分支基于最新 `slim`，`git fetch origin slim` 后无落后。
5. **门禁链改动先自检**：diff 触及门禁链时，先跑 `python scripts/check_gate_change_review.py --base origin/slim --body-file <PR 草稿>`，确认「段存在 + 每个被改文件都被列出」不靠 CI 提醒。

## 合并条件（三条全满足才合）

1. **CI 全绿**（`ci.yml` 全部门禁）。平台侧已强制：`slim` 开了分支保护，required status check 为 `checks`，CI 没绿时 GitHub 直接把合并按钮判成 `BLOCKED`——这条不再只靠自觉（issue #58）。
2. **无冲突**：GitHub 显示可合并；有冲突不强行合——在 worktree 里 `git merge origin/slim`（或 rebase）解决，重跑门禁后再推。
3. 适用时有 `## 基线验证` 段。

分支保护同时禁了强推与删分支；合并后远端分支由 `delete_branch_on_merge=true` 自动清掉。三项设置各有复现命令，探测脚本 `scripts/check_repo_settings.py` 红时会直接打印：

```powershell
# 分支保护（strict + required check `checks`，不设 review 人数、不开 enforce_admins）
gh api -X PUT repos/:owner/:repo/branches/slim/protection --input -   # 正文见脚本 PROTECTION_FIX
# 合并后自动删源分支
gh api -X PATCH repos/:owner/:repo -F delete_branch_on_merge=true
# 钩子目录（发布门禁的 commit-msg / pre-push）
git config core.hooksPath .githooks
```

合并方式跟随仓库现状（merge commit，见 #102/#105）；远端 commit sha 与本地对不上是正常的（GitHub 重签），核对内容以 **tree sha** 为准。

> 本机 `git push` 不通时（github.com:443 被代理挡），改走 API 通道推分支：逐 blob 建 tree、建 commit、建 ref，**tree sha 与本地一致才建 ref**；推送后照常 `gh pr create`。

## 合并后：对账与回签

对账是修复者自查，回签是发现者验收，两者都要做：

- **自述 vs 实际逐项核对（修复者）**：PR 说做了的每一条，合并后独立复核（`git status`、文件存在性、门禁原始输出，AGENTS.md §2.6「完成声明以磁盘为准」）。
- **欠账转新 issue**：#41 就是「PR #10 收尾欠账」对账 issue——逐项核对后发现免登录指令被误伤、扫码链路从未实机闭环、P2 续期未做，欠账拆成后续 issue 跟进。合并不算完，对账才算。
- **行为类 issue 回签（发现者 / 全新上下文验证 agent）**：修复者合并后给 issue 贴 `needs-live-verify`；回签者按下面模板贴一条评论，结论为「症状消失」才摘标签并关闭。仍复现则摘标签、留 issue 并补证据。**回签者不能是本次 PR 的作者**。
- 同根因复发时开第二代修复并互相引用（#79 → #89），每代都留门禁墓碑。

### 回签模板

贴到 issue 评论里，字段缺一不可：

- **环境**：`slim` @ `<sha>`，本地实机 / CI，`<时间>`
- **复现命令**：优先原样复用 issue「现象」段里的那条
- **期望输出**：修复后应看到什么
- **实际输出**：粘贴原始输出，不要只写「正常」
- **实机证据**：`up.py --status` 首个非空行 / 日志行 / 群内消息 id 等
- **结论**：症状消失 / 仍复现（仍复现要贴差异）

## 模板（可直接抄）

```markdown
## 问题

<issue 现象复述 + 根因，带 `文件:行号`>

## 改动

`<文件>`
- <改动点 + 为什么>

## 测试

```
<门禁原始输出摘要，✓ 列表>
```

本地同时通过：<门禁名列表>。

## 基线验证

未打补丁 base：<同一脚本红的签名，与 issue 症状一致>
打补丁后：<绿>

## 门禁改动

仅当 diff 触及门禁链（`scripts/check_*.py` / `scripts/gates.json` / `scripts/run_gates.py` / `.github/workflows/ci.yml`）时保留；逐个文件写清改了什么、为什么必须改、三种组合的红绿证据，并贴独立复核结论。

## 影响面

<行为变化 / 兼容性 / 不动的部分>

## 验收对照

- [x] <issue 验收项 1>
- [ ] <未完成项，转 #NN>

Closes #NN
```

行为类 PR 把末行换成 `Refs #NN`，合并后按「合并后：对账与回签」等发现者回签。
