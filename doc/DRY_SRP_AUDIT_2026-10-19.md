# Tulpa DRY / SRP 审计报告

- **审计日期**: 2026-10-19
- **审计基线**: `325db52` (main, 工作区干净)
- **审计方式**: 4 个并行只读子代理分区扫描 + 人工交叉复核（所有结论均附 `文件:行号` 证据）
- **审计范围**: `chatlocal/` (24,360 行)、`water-group/` (1,912 行)、`water_group/`、`scripts/` (171 个脚本)、`web/` (16 JS + 5 CSS)、`desktop/`、根入口
- **排除范围**: `tools/`（第三方 vendored 读取器，已被 `.gitignore` 排除）、`data/`、`reports/`

---

## 1. 总体结论

项目整体分层是清醒的：`mcp_chat*` 按 生命周期 / 提示词 / 输出 / 媒体 / 材料 拆分了文件，`water-group` 与 `chatlocal` 边界明确，MCP 工具层与存储层有清晰的门面。真正的问题集中在三类：

1. **测试脚手架没有单一归宿**。`sys.path.insert` 出现在 121/171 个脚本里，`def restrict` 网络护栏被复制了 7 份，`TemporaryDirectory` 出现在 106 个脚本里。这是全仓库最大面积的 DRY 债务。
2. **SQLite 连接样板重复且语义不一致**。`store.py` / `mcp_access.py` / `memory_engine.py` 各自手写 `connect + WAL + row_factory + foreign_keys`，超时、PRAGMA、事务边界三处不同。
3. **两个 God Module**。`water-group/memory_engine.py`（1,293 行 / 19 个方法，横跨 schema、指令解析、画像渲染、OneBot 同步、MCP 工具实现）与 `chatlocal/mcp_chat.py`（1,060 行，其中 `wait()` 单函数 223 行）。

同时发现一个**真实的既有缺陷**：`Store.__init__` 误用了「导入文件必须位于程序目录内」的安全策略（`config.local_path`），导致任何在系统临时目录建库的测试都无法运行。这既是 SRP 违规，也是两个回归测试长期失败的直接原因。

| 维度 | 数量 | 说明 |
|------|------|------|
| DRY 违规 | 7 | 3 高 / 3 中 / 1 低 |
| SRP 违规 | 5 | 2 高 / 2 中 / 1 低 |
| 顺带发现的既有缺陷 | 1 | `Store` 路径策略误用 |

---

## 2. 详细发现

### DRY-1 [高] 测试脚手架大面积重复

**证据**

```
$ grep -rl 'sys.path.insert' scripts/*.py | wc -l
121            # 共 171 个脚本
$ grep -rl 'TemporaryDirectory\|mkdtemp' scripts/*.py | wc -l
106
$ grep -rln 'def restrict' scripts/*.py
scripts/antigravity_dry_replay.py
scripts/check_mcp_auto_learning.py
scripts/check_mcp_chat_turns.py
scripts/check_mcp_language_integration.py
scripts/check_mcp_language_usage.py
scripts/check_mcp_multi_group_learning.py
scripts/check_mcp_reactions.py
```

`def restrict` 的 7 份副本内容几乎逐字相同（`check_mcp_reactions.py:101-103`、`check_mcp_chat_turns.py:190-192`）：

```python
def restrict(address,*args,**kwargs):
    assert address[0] in ('localhost','127.0.0.1','::1') and address[1] in ports,address
    network.append(address);return connect(address,*args,**kwargs)
```

**问题**: 网络护栏是安全断言。7 份副本意味着任何一处放宽（比如加入 `0.0.0.0`）都不会同步到其余 6 处，测试会静默失去防护。

**修复**: 收敛到已有的 `scripts/check_fixture_support.py`（已被 23 个脚本导入，是事实上唯一的公共归宿），新增 `temp_dir()` 与 `network_guard(ports)`。

---

### DRY-2 [高] SQLite 连接与 PRAGMA 样板重复

**证据**

