# 怎么开 PR（PR 操作指引）

> 本文件是仓库内所有 Agent 的 PR 操作规范，**必须遵照执行**。
> 配套：怎么发 issue 见 [ISSUE_GUIDE.md](ISSUE_GUIDE.md)；流程总览见 [AGENTS.md](../AGENTS.md) §2.7。

## 定位：PR 是最小可审查单元

- 一 issue 一 PR：PR 是 issue 的实施与验收载体，`Closes #NN` 闭环。
- 拆分原则：每个 PR 都能独立通过 CI、独立合并、独立回滚；拆不开说明边界没想清，回 issue 里补 plan。
- 大 issue 先拆再派，拆分与命名规则见下节——**分节用 `P+字母`，优先级用 `P+数字`**。

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

固定结构，**必填段缺一不可**；段名与仓库既有 PR 一致（`Closes` / `问题` / `改动` / `测试`（或`验证`）/ `基线验证` / `影响面` / `验收对照`）：

| 段 | 必填 | 内容 |
|---|---|---|
| `Closes #NN` | 是 | 放正文末尾（或开头），合并后自动关 issue |
| `## 问题` | 是 | 复述 issue 的现象 + 根因（带 `文件:行号`） |
| `## 改动` | 是 | 按文件列改动，每文件说清「为什么」；新门禁注明登记进 `scripts/gates.json` 的哪个档位，并跑过 `scripts/check_gate_manifest.py --write` |
| `## 测试` / `## 验证` | 是 | 贴门禁原始输出摘要（✓ 列表），并列本地同时通过的门禁名 |
| `## 基线验证` | 修红门禁时必填 | 见下 |
| `## 影响面` | 是 | 行为变化、兼容性、不动的部分 |
| `## 验收对照` | 大 issue 拆分时必填 | 按分节（`P+字母`）逐条对照、每条带优先级；未完成项显式列出并转新 issue |

### 基线验证是硬要求

修「门禁误报/缺陷」类 issue 时，必须证明回归真的钉住了缺陷：

- 同一脚本在**未打补丁的 base**（pristine worktree）上必须红，且红的签名与 issue 症状一致；
- 打补丁后转绿；
- 两者输出都贴进 PR（范例：#102 把过滤器换成 no-op 后门禁以 `{'Started server process': 2, ...}` 失败——正是 issue 里的签名）。
- 只有「打了补丁就绿」没有「不打补丁就红」= 假绿，不予合并。

## 开 PR 前自查

1. 本地跑相关门禁子集 + `uv run python scripts/check_publication.py --history`（发布门禁：全部文件 + commit 身份/文案，规则见 AGENTS.md §5.3）。**PR 标题与正文先存草稿，`--text <草稿>` 过了再发**；commit message 由 `commit-msg` 钩子自动拦。
2. **本机红线先跑基线**：同一脚本在未打补丁 base 上复跑一遍，确认是代码问题还是环境问题——环境问题不进修复（既有先例：`check_mcp_chat_turns` 本机端口/websocket 冲突必挂、CI 绿，归环境）。
3. 本机跑一次 `uv run python scripts/check_repo_settings.py`（平台侧只读探测：分支保护 / `delete_branch_on_merge` / `core.hooksPath`）。红说明仓库设置漂了，先按它打印的命令修好再推；CI 的 `GITHUB_TOKEN` 读 protection 会 403，这门禁只在 `manual` 档。
4. 确认分支基于最新 `slim`，`git fetch origin slim` 后无落后。

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

## 合并后：对账

- **自述 vs 实际逐项核对**：PR 说做了的每一条，合并后独立复核（`git status`、文件存在性、门禁原始输出，AGENTS.md §2.6「完成声明以磁盘为准」）。
- **欠账转新 issue**：#41 就是「PR #10 收尾欠账」对账 issue——逐项核对后发现免登录指令被误伤、扫码链路从未实机闭环、P2 续期未做，欠账拆成后续 issue 跟进。合并不算完，对账才算。
- 同根因复发时开第二代修复并互相引用（#79 → #89），每代都留门禁墓碑。

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

## 影响面

<行为变化 / 兼容性 / 不动的部分>

## 验收对照

- [x] <issue 验收项 1>
- [ ] <未完成项，转 #NN>

Closes #NN
```
