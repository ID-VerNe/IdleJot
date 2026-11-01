# Tulpa DRY / SRP 三轮审计报告

> **本轮基线**：本仓 `slim` @ `c2b0fe9`（issue #169 body 声明的审计基线，2026-10-27）；
> 现状 HEAD `67ce136`（PF-3 合入后，2026-10-28）。本仓是单根历史，起点 `42292a4`
> （2026-10-24「闲笔 slim 快照」），不是 `42292a4` 之前的 fork。前两轮报告
> （`doc/DRY_SRP_AUDIT_2026-10-19.md` / `-20.md`）引用的 `325db52` / `0adf7ac` 与 11 个
> 修复 commit 在本仓 `git cat-file -t` 全报 Not a valid object name——它们的修复产物
> （`onebot_segments.py` / `sqlite_util.py` / `mcp_schemas.py` / `mcp_chat_limits.py` /
> `fixture_server.py` 等）是随初始快照带入的，在 `42292a4` 已存在，但产生它们的 commit
> 不在本仓历史里。本轮用本仓真实基线重写，逐条给 `文件:行号` 证据；旧报告结论的逐条核对见第 4 节。

- **审计日期**: 2026-10-28
- **审计基线**: `c2b0fe9`（slim 的祖先，`git merge-base --is-ancestor c2b0fe9 slim` = YES）
- **现状 HEAD**: `67ce136`
- **审计方式**: 人工逐条 `文件:行号` 复核（`git show` / `git grep` / `wc -l` / AST），无子代理；
  方法论沿用 `-20` §5/§6（见第 5 节）
- **审计范围**: `chatlocal/`（87 文件 / 15357 行）、`water-group/`（64 文件 / 20924 行）、
  `water_group/`（桥接 shim）、`scripts/`（141 文件 / 36015 行）、根入口
- **排除范围**: `tools/`、`data/`、`reports/`、`desktop/`（仅未跟踪 `__pycache__`）
- **范围边界**: 受控 `.js`/`.css` = 0，`web/` 目录不存在——前两轮报告里所有 `web/`/`desktop/`
  发现（platform-label、file_hash、tui views、CSS 调色板）在本仓 N/A（见第 4 节）

---

## 1. 总体结论

issue #169 把全仓第三轮 DRY/SRP 清理拆成 PA–PG 七节。本轮报告兑现 PG-b：用本仓基线重写，
把两条 P1 缺陷（门禁说谎 + 截断超上限）的复现与修复、PC/PD/PE/PF 六节 P2 的落地结果钉成可核对的
现状，并把两份旧报告的结论逐条标注「仍成立 / 已失效 / N/A」。

| 分节 | 内容 | 优先级 | PR | 状态 |
|------|------|--------|-----|------|
| PA | 体量门禁漏 `scripts/`；截断门禁漏 3 handler + 漏变量切片写法 | P1 | #173 | 完成 |
| PB | 三份 `_clip` 合流 `wg_text`；补 `DAEMON_RENDER_FILES` 与变量切片判据 | P1 | #173 | 完成 |
| PC | 只读连接合流 `sqlite_util.connect_ro`，收掉手写 `as_uri()+?mode=ro` | P2 | #175 | 完成 |
| PD | 原子写合流 `wg_daemon.write_json`；先修 `wg_gap.py` 固定临时名缺陷 | P2 | #176 | 完成 |
| PE-1 | `bootstrap_paths()` + `run_cases()` 收口 `scripts/` path 注入与 CASES runner | P2 | #177 | 完成 |
| PE-2 | 13 处 `ThreadingHTTPServer` 收口 `fixture_server()`；删孤儿 `management_http()` | P2 | #178 | 完成 |
| PE-3 | 73 处手写 tempdir 收口 `temp_dir()`/`scratch_dir()` | P2 | #180 | 完成 |
| PF-1 | `MCPChat` 画像/笔记四方法外提 `mcp_chat_dossier.py` | P2 | #181 | 完成 |
| PF-2 | `check_listener_rehang.py`（2220 行）拆包 + 门禁执行链支持 `-m` | P2 | #186 | 完成 |
| PF-3 | `MemoryDossierMixin` 污染块外提 `memory_pollution.py` | P2 | #187 | 完成 |
| PG-a | 两份旧报告标注来源与失效边界 | P1 | #174 | 完成 |
| PG-b | 第三轮报告用本仓基线重写（本文件） | P1 | 本 PR | 完成 |

PA/PB 是强依赖（门禁先补、再修绿），PG-a/b 与 PA 并行，PC/PD/PE/PF 互相独立。本 PR 合入后
#169 七节全部落地。

---

## 2. 两条 P1 缺陷的复现与修复

### 复现 A：体量门禁漏扫 `scripts/`

**基线 `c2b0fe9` 实测**：

- `scripts/check_module_size.py:32` `SCAN_DIRS = ('chatlocal','water-group','water_group')`，不含 `scripts`。
- `EXEMPTIONS = {}`（`:43`），`MAX_FILE_LINES=1000`（`:33`）对 `scripts/` 131 个脚本零生效。
- `scripts/check_listener_rehang.py` 2220 行（`git show c2b0fe9:scripts/check_listener_rehang.py | wc -l` = 2220），
  门禁本体跑出来 `OK module size within walls`——门禁说谎。

**PA 修复（PR #173）落地**：

- `SCAN_DIRS` 加 `'scripts'`，现 = 四元组 `('chatlocal','water-group','water_group','scripts')`
  （`scripts/check_module_size.py:32`）。
- `check_text_truncation.py` 两个判据同补：`_bare_slice_hits` 认变量/BinOp 切片（`text[:limit]` /
  `text[:limit-1]`），`_tombstone_concat_hits` 认「正文变量切片 + 非空字符串常量尾巴」；
  `DAEMON_RENDER_FILES` 加 `sixty.py` / `miyoushe.py` / `busboss.py`（`:240-249`）。
- 反例自检：契约 `13a`（命中变量/BinOp 切片 + 非空尾巴）、`13b`（放过空尾巴 / 非字符串 / 非正文 /
  子串切片，不误伤合法拼接），照 `check_module_size._self_test_body`（`:162`）的 A/B 夹具纪律。

**现状**：

```
$ .venv/Scripts/python.exe scripts/check_module_size.py
OK module size within walls (file<=1000, class<=800, 0 exemptions pending issue #11 sections)
```

