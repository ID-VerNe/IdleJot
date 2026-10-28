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
（`patch.dict(os.environ,clear=True)` 清环境模式）、`bench_napcat_qq_fixture.py:59` 与
`snapshot_cache.py:25/90`（`dir=ROOT/'.tmp'` 仓库内 bench/recovery 目录）、`check_pr_parity.py:110`
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
| 19 个 ADVISORY 带文件（700–1000 行） | 可见不致命，ADVISORY 的设计就是「visible, not fatal」；无一个越 1000 FAIL 线，`EXEMPTIONS` 为空 |

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
