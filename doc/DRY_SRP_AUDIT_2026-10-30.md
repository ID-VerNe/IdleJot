# Tulpa DRY / SRP 第四轮审计报告

> **本轮基线**：本仓 `slim` @ `b1371a0`（issue #276 总账声明的接力基线，#169 第三轮已落地）；
> 现状 HEAD `62f46a0`（R5 合入后，2026-10-30）。本轮去重接力 #169：只报它没覆盖到的新问题
> + 它标"记录不修"但理由已失效的项。拆分按 SRP 收益，不为凑行数硬拆（沿用 #169 PF-3 判据）。

- **审计日期**: 2026-10-30
- **审计基线**: `b1371a0`（`git merge-base --is-ancestor b1371a0 slim` = YES）
- **现状 HEAD**: `62f46a0`
- **审计方式**: 三支 Explore agent 并行全仓扫（Python core / scripts+非Python / workflow+残留复验+issue去重），
  所有 file:line 证据主控逐条二次核对；修复阶段 11 支 agent 并行 worktree，每条 byte-identity 自检
- **审计范围**: 全部受控文件（Python 为主，含 .ps1/.cmd/.mjs/.cjs/.json/.yml）
- **编排**: 两阶段 agent team —— 阶段一立 13 个 issue（5 P2 + 6 advisory 拆 + 1 P3 债 + 总账），
  阶段二批 1（R1/R2/R3/R4，4 并行）+ 批 2（A1/A2 无依赖 + A3/A4/A5/A6 依赖 R4 的 assert_runner）+ R5 收尾

---

## 1. 总体结论

issue #276 把第四轮 DRY/SRP 清理拆成 R1–R5（5 条 P2）+ A1–A6（6 条 advisory 带夹具外提）+ R6（P3 验证债）。
本轮 11 条可执行 finding 全部落地合并，1 条 P3 验证债立 issue 占位不盲改。

| # | 发现 | 类型 | PR | 状态 |
|------|------|------|-----|------|
| R1 | `bootstrap.py` 905 行混 5 职责三拆 win32/napcat/tulpa | SRP 拆分 | #292 | 合并 |
| R2 | `mcp_chat_wait.wait()` 278 行抽 self-name 检测块 | SRP 抽函数 | #289 | 合并 |
| R3 | self-name/wake-word 解析三处重复且判定不一致 | DRY 合流 | #297 | 合并 |
| R4 | `check()/FAILED/全部通过` runner 样板 8 文件合流 `assert_runner` | DRY 合流 | #296 | 合并 |
| R5 | `IDLEJOT/TULPA_WG_STATE` env 钉合 24 委派文件/31 调用点，优先级规则进代码 | DRY 合流 | #303 | 合并 |
| A1 | `check_qr_login.py` 904 行拆包 | advisory 拆 | #290 | 合并 |
| A2 | `check_gap_disclosure.py` 972 行拆包 | advisory 拆 | #291 | 合并 |
| A3 | `check_followup.py` 867 行拆包 | advisory 拆 | #300 | 合并 |
| A4 | `check_repeater.py` 915 行拆包 + assert_runner 委派 | advisory 拆 | #299 | 合并 |
| A5 | `check_sixty.py` 795 行拆包 + assert_runner(tick) 委派 | advisory 拆 | #298 | 合并 |
| A6 | `check_reply_send_failure.py` 893 行拆包 | advisory 拆 | #301 | 合并 |
| R6 | reply.py/dossier.py/commands.py P3 验证债 | 占位追踪 | — | 不动 |

批 1（R1–R4）+ A1/A2 无依赖先行；A3/A4/A5/A6 依赖 R4 的 `assert_runner`（runner 是 R4 形态的委派它，
非 R4 形态的 A3/A6 按 byte-identity 保 `@case`/`check_*` 形态不动）；R5 收尾（触及同 4 个 `_fixture.py`）。

---

## 2. R1–R5（P2）落地

### R1 `bootstrap.py` 905 → 297（#292）

`water-group/bootstrap.py` 混 5 职责：win32/sandbox 纯函数（:79-182，零耦合）、QQ 解析+NapCat 启动+WMI 进程跟踪+kill（:184-649）、Tulpa MCP 健康探针（:651-718）、main CLI（:728-905）。#169 §6 标"启动编排单一变更理由"记录不修——第三轮审计报告 §b 复验 seam 可见，判 SPLIT-now，理由失效。

拆后：

| 文件 | 行 | 职责 |
|------|----|------|
| `bootstrap.py` | 297 | `main()`/`ensure_config`/`run_sync` |
| `bootstrap_win32.py` | 151 | `SEM_*`/`silence_crash_dialogs`/`sandbox_dll`/`start_process_detached` |
| `bootstrap_napcat.py` | 498 | QQ+NapCat+WMI+`LAST_LAUNCH` |
| `bootstrap_tulpa.py` | 87 | MCP 健康探针 |

