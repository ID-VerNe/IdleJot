# issue #169 第三轮 DRY/SRP 清理总账 —— 执行计划

基线 `slim` @ `a7f4834`（issue 标的 `c2b0fe9` 之后又合了 #162/#164/#165/#167/#170/#172，均与 #169 七个分节无交集）。
两个 P1 复现命令在 HEAD 仍成立：

- 复现 A（体量门禁漏 `scripts/`）：`check_listener_rehang.py 2220 lines > 1000` —— 必现。
- 复现 B（截断三份实现，两份超上限 7）：`[('sixty', 7), ('miyoushe', 7), ('busboss', 0)]` —— 必现。

## 拆解与 PR 划分

issue 自陈「不可能一个 PR 收完」，且 PA→PB 强依赖（门禁先补、再修绿）。按依赖图拆成 7 个 PR，每个 PR 一个分节（PG 拆成 PG-a 标注 + PG-b 重写两步）：

| PR | 分节 | 优先级 | 依赖 | 改运行时代码 |
|----|------|--------|------|--------------|
| 1  | PA 门禁缺口 | P1 | 无 | 否（仅门禁脚本） |
| 2  | PB 截断合流 | P1 | PA | 是（14 调用点） |
| 3  | PG-a 报告基线标注 | P1 | 无 | 否（纯文档） |
| 4  | PC 只读连接 | P2 | 无 | 是（11 处） |
| 5  | PD 原子写 | P2 | 无 | 是（14 处，含行为修复） |
| 6  | PE scripts 脚手架 | P2 | 无 | 是（131 脚本，分文件推进） |
| 7  | PF 巨型模块 | P2 | PA（豁免登记） | 是 |
| 8  | PG-b 写第三轮报告 | P1 | 全部 | 否（纯文档） |

PR 一律 `Refs #169`（issue 收尾规则：不在合并时关闭）。PR 正文含 `## 门禁改动` 段（改 `scripts/check_*.py` / `gates.json` / `run_gates.py` / `ci.yml` / `.githooks/` 任一即触发 `check_gate_change_review`）。

## PR 1 · PA 门禁缺口（本 PR）

顺序不能反：先修门禁，后面的清理才有判据。

### PA-1 check_module_size.py

- `SCAN_DIRS` 加 `'scripts'`。
- 加入后立刻红 1 个文件（`check_listener_rehang.py` 2220 行）。在 `EXEMPTIONS` 登记：
  ```python
  'scripts/check_listener_rehang.py': 'issue #169 PF：按分节注释切成包（exit_hint/budget/ledger/watch），43 条契约编号全局唯一不重排',
  ```
  豁免注释纪律（既有）：「An exemption must be added back with the issue that will remove it, never 'for now'」——这条绑定到 PF 分节，不是临时。
- `self_test()` 已有 A/B 夹具（`_make_file` / `_make_class` / 豁免 / 陈旧豁免四组），`SCAN_DIRS` 加 `scripts` 不影响 self_test（它建的是 `chatlocal/` 临时树）。仍跑 `--self-test` 确认。

### PA-2 check_text_truncation.py

两个判据同时补，否则「加进清单也不会红」：

1. **变量切片判据**：`_bare_slice_hits` 现在只认 `ast.Constant` 整数切片（`text[:80]`）。补 `ast.Name` / `ast.BinOp`（`text[:limit]` / `text[:limit - 1]`）—— 切的对象名字仍是 `_BARE_NAMES`（`content`/`text`），上限是变量也抓。
2. **任意非空常量尾巴判据**：`_TOMBSTONE_ELLIPSIS` 现在只认 `[:\d+] + '…'`。补「切片 + 任意非空字符串常量」形态，抓 `text[:limit] + " …（已截断）"` 与 `text[:limit - 1] + "…"`。
3. 判据补完再把 `busboss.py` / `miyoushe.py` / `sixty.py` 加进 `DAEMON_RENDER_FILES`。**此时必须是红的**（证明判据真的能抓），再由 PR 2 修绿。
4. 两个判据都做 A/B 反例自检（照 `check_module_size._self_test_body` 那套）：判据换成恒 True / 恒 False，用例必须随之翻转。

### 回归

- `check_module_size.py`：本 PR 后跑本体输出 `FAIL scripts/check_listener_rehang.py: 2220 lines ...`（豁免登记后 OK）；`--self-test` 绿。
- `check_text_truncation.py`：本 PR 后 `test_daemon_has_no_bare_slices` 必须红（三个 handler 模块命中两条新判据）；由 PR 2 修绿。其余 10 项仍绿。
- `check_gate_change_review.py`：PR 正文含 `## 门禁改动` 段，逐个列出 `scripts/check_module_size.py` / `scripts/check_text_truncation.py`。
- `check_gate_manifest.py`：门禁清单未动，仍绿。

### 为什么 PA 不在本 PR 修绿 check_listener_rehang

`check_listener_rehang.py` 2220 行的拆分是 PF 分节（独立 PR 7），需要按 43 条契约编号逐条核对交叉引用，体量大且与门禁判据补全无关。豁免登记就是把「这个文件现在超限、PF 分节负责拆」写进门禁里，而不是让它继续静默 OK。

## 后续 PR 概要（不在本 PR 实现）

- **PR 2 PB**：`_clip` 14 调用点 → `wg_text.render_body(text, limit)[0]`；保留各模块 `REPLY_CLIP`（业务上限，非实现细节）；`sixty._bounded`（`need_ck=False`）与 `miyoushe._bounded`（`need_ck=True`）合并成带默认参数的工厂。回归 `check_text_truncation` 红→绿 + `check_sixty`/`check_miyoushe`/`check_busboss` 仍绿。
- **PR 3 PG-a**：两份旧报告开头加来源/失效边界；`AGENTS.md:264`「既有结论」改「方法论参考」。
- **PR 4 PC**：`sqlite_util.connect_ro(path, *, timeout=5.0)`；water-group 侧延迟导入门面（同 `wg_text.clip` 范式，因 `chatlocal/config.py` import 期建目录）；收 11 处手写。回归 `check_path_hygiene`。
- **PR 5 PD**：先单独 commit 修 `wg_gap.py:234` 固定临时名（行为修复，并发写覆盖缺陷）；再 14 处迁 `wg_daemon.write_json` / chatlocal 对称实现。回归 `check_gap_disclosure`/`check_qr_login`/`check_mcp_connect` 族。
- **PR 6 PE**：分文件推进。`bootstrap_paths()` 替 146 处 `sys.path.insert`；75 个手写 temp 迁 `temp_dir()`（注意 `@contextmanager` vs `mkdtemp` 裸路径，最容易做出假绿）；12 份 runner 收敛 `run_cases(CASES)`（先核对 4 份差异是否故意）；3 个自带 `ThreadingHTTPServer` 迁 `fixture_server()`；`management_http()` 孤儿二选一。
- **PR 7 PF**：`check_listener_rehang` 切包（43 契约编号不重排）；`MCPChat` 画像四方法 → `mcp_chat_dossier`；两个 Mixin 先画边界再切。完成后撤掉 PA 登记的豁免。
- **PR 8 PG-b**：用本仓 `c2b0fe9` 作基线写 `doc/DRY_SRP_AUDIT_2026-10-27.md`，旧报告结论逐条标注「仍成立/已失效/无法核对」。

## 循环

每个 PR：plan → 追评到 issue → 改代码 → 本机门禁绿 → 提 PR → 无冲突合并 → 进下一个。PR 8 合并后 #169 收尾（issue 在最后一个 PR 用 `Closes` 或单独关闭）。