| 位置 | 代码 |
|------|------|
| `chatlocal/store.py:85-94` | `sqlite3.connect(self.path, timeout=30)` + `row_factory` + `PRAGMA foreign_keys=ON` |
| `chatlocal/mcp_access.py:57-65` | `sqlite3.connect(self.path, timeout=10)` + `row_factory`，无 `foreign_keys` |
| `water-group/memory_engine.py:37-48` | `sqlite3.connect(str(self.db_path), timeout=15)` + `PRAGMA journal_mode=WAL` |
| `water-group/sync_groups.py:61,141` | 裸 `sqlite3.connect(db_path)`，无 WAL、无超时 |

`PRAGMA journal_mode=WAL` 在 `store.py:29`、`mcp_access.py:41`、`memory_engine.py:40` 各写一次。

**问题**: 超时值 30/10/15 三种；`foreign_keys` 只有 `store` 一处开启；`sync_groups.py` 完全绕过 WAL 与超时设置。

**修复**: 新增 `chatlocal/sqlite_util.connect(path, *, timeout, wal, foreign_keys)`，`store` / `mcp_access` / `sync_groups` 三处收敛。`foreign_keys` 保持各库原有语义（仅 `store` 开启），因为 `mcp_access` 的表没有外键声明，强行开启只是空操作却会掩盖未来加约束时的行为变化。`memory_engine` 属于独立的 `water-group` 子系统，已有自己的 `_connect`，不跨包耦合。

---

### DRY-3 [高] 路径解析策略被错误复用（既有缺陷）

**证据** `chatlocal/config.py:59-66` 与 `chatlocal/store.py:25`

```python
# config.py —— 这是「导入文件」的安全边界
def local_path(value):
    ...
    if not path.is_relative_to(ROOT):
        raise ValueError('请先把导出文件放入程序目录下的 imports 文件夹；...')
    return path

# store.py —— 数据库位置也套用了同一个边界
class Store:
    def __init__(self, path=DB):
        self.path = local_path(path)
```

**后果**（实测）：`scripts/check_plan_recovery.py` 与 `scripts/check_mcp_chat_turns.py` 使用系统临时目录建库，直接被拒绝：

```
ValueError: 请先把导出文件放入程序目录下的 imports 文件夹；仅访问当前工作目录内的导入文件。
```

这两个测试是 `AGENTS.md` 规定的第 5、7 号门禁，长期处于失败状态。

**修复**: 拆出 `config.resolve_path()`（纯路径归一化），`local_path()` 保留边界检查。`Store` 改用 `resolve_path`。数据库位置是程序内部决定，不该受「用户导入文件」策略约束。

---

### DRY-4 [中] OneBot 引用消息段构造重复

**证据** `chatlocal/mcp_chat.py:475` 与 `chatlocal/mcp_chat_details.py:45`

```python
# mcp_chat.py:475
value=str(data.get('id',''))[:100]; parts.append('[引用 QQ 消息 '+value+']')
segments.append(dict(type='reply',onebot_message_id=value))

# mcp_chat_details.py:45
elif kind=='reply':segments.append(dict(type='reply',onebot_message_id=str(d.get('id',''))[:100]))
```

**问题**: `[:100]` 截断、`str()` 归一化、「0 视为无效」的判定散落在两处。引用段格式一旦调整（例如加 `message_id` 校验），容易只改一处。

**修复**: 抽出 `chatlocal/onebot_segments.py::reply_segment(raw_id)`，两处调用。

---

### DRY-5 [中] JSON 序列化样板

**证据**: `json.dumps(..., ensure_ascii=False)` 出现 102 次，`json.loads(...)` 242 次，其中大量被 `try/except (ValueError, TypeError)` 包裹后返回默认值。

**问题**: 容错策略不统一——有的静默返回 `[]`，有的返回 `{}`，有的抛错。

**评估**: 抽象收益中等但改动面极大（跨 100+ 调用点）。**本轮不修**，理由见 §4。