耦合仅 `LAST_LAUNCH` 全局 + 2-3 常量。踩坑：top-level `from water_group import bootstrap` 触发桥接 reload 撕裂模块态 → 改函数体内延迟 import；`LAST_LAUNCH` 再导出是 import-time 快照不回写 → `last_launch`/`_record_launch`/`ensure_napcat`/`LAST_LAUNCH` 全落 `bootstrap_napcat`，门禁 C7 monkeypatch 目标改打 `bootstrap_napcat.LAST_LAUNCH`。

### R2 `mcp_chat_wait.wait()` 278 → 205（#289）

`chatlocal/mcp_chat_wait.py` `wait()` 跨 :45-322，:128-200 是纯局部输入输出块（只写 `has_mention`/`own_sent`/`is_in_dialogue` + 一次 `set_dialogue_window`）。#169 §6 标"覆盖不足先补覆盖再拆"——文件已 219→322 行、覆盖门禁 `check_mcp_chat_dialogue_window.py` 已存在，理由部分失效，partial SPLIT-now。

抽 `_detect_mention_and_dialogue(self, sid, row, pending, account, mention_only, followup_turns, followup_seconds, now, end, window_anchor, dialogue_turns, dialogue_until) -> MentionState` NamedTuple（7 字段：`has_mention`/`is_in_dialogue`/`allow_learning`/`window_anchor`/`dialogue_turns`/`dialogue_until`/`trimmed`）。`wait()` 缩到 ~205 行。踩坑：抽块时漏带 `load_water_group_config` → helper 内延迟 `from .mcp_chat import load_water_group_config`。

### R3 self-name/wake-word 三处合流（#297）

唤醒词解析三处重复且判定不一致：`chatlocal/mcp_chat_wait.py:137-159`（全合并：top+per-group+persona+account）vs `water-group/daemon/wg_up_wake.py:186-188`（仅 top-level `self_names`）；`repeater.py:278-281`、`commands.py:301` 各归一一遍。#169 未触及唤醒词。两边对"什么算唤醒"已不一致（daemon 只认 top-level，server 全合并）。

新建 `water-group/daemon/wg_wake_words.py`（115 行）作为单一出处：top `self_names` + per-group + persona + account，casefold。daemon 与 chatlocal 都 import 它（放 daemon 层避 chatlocal↔daemon 互 import 环）。新建门禁 `scripts/check_wake_words.py`（4 case，A/B fixture，验 per-group wake_words flip）。

### R4 `assert_runner` 收口 8 文件（#296）

8 文件 `def check(name, ok, detail="")` + 尾 runner `if FAILED: print(f"{len} 项失败...")/全部通过` 逐字重复：`check_busboss.py:48`/`check_log_encoding.py`/`check_miyoushe.py:28`/`check_plugin_registry.py:46`/`check_qr_login.py:83`/`check_repeater.py:65`/`check_sixty.py:43`/`check_slash_commands.py:29`。#169 PE-1 的 `run_cases()` 服务 `(name,fn)` case 列表族，**不服务**这个 `(name,ok,detail)` 断言族——是 #169 刻意划在范围外的形态。

`scripts/check_fixture_support.py:189` `class AssertRunner` / `:231` `check_call` / `:261` `def assert_runner(*, markers="ok", indent="")`。8 文件改委派 `_runner = assert_runner(markers="ok")`，本地 `def check` 删（grep `^def check(name` = 0）。A1 拆包后的 `check_qr_login/__init__.py` 也委派（A1 留的 local runner 被 R4 收口）。byte-identity：8 文件输出逐字不变。

### R5 `pin_state_env` 收口 24 委派文件（#303）

改前 24 个委派文件手写 `os.environ["TULPA_WG_STATE"]=...; os.environ["IDLEJOT_WG_STATE"]=...`（共 31 个调用点）；优先级 `IDLEJOT_WG_STATE or TULPA_WG_STATE`（`wg_paths.state_dir()`）只在 `check_reply_send_failure/_fixture.py:45`/`check_followup/_fixture.py:54`/`check_unmute_probe.py:57`/`check_gap_disclosure/_fixture.py:39` 注释里。#169 PE-3 收的是 `temp_dir()`/`scratch_dir()` 目录管理，**没收** env 钉合。