19 个文件报 ADVISORY（700 行带，可见不致命）：`chatlocal/mcp_chat.py:753`、`water-group/bootstrap.py:905`、
`water-group/daemon/{dossier.py:973, reply.py:948, qr_login.py:891, commands.py:851, miyoushe.py:811,
busboss.py:768, sixty.py:730}`、`scripts/{check_dossier_cadence.py:923, check_gap_disclosure.py:923,
check_qr_login.py:904, check_mcp_chat_enrichment.py:862, check_sixty.py:795, check_group_memory.py:781,
check_followup.py:765, check_reply_send_failure.py:753, check_repeater.py:752, check_mcp_chat_turns.py:721}`。
无一个越 1000 FAIL 线；`EXEMPTIONS` 为空（PF-2 拆包后清空，见 §3 PF-2）。

### 复现 B：三份 `_clip`，两份超上限

**基线 `c2b0fe9` 实测**（字符数算即可复现，无需跑模块）：

| 文件 | 行 | 实现 | 尾巴长度 | 切片让位 | 净超 |
|------|----|------|----------|----------|------|
| `water-group/daemon/sixty.py` | 220 | `text[:limit] + " …（已截断）"` | `len(" …（已截断）")=7` | 0 | +7 |
| `water-group/daemon/miyoushe.py` | 464 | `text[:limit] + " …（已截断）"` | 7 | 0 | +7 |
| `water-group/daemon/busboss.py` | 535 | `text[:limit - 1] + "…"` | `len("…")=1` | 1 | 0 |

即 issue #169 body 复现 B 的 `[('sixty',7),('miyoushe',7),('busboss',0)]`：`sixty`/`miyoushe` 先切到上限
再加 7 字符尾巴，净超 7；`busboss` 切到 `limit-1` 再加 1 字符尾巴，守得住上限。

**PB 修复（PR #173）落地**：

- 三份 `_clip` 改委派 `wg_text.render_handler_reply(text, limit)`（`water-group/daemon/wg_text.py:152`），
  各自的 `REPLY_CLIP`（`sixty:38`=1200 / `miyoushe:37`=480 / `busboss:61`=1200）保留——它们是各模块的
  业务上限，不是实现细节。
- `sixty._bounded`（`:685`）/ `miyoushe._bounded`（`:772`）改委派 `wg_text.bounded_handler(fn, *, limit, need_ck)`
  （`wg_text.py:185`）。`need_ck` 默认值差异（米游社 `True` / 60s `False`）是各模块隐私策略，留在各自
  `_bounded` 默认里，不进共享装饰器——米游社刻意 `True`（新加 handler 忘标宁可被绑定检查拦住），
  60s 全部指令免凭证（`False`）。

**现状**：

```
$ .venv/Scripts/python.exe -c "import sys;sys.path.insert(0,'water-group/daemon');
import sixty,miyoushe,busboss;s='x'*2000;
print([(m.__name__,len(m._clip(s))-m.REPLY_CLIP) for m in (sixty,miyoushe,busboss)])"
[('sixty', 0), ('miyoushe', 0), ('busboss', 0)]
```

三个元素全 0，任何模块的输出都不超过自己声明的 `REPLY_CLIP`。

---

## 3. PC/PD/PE/PF 落地（P2）

每节给基线债务数、修复 PR、现状数、残留（若有）及残留是否合法。

### PC（#175）只读连接合流

**基线债务**：11 处手写 `DB.resolve().as_uri() + "?mode=ro"` + `uri=True`（`followup.py:106/251/407/431`
、`reply.py:547/572`、`wg_text.py:151`、`dossier.py:82`、`wg_gap.py:68`、`mint_grant.py:51`、
`investigation_sql.py:71`）。`chatlocal/sqlite_util.connect(path, *, timeout, ...)` 无只读入口，
只读样板逐字重复；`wg_gap.py:63` 把「拼接 `?mode=ro` 会被 sqlite 当文件名」写成注释——知识只在注释里。

**修复**：

- `chatlocal/sqlite_util.connect_ro(path, *, timeout=5.0)`（`sqlite_util.py:46`）：内部走
  `Path(path).resolve().as_uri() + "?mode=ro"` + `uri=True`。Windows 上裸 `file:{path}` 带反斜杠
  会被解析成文件名、`?mode=ro` 被当文件名一部分（`unable to open database file`），必须 `as_uri()`。
- `wg_text.connect_ro(path, *, timeout=5.0)`（`wg_text.py:89`）：`chatlocal.sqlite_util.connect_ro`
  的延迟导入门面。daemon 不能模块级 `import chatlocal`——`chatlocal/config.py` 在 import 期建数据目录
  （`wg_text.py:23` 注释），沿用 `wg_text.clip()` 那种门面范式。
- `investigation_sql.py` 跑不可信 SQL 带 progress handler，原 `timeout=0.5` 保留：调
  `connect_ro(path, timeout=0.5)`。

**现状**：6 个 caller 走 `connect_ro`（`wg_gap`/`wg_text`/`reply`/`dossier`/`mint_grant`/`investigation_sql`）。
残留 2 处 `as_uri()`：`wg_text.py:93` 是门面自身 docstring 引用、`wg_gap.py:64` 是迁移注释——非债务。

### PD（#176）原子写合流 + 缺陷修复

**基线债务**：14 个文件手写 `os.replace`（`reload.py`/`qr_login.py`/`bootstrap.py` 各 2 处、
`wg_send_failure.py`/`wg_gap.py`/`wg_daemon.py`/`user_store.py`/`miyoushe.py`/`busboss.py`/
`mcp_sticker_library.py`/`mcp_qq_media.py`/`mcp_connect.py`/`mcp_chat_media.py`）。真正 import
`wg_daemon.write_json`（`wg_daemon.py:99`，唯一临时名 `{name}.tmp.{pid}.{token_hex(4)}` + `os.replace`）
的只有 4 个文件。**数据面已长成缺陷**：`wg_gap.py:234` 的 `tmp = STATE.with_name(STATE.name + ".tmp")`
是固定名——两个进程同时 `record()` 互相覆盖临时文件，落盘的是后写者的一部分内容，缺口账本被悄悄截断。

**修复**：

- 先修缺陷：`wg_gap.py` 的 `record()`（`:233`）改走 `wg_daemon.write_json(STATE, entry)`（`:244`），
  唯一临时名 pid+随机后缀，并发写互不踩；`write_json` 失败返回 `False` 而不抛，与原 `except` 等价。
- 再合流 14 处手写 `os.replace` 迁 `wg_daemon.write_json`。