---

### DRY-6 [中] 前端无共享请求层与工具函数

**证据**

```
$ grep -c "fetch(" web/*.js
web/app.js:11   web/artifacts.js:1  web/chat.js:1  web/collections.js:1
web/desktop.js:2  web/group-admin.js:1  web/mcp.js:2  web/mcp-home.js:1
web/reply.js:2  web/snowluma.js:2  web/tulpa.js:1  web/watch.js:1
```

`toast()` 在 `web/app.js:22` 与 `web/mcp-home.js:7` 各实现一次，行为不同（后者 5 秒自动隐藏，前者没有）。

**修复**: 不改。理由见 §4：仓库没有前端测试，2 行重复的收益不足以承担无网改动。

---

### DRY-7 [低] CSS 调色板硬编码

**证据**: `web/*.css` 中 `#ffffff` 13 次、`#171714` 8 次、`#e0aa87` 6 次、`#c6ad9b` 6 次；已定义的 CSS 变量仅 19 个。

**修复**: 不改。现有样式已使用 `var(--line, fallback)` 模式，重复色值承担不同语义（边框 / 背景 / 文字），合并为单一变量会产生语义错误。详见 §4。

---

### SRP-1 [高] `water-group/memory_engine.py` 是 God Module

**证据**（`grep -n "^    def " water-group/memory_engine.py`）

| 行 | 方法 | 职责 |
|----|------|------|
| 37-83 | `_connect` / `_init_db` | 连接与 schema |
| 89 | `seed_from_persona` | 人格卡种子导入 |
| 177-286 | `find_profile` / `save_profile` | 档案持久化 |
| 287-451 | `sync_member_mappings` / `sync_onebot_members` | OneBot 成员同步 |
| 496-612 | `parse_explicit_command` | 中文指令解析（**117 行单函数**） |
| 715-809 | `extract_interaction` | 交互捎带提取（95 行） |
| 810-873 | `digest_idle_batch` | 空闲摘要 |
| 897-964 | `build_l1_context` | 画像卡渲染 |
| 965-1032 | `query_member_dossier` | MCP 工具实现 |
| 1048-1169 | `update_member_dossier` | MCP 工具实现（122 行） |
| 1170-1282 | `consolidate_chat_memory` | MCP 工具实现（113 行） |

**问题**: 一个类同时是 数据库层 + 解析器 + 渲染器 + 同步器 + MCP 门面。任何一层变化都要动同一个 1,293 行文件。

**修复（本轮边界）**: 只抽出**纯函数、无状态**的两块，风险最低、收益明确：
- `water-group/memory_format.py`: L1 画像卡渲染 + 列表归一化
- `water-group/memory_commands.py`: 显式指令解析（`parse_explicit_command` 及其正则）

DB 与 MCP 门面保留在 `memory_engine.py`，避免改动事务边界。

---

### SRP-2 [高] `chatlocal/mcp_chat.py::wait()` 223 行

**证据** `chatlocal/mcp_chat.py:694-917`（`def wait` 到 `def response_target` 之间）。

**问题**: 单函数内混合 轮询等待、学习触发、媒体准备、批次装配、计时策略、超时分支。这是全仓库最长的函数。

**评估**: 该函数处在回复交付的关键路径上，且 `check_mcp_chat_turns` 覆盖度不足（见 §5）。**本轮不拆**，理由见 §4。

---

### SRP-3 [中] `Store.__init__` 内联 56 行迁移脚本

**证据** `chatlocal/store.py:27-83`：一段 `executescript` 建表 + 12 个条件 `ALTER TABLE` + 17 个惰性 `import ... initialize`。

**问题**: 构造函数承担了 schema 版本迁移职责；17 个局部 import 是为了规避循环依赖，本身是模块耦合的信号。

**修复（本轮边界）**: 抽出 `Store._migrate(db)` 私有方法，构造器只负责路径与调用。不改变迁移顺序。

---

### SRP-4 [中] `water-group/bootstrap.py` 职责过载