`scripts/check_fixture_support.py:78` `def pin_state_env(state, home=None, *, env=None)`：两腿都钉（`IDLEJOT_WG_STATE` + `TULPA_WG_STATE`），`env=None` 钉 `os.environ`（import-top 用，常量 import 期求值，`patch.dict` 太晚——issue #89），传 `env` dict 钉子进程。优先级规则从注释进代码。24 个委派文件 = 19 单文件 `check_*.py` + 4 个包 `_fixture.py`（check_followup/check_gap_disclosure/check_repeater/check_reply_send_failure）+ 1 个包 `__init__.py`（check_followup/__init__.py，唯一在用例体里也钉 state 的包）；31 个调用点逐个委派。各保留原 scratch 子路径（`/state` vs `/home` 三档用 `home` 参数化，不统一数据根——统一会移数据根、破 byte-identity）。注：check_sixty 的 `_fixture.py` 只钉 `WATER_GROUP_HOME`+`IDLEJOT_WG_MEMORY_DB`、不钉 `WG_STATE`，不在 R5 范围。

**新增静态断言**（不新增门禁 → 不动 gates.json/AGENTS.md/ci.yml）：`scripts/check_path_hygiene.py:197` `scan_state_env_pin()` 断言 `pin_state_env` 存在且同时钉两腿；`:346` A/B self-test 验优先级规则 + 钉合堵穿透（A 只钉 `TULPA_WG_STATE` + 投毒 `IDLEJOT_WG_STATE` → `state_dir()` 读毒；B 两腿都钉 → 读夹具）。

---

## 3. A1–A6（advisory 带夹具外提）

模式统一：夹具层（FixtureHandler 子类 + route 表 + setup helper）外提到 `_fixture.py`，用例留主文件。`gates.json` name 去 `.py` 走包式 `-m`，门禁链同步。byte-identity 自检（用例全绿、输出逐字不变）。`scripts/` 不加 `__init__.py`。

| # | 文件 | 行(前) | 包结构 | runner 形态 |
|------|------|----|------|------|
| A1 | `check_qr_login.py` | 904 | `__init__.py` 787 / `__main__.py` 5 / `_fixture.py` 165 | 委派 assert_runner（R4 收口） |
| A2 | `check_gap_disclosure.py` | 972 | `__init__.py` 65 / `__main__.py` 5 / `_fixture.py` 214 / `cases.py` 750 | `case(fn)` 收集器，非 R4 族 |
| A3 | `check_followup.py` | 867 | `__init__.py` 934 / `_fixture.py` 141 / `__main__.py` 5 | 裸 assert + `  ok  ` + PASS 尾，**非 R4 形态** |
| A4 | `check_repeater.py` | 915 | `__init__.py` 808 / `_fixture.py` 155 / `__main__.py` 5 | 委派 assert_runner(markers="ok") |
| A5 | `check_sixty.py` | 795 | `_fixture.py` 256 / `__init__.py` 622 / `__main__.py` 5 | 委派 assert_runner(markers="tick", indent="  ") |
| A6 | `check_reply_send_failure.py` | 893 | `__init__.py` 801 / `_fixture.py` 197 / `__main__.py` 5 | 37 `@case` 保序，**非 R4 形态** |

**契约保序纪律**（参照 #169 PF-2 / #264 cadence 拆包先例）：`@case`/`check_*` 注册序与原文件逐字一致。A6 的 35 `@case`、A2 的 case 收集器序都保。

**重要修正**：审计期主控一度误判 A3 是 R4 形态（`check()`/`FAILED`/全部通过），实际是裸 assert + `print("  ok  <desc>")` + `PASS` 尾（grep `def check`=0、`FAILED`=0、`全部通过`=0、`PASS`:975、170 asserts）。A6 同理用 `@case`/`CASES` 收集器非 R4 族。这两条按 byte-identity 保形态不动，不强行委派 assert_runner——主控的误述已在 issue #284 追评更正。

---

## 4. 总验（端到端）

基线 `62f46a0`（全部合并后）：

1. **体量门禁** `check_module_size.py`：`OK module size within walls`，`EXEMPTIONS` 空。advisory 带文件 19 → 17（baseline 的 `bootstrap.py` 905 / 6 个 advisory 单体门禁出带；新增 `check_followup/__init__.py` 932、`check_sixty/__init__.py` 622 等仍 advisory 但各叶 < 1000）。
2. **全量门禁** `run_gates.py ci`：90 条全绿（492.3s）。
3. **byte-identity**：6 advisory-split 门禁 + R4 family 8 文件（含 check_qr_login/repeater/sixty 包式），pre-R5（`d1e73a0`）vs R5（`62f46a0`）输出 timestamp 归一后逐字一致。
4. **commit 身份** `check_publication.py --history`：exit 0（author/committer 全 `49872960+ID-VerNe@users.noreply.github.com`）。
5. **11 issue 全关**：#277–#287 全 CLOSED（#288 P3 债 OPEN 占位）。

### 本地直跑的 3 条假阳性（非回归）