**现状**：`write_json` caller 从基线 4 增至现状 10（`miyoushe`/`qr_login`/`reload`/`reload_exec`/
`reload_plan`/`user_store`/`watchdog`/`wg_daemon`/`wg_gap`/`wg_send_failure`）。

### PE-1（#177）path 注入 + CASES runner 收口

**基线债务**：`sys.path.insert` 在 `scripts/` 出现 146 次（occurrence 级，`git grep -c` 核过），
形态 13 种（`str(ROOT)` 31 次、`str(ROOT / "water-group" / "daemon")` 28 次、
`str(Path(__file__).resolve().parent)` 24 次…）；`ROOT = Path(__file__).parents[1]`/`.parent.parent`
定义 67 份；12 个脚本用 `for fn in CASES` 跑自检，runner 块 6 种变体。

**修复**：

- `check_fixture_support.bootstrap_paths(*, daemon=True, scripts=True, root=True) -> ROOT`：一次注入
  `scripts/` / `ROOT` / `water-group/daemon` 三条 path，幂等。
- `run_cases(cases, *, setup=None, prefix=True, catch=True) -> int`：承载 11 个文件三族 `main()` 循环差异
  （A 族中文 ✓/✗ +「全部通过」、B 族英文 PASS/FAIL、C 族无 try/except 裸跑）；`catch=True` 把抛异常的用例
  记红不停在第一个（门禁自己坏了也算红，issue #62）。

**现状**：`bootstrap_paths` 19 caller、`run_cases` 11 caller。`sys.path.insert` 基线 146 → 现状 124。
残留：77 文件仍自带 `sys.path.insert`、5 文件仍 `for x in CASES`——均为 PE-1 PR 正文明确划在范围外的
特殊形态（`fixture_server` 包裹 / `(label,fn)` 元组收集 `FAILURES` / `(name,case)` 非 `@case`），
逐文件核对过差异是故意的，记录在案不修（见 §6）。

### PE-2（#178）假服务器 + 孤儿助手

**基线债务**：13 处自带 `ThreadingHTTPServer`（`check_mcp_chat_turns.py:234`、`check_mcp_chat_enrichment.py:566`、
`check_mcp_chat_materials_clients.py:83` 等）；`scripts/fixture_server.py:62` 的 `fixture_server()` 已存在
（根提交带入），但只 12 caller。孤儿助手 `check_fixture_support.py:116` 的 `management_http()` 全仓 0 引用
——抽象出来了但没人用，反向 DRY 债。

**修复**：13 处迁 `fixture_server()`；3 个自带服务器的门禁（`check_mcp_chat_turns`/`_enrichment`/
`_materials_clients`）现 `ThreadingHTTPServer=0`、各 2 处 `fixture_server` 调用；孤儿 `management_http()`
删除。

**现状**：`fixture_server` 26 caller。残留 2 处 `ThreadingHTTPServer`：`fixture_server.py` 自身（助手定义，
合法）+ `antigravity_dry_replay.py:37`（WS/replay + `def restrict` 网络护栏持有者，形态特殊，是 `-19` DRY-1
收口后剩下的网络护栏副本，见 §4）。

### PE-3（#180）临时目录收口

**基线债务**：75 处手写 `TemporaryDirectory`/`mkdtemp`；真正调 `check_fixture_support.temp_dir()`
（`@contextmanager`，`:154`）的只有 8 个文件。**坑**：70 个手写处大多是 `mkdtemp()` 裸路径，迁移要改成
`with temp_dir() as tmp:`，不是换函数名——否则把 yield 当返回值用是假绿。

**修复**：73 处迁 `temp_dir()`/`scratch_dir()`。

**现状**：`temp_dir`/`scratch_dir` 84 caller。残留 6 文件命中 `TemporaryDirectory`/`mkdtemp`，全部合法：
`check_fixture_support.py:32/81`（定义 `temp_dir` 自身，用 `mkdtemp` 钉夹具 home）、`antigravity_dry_replay.py:48`
（`patch.dict(os.environ,clear=True)` 清环境模式）、`bench_napcat_qq_fixture.py:59`
（`dir=ROOT/'.tmp'` 仓库内 bench 目录）、`check_pr_parity.py:110`
（pristine worktree，在 `scripts/` 跑 run_gates 外）、`check_listener_rehang/ownership.py`
（命中的是 `from ._common import ... temp_dir` 的 import 行，已用 `temp_dir`）。

### PF-1（#181）MCPChat 画像/笔记四方法外提

**基线债务**：`chatlocal/mcp_chat.py` 817 行，`MCPChat` 类跨 73-814（743 行 / 37 方法），同时管会话生命周期、
权限校验、画像查询（`query_member_dossier`/`update_member_dossier`/`consolidate_chat_memory`/
`query_group_notes` 四方法是明确的第二职责，与 `mcp_chat_turns`/`mcp_chat_media` 同一拆分范式）。

**修复**：四方法外提到 `chatlocal/mcp_chat_dossier.py`（82 行）。`mcp_chat.py` 817→753；
`MCPChat` 类跨 743→681（73-753）、37→33 方法。

**现状**：

```
$ mcp_chat_dossier.py::query_member_dossier: 34-40
$ mcp_chat_dossier.py::update_member_dossier: 42-58
$ mcp_chat_dossier.py::consolidate_chat_memory: 60-73
$ mcp_chat_dossier.py::query_group_notes: 75-82
```

`mcp_chat.py` 仍在 ADVISORY 带（753），但无墙压力；继续拆分收益递减（已有 `mcp_chat_turns`/
`prompts`/`output`/`details`/`media`/`materials`/`hints`/`limits`/`schemas`/`wait`/`dossier` 十一个伴生模块）。

### PF-2（#186）check_listener_rehang 拆包 + 门禁执行链支持 `-m`

**基线债务**：`scripts/check_listener_rehang.py` 2220 行 / 53 case / 44 契约，是全仓最大单文件且**不在
体量门禁范围内**（PA 修前 `SCAN_DIRS` 不含 `scripts`）。文件内只有 3 处分节注释（`:499`/`:542`/`:574`），
其余 2000 行无结构标记。它是 `check_module_size.EXEMPTIONS` 里唯一一条（PA 修后纳入扫描、登记 exemption
绑定到 PF）。

**修复**：

- 拆成 `scripts/check_listener_rehang/` 包（`__init__`/`__main__`/`_common`/`exit_hint`/`budget`/
  `ownership`/`ledger`/`watch` 8 文件）。53 个 `@case` 运行顺序与原文件逐字一致——`__init__.py` 导入序
  = 子模块内源码序 = 旧文件 `@case` 注册序；契约编号（1-44）靠保序保全局唯一。