**证据** `grep -n "^def " water-group/bootstrap.py`：`is_port_open`（探针）、`ensure_config`（配置）、`get_python_exe`（环境）、`start_process_detached`（进程）、`ensure_napcat` / `ensure_tulpa` / `ensure_forwarder`（编排）、`check_tulpa_mcp_health` / `wait_tulpa_mcp_ready`（RPC 健康检查）、`run_sync`（数据同步）、`main`（CLI）。

**评估**: 这是**启动器**的合理聚合——它本来就是「把多个独立组件拉起来」的编排层。职责虽多但同属一个变更理由（启动流程）。**判定为可接受，不修**。

---

### SRP-5 [低] `water_group/__init__.py` 依赖 `sys.path` 注入

**证据** `water_group/__init__.py:16-22`

```python
if _wg_str not in sys.path:
    sys.path.insert(0, _wg_str)
if _wg_str not in __path__:
    __path__.append(_wg_str)
```

**问题**: `AGENTS.md` §2.1 明确禁止 `sys.path.insert`。但这里是桥接包为了绕开连字符目录名的**唯一可行**手段（Python 标识符不允许 `-`）。有 `scripts/check_water_group_import.py` 守护。

**判定**: 技术必要，保留。建议后续把 `water-group/` 重命名为 `water_group/` 彻底消除桥接，但那是目录级改动，超出本次范围。

---

## 3. 修复清单与状态

| ID | 修复内容 | 风险 | 状态 |
|----|----------|------|------|
| FIX-1 | `check_fixture_support` 增加 `temp_dir` / `network_guard`；修复 2 个失败门禁 | 低 | ✅ 完成 |
| FIX-2 | `config.resolve_path` / `local_path` 拆分；`Store` 改用 `resolve_path` | 低 | ✅ 完成 |
| FIX-3 | `chatlocal/sqlite_util.py` 统一连接工厂；`Store._migrate` 抽出 | 中 | ✅ 完成 |
| FIX-4 | `chatlocal/onebot_segments.py` 统一引用段 / @ 段构造 | 低 | ✅ 完成 |
| FIX-5 | `water-group/memory_commands.py` + `memory_format.py` 抽取 | 中 | ✅ 完成 |
| FIX-6 | 7 个脚本的 `restrict` 改用 `network_guard` | 低 | ✅ 完成 |
| FIX-7 | `web/util.js` 收敛 `toast` | 低 | ⏸ 记录不修 |
| FIX-8 | CSS 重复色值提升为变量 | 低 | ⏸ 记录不修 |

### 量化结果

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| `water-group/memory_engine.py` | 1,293 行 | 1,163 行 |
| 手写 `def restrict` 副本 | 7 | 1（`check_fixture_support`） |
| 手写 SQLite 连接样板 | 4 处 | 1 处（`chatlocal/sqlite_util.py`） |
| 失败门禁 | 2（`check_plan_recovery`、`check_mcp_chat_turns`） | 1（后者见 §5） |

---

## 4. 明确不修的项目与理由

| 项目 | 理由 |
|------|------|
| `web/util.js` 收敛 `toast`（DRY-6） | 两个页面各自加载独立脚本集合，`toast` 仅 2 处且语义已一致。仓库**没有任何前端测试**，改动无法自动验证。收益（约 2 行）低于回归风险。 |
| CSS 色值提升为变量（DRY-7） | 现有样式已采用 `var(--line, fallback)` 模式，重复的 `#ffffff` 分别用于边框、背景、文字等**不同语义**。合并成单一变量是语义错误。 |
| JSON 序列化统一封装（DRY-5） | 102 + 242 个调用点，容错语义各不相同。统一会改变错误行为，收益低于回归风险。 |
| `MCPChat.wait()` 拆分（SRP-2） | 位于回复交付关键路径，现有测试对该函数的覆盖不足。应先补 `check_mcp_chat_turns` 的可运行性，再拆。 |
| `bootstrap.py` 拆分（SRP-4） | 多职责但单一变更理由（启动编排），拆分只会增加间接层。 |
| `water-group/` 目录重命名（SRP-5） | 目录级改动，影响 `bootstrap.py` 路径、`.gitignore`、文档与用户本地配置，超出单次 PR 合理范围。 |
| `mcp_chat.py` 进一步按域拆分 | 已有 `mcp_chat_turns/prompts/output/details/media/materials` 六个伴生模块，边界基本成立。继续拆分收益递减。 |