总验期本地 `run_gates.py ci` 直报 3 条未通过：`check_followup`、`check_pending_reply.py`、`check_image_visibility.py`。**根因非 R5 回归**：这三条依赖 `wg_isolation.resolve()` 的会话口径，`resolve()` 在 env 未钉 `IDLEJOT_WG_CONVERSATION` 时退化读 live `config.json`——本机 `~/.water-group/config` 恰有一启用群，于是 `active_session_id()` 按那个 live 群的 `conversation_id` 过滤 `chat_sessions`，夹具插入的是 `111:group:123`，无匹配 → `session=''` → `check_open()` 的 `assert state["session"] == SID` 翻车。

证据链：

- pre-R5（`d1e73a0`）直跑同样 exit 1、同一 `AssertionError`（timestamp 归一后字节一致，仅行号 893→891 差）——**改前就翻**。
- `IDLEJOT_WG_CONFIG=<空 groups 配置>` 模拟 CI 干净 checkout（无 live config）→ 三条全绿（`check_followup` PASS / `check_pending_reply` 全部通过 / `check_image_visibility` OK）。
- CI run `41052375170`（PR #303 最后一次）五分片全绿，正是这个环境差。
- R5 只动 state-dir 两腿钉合，**没动** config/conversation 解析；夹具从未钉 `IDLEJOT_WG_CONVERSATION`，`resolve()` 一直读 live config。

这是夹具与 `wg_isolation.resolve()` 的既存环境耦合，CI 因无 live config 而免于触发。不在本轮流水线（不属 DRY/SRP 范畴），记此供后续若要"本地直跑等价 CI"时收口（候选：夹具补 `IDLEJOT_WG_CONVERSATION` 钉合，或在 `run_gates` 层统一隔离 config）。

---

## 5. 踩坑（本轮新增，供后续接力）

- **`## 门禁改动` 段逐路径列**：`check_gate_change_review.py:34-40` 按路径 substring 匹配。A1/A2/R4/A3/R5 初版 CI 都因把多文件归一行（或段缺失）翻车。修法：列出每个触及路径。R5 触 26 文件须逐个列。
- **PR body 编辑不重触发 CI**：`gh run rerun --failed` 复用旧 payload（body 快照）。修法：推空 commit `git commit --allow-empty -m "chore: 触发 CI 重跑"`。R5 的 PR #303 正是 body 段后补、CI 用旧 body 评 → 推空 commit `6d60c96` 后 run `41052375170` 全绿。
- **AGENTS.md §5.1 生成块冲突**：并行 advisory PR 各跑 `check_gate_manifest.py --write`。修法：rebase + 重跑 `--write`（幂等），force-push。
- **包式门禁 gates.json name 去 `.py`**：advisory 拆包把 `check_X.py` 改 `check_X`，门禁链 4 处同步（manifest NAME 正则 / repo_settings gate_paths 收 `check_*/` 目录枚举 / TEAM_CODEOWNERS_FIX / ci.yml 若引用）——参照 #186 PF-2。漏一处即红。
- **`check_mcp_chat_turns` needs_wait / `check_mcp_language_integration` 偶发**：本地过 CI 翻。修法：`gh run rerun --failed` 单分片。
- **R1 桥接 reload 陷阱 + LAST_LAUNCH 快照**：见 §2 R1。
- **A5 `FIXTURE_BASE` 跨模块全局二次赋值**：`check_sixty/_fixture.py` 的 `FIXTURE_BASE` 在 `__init__.py` 又赋一遍——保形态时容忍，记此待后续是否收口。

---

## 6. 仍记录不修（本仓现状，沿用 #169 理由 + 本轮新增）

- **P3 验证债**（issue #288，OPEN 占位）：`reply.py` 998 / `dossier.py` 973 / `commands.py` 884 — 本轮 agent 明确"未读到底别动"。#169 PF-3 的"拆开付耦合税无体量收益"判据（`MemoryProfileMixin` 先例）可能适用，需逐个读完定夺 SPLIT/KEEP。下一轮：每文件派一支 agent 读完 end-to-end 给判定。
- **沿用 #169 记录不修**：JSON 序列化 420/441、`water_group` sys.path 桥、`MemoryProfileMixin`、`antigravity_dry_replay`、advisory 带其余文件（`check_mcp_chat_enrichment`/`check_mcp_chat_turns`/`check_group_memory`/`chatlocal/mcp_chat.py`/`busboss.py`/`miyoushe.py`/`qr_login.py`/`sixty.py`）。
- **已闭环/已去重不重复立**：#255（conversation_id DRY，PR #293）、#211+PR#252（wg_gap group-of）、#204（reply id 命名空间，PR #295）。

---

## 7. 结论

第四轮 11 条可执行 finding 全部合并入 `slim` @ `62f46a0`，体量门禁全绿、`EXEMPTIONS` 空、byte-identity 全保、commit 身份钉对、11 issue 全关。advisory 带文件 19 → 17。R6（P3 验证债）立 #288 占位，下一轮读完 reply/dossier/commands 三文件再定 SPLIT/KEEP。本轮不盲改 P3。