- issue #182 的 3 条游标前未回唤醒回溯补扫 case（含 contract 44）落在 `exit_hint.py`，位于「冷启动
  不翻历史积压」与「发送后默认续挂」之间，与原文件物理位置一致。
- 门禁执行链改支持 `python -m scripts.<name>` 调包式门禁：`run_gates.command_of` 加 `.py` 后缀分支
  （有 `.py` 走路径脚本、无 `.py` 走 `[python,-m,scripts.X]`）；`gates.json` 第 7 行
  `check_listener_rehang.py`→`check_listener_rehang`；`check_gate_manifest` 的 `NAME` 正则放宽允许
  无 `.py` 包名、`disk` parity 加 `check_*/` 目录枚举；`check_repo_settings` 的 `gate_paths()` 收
  `check_*/` 目录枚举、`TEAM_CODEOWNERS_FIX` 改 `/scripts/check_*/`。
- `check_module_size.EXEMPTIONS` 清空——PF-2 拆包已落地，唯一条目移除。

**现状**：包 8 文件、最大叶 `budget.py` 667 < 1000，全部在墙内。`python -m scripts.check_listener_rehang`
输出与旧单体逐字节一致（53 全部通过）。`scripts/` 不加 `__init__.py`（3.3+ 命名空间包默认支持
`-m scripts.X`；加了反而动 `check_module_size` 扫描）。

### PF-3（#187）MemoryDossierMixin 污染块外提

**基线债务**：`water-group/memory_dossier.py` 的 `MemoryDossierMixin` 跨 14-793（780 行 / 11 方法），
紧贴 `check_module_size.MAX_CLASS_SPAN=800` 这堵墙（文件 793 行在 ADVISORY 带里），是 PF 里唯一一个
类贴近体量上限的产物。issue #34 的污染审计/清理/还原（`_pollution_rows`/`_pollution_plan`/
`audit_pollution`/`scrub_pollution`/`restore_pollution`）那整块挤在 dossier 里。

**修复**：把污染块抽到独立 mixin `MemoryPollutionMixin`，落在新文件 `water-group/memory_pollution.py`
（251 行）。`memory_dossier.py` 793→559；`MemoryDossierMixin` 跨 780→546（14-559）、11→6 方法；
`MemoryPollutionMixin` 跨 235（17-251）、5 方法。门面 `memory_engine.py` import + `__all__` + class bases
加 `MemoryPollutionMixin`（最右，镜像 store 底→...→pollution 最外的分层序）；桥接
`water_group/__init__.py` 的 `_SUBMODULES` 加 `memory_pollution`。

**运行时耦合（拆分安全性的根）**：污染块的 `self.<x>` 只有 `self.lock`/`self._connect()`（来自
`MemoryStoreMixin`，MRO 运行期解析）和块内自调——无任何对 `MemoryProfileMixin`/`MemoryObserveMixin`
的 `self.` 依赖。`MemoryPollutionMixin` 是干净的横向切片。无 caller 按 mixin 类名引用 pollution 方法，
全部走 `engine().<method>()`（composed `MemoryEngine`），MRO 位置不影响解析。`check_pollution_scrub.py`
的 snapshot 断言（byte-identity）守住——污染块方法体逐字搬运，输出与拆前逐字节一致。

**PF-3 收窄说明**：PF-3 原计划含 `MemoryProfileMixin` 切分，但 profile 类跨 597 < 800、文件 611 < ADVISORY
700，无墙压力；其方法耦合紧（dossier+observe 双向 `self.` 调 `find_profile`/`_memory_view`/
`_merge_memories`/`_upsert_group_memory`），拆开付耦合税无体量收益。故 PF-3 收窄为只拆 dossier 污染块——
唯一一个类贴近墙的。`MemoryProfileMixin` 刻意不拆（见 §6），若未来越过 ADVISORY 带再单开一刀。

---

## 4. 两份旧报告结论的逐条核对

| 旧 finding | 本仓现状 | 判定 |
|------------|----------|------|
| `-19` DRY-1 `def restrict` 7 副本 | 2（`antigravity_dry_replay.py:43` + `check_fixture_support.py`），7 份 `check_mcp_*` 副本已收 `network_guard` | 已失效（快照带入已修） |
| `-19` DRY-2 SQLite 连接 4 处 | `store`/`mcp_access`/`memory_engine`/`sync_groups` 0 处裸 `sqlite3.connect`，`sqlite_util.connect` 4 caller | 已失效（快照带入已修） |
| `-19` DRY-3 `resolve_path`/`local_path` 拆分 | `chatlocal/config.py:69`/`:77`、`store.py:30` `self.path=resolve_path(path)` | 已失效（快照带入已修） |
| `-19` DRY-4 onebot reply 段 | `chatlocal/onebot_segments.py`（27 行，根提交 `42292a4` 已在）、2 caller（`mcp_chat.py`/`mcp_chat_details.py`） | 已失效（快照带入已修） |
| `-19` DRY-5 JSON 序列化（102/242） | 现 408 `json.dumps` / 439 `json.loads`，仍不统一 | 仍成立（记录不修，理由沿用：调用点容错语义各异） |
| `-19` DRY-6 web/ 前端无共享请求层 | `web/` 目录不存在，受控 `.js`=0 | N/A |
| `-19` DRY-7 CSS 调色板硬编码 | `web/` 不存在，受控 `.css`=0 | N/A |
| `-19` SRP-1 `memory_engine` God Module 1293/19 | 门面 `water-group/memory_engine.py` 80 行、`MemoryEngine` 类跨 61-62（2 行）、拆 8 个 `memory_*` 文件（issue #11 PA，快照带入） | 已失效（快照带入已修） |
| `-19` SRP-2 `mcp_chat.wait` 223 行 | `chatlocal/mcp_chat_wait.py:21-239`（219 行），未拆 | 仍成立（记录不修，理由沿用：回复交付关键路径、`check_mcp_chat_turns` 覆盖不足，先补覆盖再拆） |
| `-19` SRP-3 `Store._migrate` 56 行内联 | `chatlocal/store.py:35` `def _migrate(self, db)`，构造器只负责路径与调用 | 已失效（快照带入已修） |
| `-19` SRP-4 `bootstrap.py` 过载 | `water-group/bootstrap.py` 905 行，ADVISORY，未拆 | 仍成立（记录不修，理由沿用：启动编排单一变更理由） |
| `-19` SRP-5 `water_group` sys.path 桥 | `water_group/__init__.py`，`check_water_group_import` 守护 | 仍成立（技术必要，保留） |
| `-20` DRY-2 platform-label 27 处 | `web/` 不存在 | N/A |
| `-20` DRY-3 mcp_schemas 七处内联 | `chatlocal/mcp_schemas.py`（33 行，根提交已在）、8 caller | 已失效（快照带入已修） |
| `-20` DRY-4 fixture_server 19 副本 | `scripts/fixture_server.py`（83 行，根提交已在）+ 本轮 PE-2 再收 13 处 daemon 测试副本 | 已失效（快照带入已修 + 本轮 PE-2 加固） |
| `-20` DRY-5 wait 超时 150 四处 | `chatlocal/mcp_chat_limits.py`（6 行，根提交已在）、`WAIT_TIMEOUT_SECONDS=150`、5 caller | 已失效（快照带入已修） |
| `-20` DRY-6 tui views flag/kv | `chatlocal/tui/` 只剩 `__pycache__`，无 `managed.py`/`views.py` | N/A |
| `-20` DRY-7 SHA-256 五处 / `file_hash.py` | `chatlocal/file_hash.py` 不存在、`snowluma_secure.py` 不存在、`desktop/` 不存在 | N/A |
| `-20` SRP-1 `_normalize_list` 透传 `memory_engine:912` | 门面已无 `_normalize_list`（随 God Module 拆分消失） | 已失效（快照带入已修） |