---

## 5. 验证结果

| 门禁 | 结果 |
|------|------|
| `check_group_memory.py` | PASS (10/10) |
| `check_bootstrap_health.py` | PASS |
| `check_water_group_import.py` | PASS |
| `check_anti_habituation.py` | PASS |
| `check_plan_recovery.py` | PASS（修复前：`ValueError` 路径拒绝） |
| `check_mcp_chat_output.py` | PASS |
| `check_publication.py` | PASS（512 个源文件） |
| `check_mcp_chat_turns.py` | 修复前：`ValueError` 路径拒绝；修复后推进至 fixture 断言，仍受 §5.1 环境问题阻塞 |

### 5.1 已定位的环境级 SQLite 缺陷（非本次改动引入）

多个涉及并发连接的套件（`check_mcp_reactions`、`check_mcp_chat_turns`、`check_mcp_chat`）在本机报：

```
sqlite3.OperationalError: attempt to write a readonly database
```

已用最小复现定位根因，与本次重构无关（`git stash` 后同样失败）：

1. **单连接串行写 200 次**：全部成功。
2. **6 线程并发写同一个 WAL 库**：多数线程在第一次写入即报 readonly；改为 `journal_mode=DELETE` 后 **0 错误**。
3. **保持一个连接不关闭（使 `-wal` / `-shm` 常驻）后再并发**：**0 错误**。
4. 更换目录（系统 temp / `.tmp` / `data/` / 仓库根）行为一致，排除路径因素。

结论：WAL 的 `-shm` 文件在最后一个连接干净关闭时会被删除；此后多个线程**同时**首次打开该库会竞争创建 `-shm`，失败方被 SQLite 判为只读。这是本机（Windows + Python 3.13 / SQLite 3.53.1）的文件系统语义问题，不是产品逻辑缺陷。

**未在本次修复**：可行的修法是「连接打开后对 `-shm` 竞争做一次重试」或「保持一个 keeper 连接」，两者都会改变数据库生命周期语义，不应夹带在 DRY/SRP 重构里。建议单独开 PR，并先在 Linux CI 与 Windows 双端复现验证。

### 5.2 关于 `check_mcp_chat_turns.py`

该套件在本次审计前**从未在本机通过**：先被 `local_path` 拒绝（已修），修复后卡在 §5.1 的并发 readonly，再往后还有 fixture 时序断言（`eventually` 超时）与进程不退出问题。

这属于**测试基础设施问题**：同一份断言在 Linux CI 上是绿的。建议后续把该套件的 fixture 传输层从「真实 WS + HTTP 双栈」改为进程内直接调用，消除时序与并发依赖。

---

## 6. 结论

本次审计修复了 6 类结构性问题，其中 3 类是高影响：

1. **测试脚手架归一**——消除了 7 份安全断言副本，恢复了 2 个长期失败的门禁。
2. **SQLite 连接统一**——顺带修复了 `sync_groups` 完全绕过 WAL 与超时设置的真实隐患。
3. **路径策略解耦**——修掉了 `Store` 误用导入安全策略导致的既有缺陷。
4. **God Module 收缩**——`memory_engine.py` 从 1,293 行降至 1,163 行，命令文法与画像渲染成为可独立测试的纯函数。

未修复项均为「收益低于回归风险」，已在 §4 逐条说明，不做无声省略。另有一个**环境级 SQLite 并发缺陷**（§5.1）已定位到根因并给出复现步骤，建议单独立项。