**判定口径**：

- **已失效** = 产物在本仓存在且债务已清（无论是快照带入还是本轮修的）。
- **仍成立** = 债务仍在，沿用旧报告的「记录不修」理由（见 §6）。
- **N/A** = 范围在本仓不存在（`web/`/`desktop/`）。
- **无法核对** = 旧报告引用的 commit/文件在本仓不存在且无法对应到任何现存产物。**本轮无此项**——每条都对上了。

两条「仍成立」+「技术必要保留」属预期：旧报告把它们判为「记录不修」时理由是「收益低于回归风险」，
本轮基线没有改变这个权衡（`mcp_chat.wait` 覆盖未补、`bootstrap.py` 仍是启动编排、`water_group` 桥接
仍是连字符目录名的唯一手段）。JSON 序列化那条调用点反而从 102/242 涨到 408/439——债务在长，但
容错语义各异的问题没变，仍不修。

---

## 5. 方法论（从 `-20` §5/§6 承接，按本仓实际改写）

- **每条结论附 `文件:行号`**：子代理易整段虚构（`-20` §5 的 audit2-ui / audit2-chat 事故——虚构的
  `public/chat/`、`theme.css`、`reply_quote.css`、`encode(...)>4096` 断言等），无证据一律视为虚构。
  本轮是一人核到底、每条都 `git show`/`git grep`/`wc -l`/AST 实测，但仍保留这条纪律作未来门槛。
- **工人完成声明以磁盘为准**：`git status`/文件存在性/门禁原输出，不信逼真的报告文本（`-20` §6 的
  impl-ui-batch1 / impl-chat-limits 假完成事故——附逼真 `git status`/grep/harness 输出，磁盘零改动）。
- **门禁安静窗口 solo 执行**：跑门禁时不开并发写，避免把「本来就红」记成新引入（`-20` §3 的 pristine
  对照法——本机 WAL `-shm` 竞态、Linux 安装器缺失等环境问题不能记成回归）。
- **判据补完先于清单补完**：PA 的教训——先把 `_bare_slice_hits`/`_tombstone_concat_hits` 判据补到
  「加进 `DAEMON_RENDER_FILES` 必红」，再加清单。否则「加进清单也不会红」是门禁说谎，正是 PA 要修的。
- **反例自检**：新增判据用 A/B 夹具验证翻转（`check_text_truncation` 契约 13a/13b、
  `check_module_size._self_test_body:162` 的 `_make_file`/`_make_class`）。把判据换成恒 `True`/恒 `False`，
  用例必须随之翻转。
- **拆分先画边界再切**：PF-2 的 43 条契约编号必须全局唯一、不许重排（`AGENTS.md` 契约让位规则），
  切片时逐条核对交叉引用；PF-3 的污染块先核运行时耦合（`self.<x>` 全清单）确认是干净横向切片再搬。

---

## 6. 仍记录不修（本仓现状，沿用旧理由 + 本轮新增）

| 项目 | 理由 |
|------|------|
| JSON 序列化统一封装（`-19` DRY-5，408/439 调用点） | 容错语义各异（静默 `[]` / `{}` / 抛错），统一改变错误行为，收益低于回归风险 |
| `mcp_chat.wait()` 拆分（`-19` SRP-2，219 行） | 回复交付关键路径，`check_mcp_chat_turns` 覆盖不足；先补覆盖再拆 |
| `bootstrap.py` 拆分（`-19` SRP-4，905 行） | 启动编排，多职责单一变更理由，拆只增间接层 |
| `water_group` sys.path 桥（`-19` SRP-5） | 连字符目录名的唯一可行手段，`check_water_group_import` 守护 |
| `MemoryProfileMixin` 拆分（PF-3 收窄） | 597 span / 611 文件均在墙内，方法双向耦合紧（dossier+observe 调 `find_profile`/`_memory_view`/`_merge_memories`/`_upsert_group_memory`），拆付耦合税无体量收益；越 ADVISORY 带再单开 |
| `scripts/` 残留 `sys.path.insert`（77 文件）/ `for x in CASES`（5 文件） | PE-1 划在范围外的特殊形态（`fixture_server` 包裹 / `(label,fn)` 元组收集 `FAILURES` / `(name,case)` 非 `@case`），逐文件核对过差异是故意的 |
| `antigravity_dry_replay.py` 的 `ThreadingHTTPServer` + `def restrict` | WS/replay 形态特殊，是 `-19` DRY-1 收口后剩下的网络护栏持有者；迁移收益低于风险 |
| 18 个 ADVISORY 带文件（700–1000 行） | 可见不致命，ADVISORY 的设计就是「visible, not fatal」；无一个越 1000 FAIL 线，`EXEMPTIONS` 为空。**计数按 `check_module_size.py` 实测**（issue #288）：上表说的是 19，`check_module_size.py` 当场只打 18 行 ADVISORY——`check_listener_rehang` / `check_dossier_cadence` / `check_gap_disclosure` 等拆包后，原单体文件已出带，行数口径也跟着变了（#313 合入后复测为 **19**，见 §6.2 表下注） |

### 6.1 三个 advisory 带回读（issue #288，四文件结论 KEEP）

#288 的事实前提是「第四轮审计里这三个文件**未读到底**，不盲改」。本轮逐个读完 end-to-end
（`reply.py` 1–999、`dossier.py` 1–973、`commands.py` 1–884 全部落目），给结论。
**三个都是 KEEP**，理由不是「行数没越墙」（那是 ADVISORY 带的设计前提，不是拆分依据），
而是「没有干净 seam」或「拆开只增间接层」。行数按 `origin/slim@f0dc7ef` 实测，
`check_module_size.py` 当场输出 `0 exemptions`。

判据统一用 #169 PF-3 的「拆开付耦合税无体量收益」（`MemoryProfileMixin` 先例，§6 上一行），
再加两条本轮新增的硬判据：

- **seam 干净度**：切出去的那块，被留下的那半是否还要反向调它、或它是否要反向调留下的半。
  双向都要调 = 不是 seam，是同一个变更理由被劈成两半（两处要一起改，那就更糟）。
- **门禁的 AST/模块级依赖**：门禁是否按**字面文件名**解析这个模块的 AST、
  是否在模块级 `import X` 后**就地改** `X.SOMETHING`。这两种依赖让「拆成包」变成门禁改动，
  而门禁链改动要另走 `## 门禁改动`  + 独立复核（`doc/PR_GUIDE.md`）。

| 文件 | 行 | 代码/注释/空行 | 结论 | 为什么 |
|---|---|---|---|---|
| `water-group/daemon/reply.py` | 999 | 542 / 328 / 129 | **KEEP** | 见下 reply 段 |
| `water-group/daemon/dossier.py` | 973 | 613 / 245 / 115 | **KEEP** | 见下 dossier 段 |
| `water-group/daemon/commands.py` | 884 | 544 / 212 / 128 | **KEEP** | 见下 commands 段 |

**`reply.py` KEEP**。#288 猜的唯一 seam 是追问+续挂块（`open_followup` 616–652 /
`wait_for_next` 655–736）。读完确认它**确实是单向的**（两块都不调 send 侧任何函数，
只 `plugins.emit` 与 `winconsole.run_passthrough`），但拆它不划算，三条理由都有证据：

1. **门禁按字面文件名解析它的 AST**：`scripts/check_listener_rehang/budget.py:243-247`
   用 `ast.parse((DAEMON / "reply.py").read_text(...))` 找名为 `wait_for_next` 的 `FunctionDef`，
   找不到就**返回 False（静默红）**。模块搬家 = 改这个路径常量；拆成包还得让
   `reply.wait_for_next` 的属性链继续成立（`:275` 直接调 `reply.wait_for_next(...)`），
   即需要一层再导出——新增的间接层只为体量，不为职责。
2. **被拆的那块只是 121 行，留下的 878 行仍在 ADVISORY 带**：`wait_for_next` 自己
   的 docstring（656–694）就占 39 行、正文注释占大头。拆完 `reply.py` 仍 ~880 行，
   仍在 700+ 带，只是把「一个 999 行的文件」换成「878 + 121 两个头」。
3. **反向依赖已经存在且是刻意的**：`water-group/daemon/wg_context.py:77` 延迟
   `import reply`（`:75-76` 注释写明「顶层反向 import 会成环」）。再多一层模块，
   环的形状不变但多一个节点，而 `reply.py` 的模块级再导出（`BUBBLE_GAP`/`MAX_BUBBLES`，
   `:103-105`）已被 `check_bubble_cap.py:141`、`check_reply_context.py:167` 直接读——
   任何拆分都要在这两处维持同一记号。

已拆出、无需再动的部分（#288 证据链里已验证）：只读分支 `_run_readonly`（789–811）
已把 `--context`/`--show`/`--list`/`--who` 全委派给 `wg_context` / `wg_text` / `wg_wakes`；
参数表 `_build_parser`（739–786）已因「让 `main()` 读得懂」（`:742` 原文）单拎；
气泡上限、发送失败诊断、图片校验分别收在 `wg_bubbles` / `wg_send_failure` / `wg_images`。
剩下的 `main()`（813–995）是两条发送路径（OneBot 直发 / 计划通道）的**同一套**前置校验
（`--max-bubbles` 越界、`--at` 全未解析、气泡拆分、`--also` 归属、图片校验），
拆开会把「发之前先把能拒的都拒掉」这条不变式劈成两半。

**`dossier.py` KEEP**。文件里有四个可辨认的关注点，读完逐个否掉：

1. cadence 块（`settings` 133 / `quiet_*` 217–313 / `due_*` 344–415 / `cmd_check` 417 /
   `cmd_tick` 458 / `cmd_dump` 514 / `cmd_digest` 534 / `cmd_mark` 581，到 604 止）——**内聚**：
   整块只读同一个 mark 文件，`load_mark`/`save_mark`（173–193）是它自己的存储层，
   `:181-186` 的 docstring 记着「read-modify-write」是 #162 修出来的不变式。切出去就是
   「一个文件 + 一次搬家」，没有耦合税，但也没有职责分离（同一个变更理由）。
2. 写入动词（`cmd_ingest` 647 / `plan_apply` 689 / `cmd_apply` 722 / `cmd_notes` 852）——
   **与污染动词共用五个 helper**：`engine()`（607）、`event_ceiling()`（619）、
   `resolve_group()`（626）、`read_payload()`（640）、`emit()`（780）。只看 `grep -n`
   的调用点密度（`engine()` 被 647/750/789/818/839/852 六处调用）就能看出：谁来切，
   这五个 helper 都得上搬家或者再导出一层，双向绑定——按上文的 seam 判据这不是 seam。
3. 污染动词（`cmd_audit_pollution` 789 / `cmd_scrub_pollution` 818 / `cmd_restore_pollution` 839）——
   #169 PF-3 已经把**引擎侧**的污染块搬到 `memory_pollution.py`，这三条是**CLI 壳**
   （60 行，含参数与报告渲染），搬走等于把一条命令的入口和它的实现分在两地。
4. `main()`（903–969）是 13 个动词的**并列一张表**，每支一行 `return cmd_*`；
   这正是「拆分先画边界再切」（§5）要求的形态，边界已经画好了。

额外一条：`check_dossier_cadence` 与 `check_dossier_ingest` / `check_pollution_scrub` /
`check_wake_dossier` 都按**字面文件名**跑子进程或解析 AST
（`scripts/check_dossier_ingest.py:21`、`scripts/check_pollution_scrub.py:28`、
`scripts/check_wake_dossier.py:394`、`scripts/check_dossier_cadence/_common.py:37`），
搬家要同步改四处（含一个包内的 `_common`），收益是 973→0 与 973→~700 的差别，
但改动面是把三个门禁推入「门禁改动」复核流程。**不做。**

**`commands.py` KEEP**。#288 猜的 seam 是 settings 块（`DEFAULTS` 59–81 + `settings_from` 276–305 +
`_plugins_from` 203–273），抽成 `wg_settings.py`。读完确认**这个 seam 不干净**：

1. **`_plugins_from` 与插件注册表是双向绑定的**：`_plugins_from`（`:231`）直接遍历
   `PLUGINS.plugins()`、`:236` 读 `plugin.legacy_section`、`:262-263` 读
   `plugin.options_defaults` / `plugin.normalize`。把 `_plugins_from` 搬走，它要反向
   `import commands`（或把 `PLUGINS` 也搬走）——`PLUGINS` 在 `:103` 由
   `plugins.load_all(...)` 在 **import 期**装配，参数里还带 `settings_provider=lambda: SETTINGS_BOX["settings"]`（`:106`）
   与 `sender=_send_via_onebot`（`:105`，`_send_via_onebot` 又调 `send_group_msg`/
   `segments_for`，两个都在宿主里）。这是**环形绑定**，不是 seam。
2. **门禁在模块级 import 后就地改这四个名字**：`commands.HANDLERS`（`check_qr_login/__init__.py:355-357`
   就地插入探针、`:373` 再整体赋回 `commands.HANDLERS = real`）、`commands.PLUGINS`
   （`check_plugin_registry.py:61`、`check_busboss.py:320`）、`commands.SETTINGS_BOX["settings"]`
   （`check_plugin_registry.py:224` 就地写）、`commands.LOG` / `commands.LOCK`
   （`check_miyoushe.py:507`、`check_sixty/__init__.py:595`、`check_qr_login/__init__.py:758`、
   `check_slash_commands.py:298`）。`:108-109` 的注释把这条写成了设计：
   「合并成模块级名字而不是在 `resolve_reply` 里现拼：测试直接改 `commands.HANDLERS` 打桩」。
   任何搬家都得保留这些属性链**在同一个模块对象上**，那就必须再导出一层。
3. **多进程/一次性 CLI 都按模块取三个不同的东西**：`up.py:313-318` 取
   `load_settings` + `PLUGINS.status_line` + `PLUGINS.emit`（`--status` 只读）、
   `wg_up_stack.py:180` 取 `clear_stale_lock`（单实例锁，`AGENTS.md` §2.5 点名它是
   锁路径的**唯一**归属）、`reply.py:610` 与 `watch.py:254/387` 延迟 import 取 settings。
   拆一刀要么让 `--status` 走两次 import，要么让锁跟着 settings 一块儿搬（那是错搬）。
4. **传输与仲裁（331–628）本身就是本文件唯一「域无关」的那半**，
   `:20-26` 的模块 docstring 明确写了「本文件只留传输与仲裁（前缀、冷却、去重、引用、
   凭证链路、发送），域知识一律在插件里（issue #4 PA–PC）」——**审计要给的那一刀已经
   在 #4 里切完了**，剩下的 884 行是宿主本身。

### 6.2 其余 advisory 带回读（issue #320，2 SPLIT / 6 KEEP）

#288 只写回了 `reply.py` / `dossier.py` / `commands.py` 三个。#320 把 `check_module_size.py`
当场输出的**其余 15 条**再排一遍，挑出体量最大、当时一个都没读到底的那 8 个，
逐个做 AST 顶层函数体量 + `文件:行号` 耦合清单。判据沿用 §6.1 的两条（seam 干净度 /
门禁的 AST·模块级依赖），前置的红门禁 `check_mcp_chat_turns` 由 **#322** 修好后
（PR #326，`7235a39`）才动基线。

| 文件 | 行（code/注释/空） | 结论 | 依据 |
|---|---|---|---|
| `scripts/check_followup/__init__.py` | 932 → **799** | **SPLIT** | 搬走 #206 多群那族，见 §6.2.1 |
| `scripts/check_mcp_chat_turns/__init__.py` | 964 → **926** | **SPLIT**（浅） | 搬走两族现场，见 §6.2.2 |
| `water-group/daemon/qr_login.py` | 891（716/35/140） | KEEP | 44 个顶层函数 + 1 个 class，最大 `start_login` / `bind_uid` 各 37 行：一张「passport 双流 + DPAPI + 限流」的平铺动作表，按动词切出去后调用点仍全在本文件 |
| `scripts/check_mcp_chat_enrichment.py` | 862（800/27/35） | KEEP | 10 函数 + 1 class，`main`（`533-859`，327 行）是唯一大块，但它是**一条线性 5 步流水线**（`wire_pages` → `wire_nested` → `nested_limits` → `wire_materials` → `concurrent_details`），每步已是独立函数；再套一层包正是 §6 上表「不能照抄结论」要防的 |
| `scripts/check_group_memory.py` | 845（615/100/130） | KEEP | 17 个函数里 15 个 `test_*`，最大 `test_native_dossier_updates_and_consolidation` / `test_memory_cap_consistency` 各 105 行；无共享大块可提，拆开只是把 `test_*` 分到两个文件 |
| `water-group/daemon/miyoushe.py` | 811（658/23/130） | KEEP | 42 个顶层函数 + 2 个 class，最大的类 `Client` 114 行、最大的函数 25 行：一个 API 客户端，每个端点一个薄函数，拆开只增一层 import |
| `scripts/check_repeater/__init__.py` | 808（671/60/77） | KEEP | 已被 **#285** 按 `_fixture` 切过一刀（`906 → 808`），剩下 14 个函数里 12 个 `test_*`（最大 `test_frequency` 117 行）+ runner，无第二 seam |
| `scripts/check_reply_send_failure/__init__.py` | 809（664/30/115） | KEEP | 41 个函数，最大的也只有 39 行（`提前解禁_禁言时刻在未来也必须复查`）：一整族「禁言文案」用例平铺，自成一个变更理由 |

> 行数是 `check_module_size.py` 与 `(行, code/注释/空)` 实测口径（本轮当场复核），
> 与 §6 上表「ADVISORY 带文件」的计数同源。#320 两个 SPLIT 落地后 `check_module_size.py`
> 打 **19** 行 ADVISORY（#288 时是 18，其后 #313 的 `check_listener_rehang/budget.py`
> 涨到 827 行又进带）——两个被拆的文件都还在 700–1000 带内，只是从带的高处往下降了一截，
> 这正是本单「**不承诺**跨到 700 线以下」的原文口径。

#### 6.2.1 `check_followup/__init__.py` SPLIT（PR #326）

搬出去的是 #206 多群那族：它**不碰本进程的追问窗口状态**，而是另建临时库 + 另起子进程
复现「一个会话只看一个群」，夹具名（`TMP` / `ROOT` / `scratch_dir` / `pin_state_env`）
一律从 `._fixture` 取。留下的 23 个 `check_*` 共用同一个 runner 与同一套进程内窗口状态，
没有第二个干净 seam——所以 `932 → 799`，不承诺进 700 带。

- seam 干净度：**单向**。新模块 `scripts/check_followup/multigroup.py`（137 行）只
  `from ._fixture import ...`，`__init__.py` 反向只 import 那一个函数；被搬走的那族不调
  runner、不读 `main` 的窗口变量。
- 验收三条全兑现：用例条数前后一致（`grep -c "  ok  "`）、拆后 `python -m scripts.check_followup`
  的完整 stdout 与基线 `diff` 为空、`gates.json` 未动（入口名本来就是 `check_followup`）。

#### 6.2.2 `check_mcp_chat_turns` SPLIT（浅拆一刀）

原单体 `scripts/check_mcp_chat_turns.py` 964 行的唯一大块是 `main`（原 `381-961`，581 行），
其余是 `wire` / `wire_waiting` 两个协程与本文件 20 余行 fixture 头。`main` 里
**没有 plan 原文猜的 `after_wait` 符号**（那是猜测，原文件里不存在），真正的结构是
`with TestClient(app) as ui:` 里一串平铺断言，共用 10 余个别名夹具
（`call` / `stored` / `add` / `plan` / `send` / `key` / `receiver_status` / `sid`）。

按「各自另开会话或另换实例、不读 `main` 游标与计划队列」切出两族：

| 新模块 | 行 | 原位置 | 为什么是干净 seam |
|---|---|---|---|
| `scripts/check_mcp_chat_turns/dialogue_window.py` | 76 | `886-935`（`#217` 对话窗口跨 wait 与进程重启） | 自己开 `111:group:223` 的会话，只收 `call` / `tools` / `access` / `events` 四个夹具；`tools.chat` 是模块级属性，换实例改的就是 `main` 看到的那份状态 |
| `scripts/check_mcp_chat_turns/stop_revoke.py` | 32 | `936-948`（stop / revoke 打断正在睡的发送尾巴） | `revoke` 那一轮另开会话（旧 `sid` 刚被停掉），`start()` 由调用点注入，同一个回调也负责把新 `sid` 写回 `main` |

`ChatTurns.__init__`（`chatlocal/mcp_chat_turns.py:107`）用 `chat.access` 推
`self.access`，所以两族收到的就是同一对夹具，**没有为拆而新增的反向依赖**。

- 迁移形态：`runner` 留在 `scripts/check_mcp_chat_turns/__init__.py`，`__main__.py` 与
  #283 / #285 / #247 先例逐字一致；`sys.path` 指到 `parent.parent`（`scripts/`）才能
  import `check_fixture_support` / `fixture_server`。
- 正文只改两处取名：`sid3=call('start_chat_session', ...)` → `sid3=ensure()`，
  `sid=call('start_chat_session', ...)` → `sid=start()`（`start` 由 `main` 里的
  `start_and_remember()` 提供，`nonlocal sid` 写回）。其余断言、`#` 注释一字未改。
- **`results.append` 仍留在 `main`**，两族只做断言——这是保证 `checks` 数组顺序
  byte-identical 的关键。
- 验收三条全兑现：22 条 `checks` 拆前拆后**逐字符串相同且顺序一致**，`status` /
  `original_persona_sha256` / `wire` / `mock_text_or_image_sends` / `mock_reactions` /
  `external_connections` 六个字段相同（唯一差异是 `validation_cost` 的计时，
  本来就是逐次浮动的）；`gates.json` 只把名字从 `check_mcp_chat_turns.py` 改成
  `check_mcp_chat_turns`（与 #283 / #285 / #247 同形），`check_gate_manifest.py --write`
  重写 AGENTS §5.1 生成段，`ci --shard mcp` 18 条全绿、`ci --shard static` 19 条全绿。

**判据没有变**：`main` 里剩下的 500 余行是同一套夹具上的平铺断言，再切只能把参数表
搬来搬去（`check_mcp_chat_enrichment.py` 的 KEEP 理由正是这条）。所以这一刀是「把最重的
两块搬到有名字的模块里，让下次改动能局部验证」，**不是**「把它压进 700 带」。

---

## 7. 结论

第三轮在本仓真实基线 `c2b0fe9`→`67ce136` 上兑现了两条 P1（门禁说谎 + 截断超上限）+ PC/PD/PE/PF 六节 P2，
每节附 `文件:行号` 与 before→after 数字：

- PA 把 `scripts/` 纳入体量门禁、补全截断判据（变量切片 + 非空尾巴），门禁不再说谎。
- PB 三份 `_clip` 合流 `wg_text.render_handler_reply`，超上限归零。
- PC 11 处只读样板合流 `sqlite_util.connect_ro`（Windows 走 `as_uri()`）+ `wg_text.connect_ro` 门面。
- PD 先修 `wg_gap` 固定临时名并发缺陷，再 14 处原子写合流 `wg_daemon.write_json`。
- PE-1/2/3 收口 `bootstrap_paths`/`run_cases`/`fixture_server`/`temp_dir`，删孤儿 `management_http`。
- PF-1/2/3 拆 `MCPChat` 四方法、`check_listener_rehang` 2220 行单体→包、`MemoryDossierMixin` 污染块。

两份旧报告逐条核对，无「无法核对」项——要么已失效（快照带入已修或本轮修），要么仍成立（沿用记录不修
理由），要么 N/A（`web/`/`desktop/` 不存在）。`-19`/`-20` 报告开头的 forward-reference（「第三轮报告
`doc/DRY_SRP_AUDIT_2026-10-27.md` 用本仓基线重写」）由本文件兑现。issue #169 七节（PA–PG）全部落地。
