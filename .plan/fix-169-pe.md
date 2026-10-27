# issue #169 PE — scripts/ 脚手架清理

基线 `slim` @ `862c498`（PD 合入后）。本仓实测 PE scope（不是 issue 文里的 146/67/75/12 估算，是重新数过且更小的真值）：

| 子项 | issue 文 | 实测 | 备注 |
|---|---|---|---|
| `sys.path.insert` 出现 | 146 | **154 次 / 85 文件** | 13 种形态，`str(Path(__file__).resolve().parent)` 43 / `str(ROOT)` 38 / `str(ROOT/"water-group"/"daemon")` 30 居前 |
| `ROOT = Path(__file__)` | 67 | 58 文件同时有 insert+ROOT def，27 文件只有 insert | `parents[1]` 48 / `parent.parent` 18 |
| 手写 tempdir | 75 | 74 文件 | `check_fixture_support.temp_dir()` 是 `@contextmanager`，70 处是裸 `mkdtemp()` 路径——迁移是改成 `with temp_dir() as tmp:`，不是换函数名 |
| runner 变体 | 12（6 种） | **15 文件**用 CASES 循环 | 归 3 族 + 2 个异类 |
| 自带 `ThreadingHTTPServer` | 3 | **13 文件**未走 `fixture_server()` | 各 2 处 server ref |
| 孤儿 `management_http()` | 1 | 0 调用点确认 | 唯一命中是定义自身 |

issue 文把「3 个自带服务器」写成全部，实测是 **13 个**——`fixture_server()` 已被 12 个文件采用，但 `_mcp_*` / `_image_*` / `_language_*` / `reactions` / `sticker_library` 这 13 个还在手写。

## 顺序（issue 要求「先有唯一归宿，再 codemod」，分文件不整体重写）

1. **`bootstrap_paths()` 入 check_fixture_support**——一次注入 `ROOT` / `DAEMON` / `scripts` 三条 path，返回 `ROOT`。替换 154 处 `sys.path.insert`。
   - 注意 28 处注入的是 `water-group/daemon`，与 `scripts` 自身不同层——`bootstrap_paths()` 要三条都注。
   - 7 处 `str(Path(__file__).parent)`（无 `.resolve()`）与 43 处带 `.resolve()` 形态等价但写法不一，统一。
2. **`run_cases(CASES)` 入 check_fixture_support**——15 文件的 CASES 循环收一份。先核对差异是否故意（见下「runner 族分析」）。
3. **75 处手写 tempdir → `temp_dir()`**——改成 `with` 块，不换函数名假绿。
4. **13 处自带 `ThreadingHTTPServer` → `fixture_server()`**——`check_mcp_chat_turns` / `check_mcp_chat_enrichment` 本机已有偶发红（`readonly database` / 夹具超时），迁移与既有红签名对照。
5. **`management_http()` 二选一**——0 调用点，删掉。

## runner 族分析（差异是否故意）

| 族 | 成员 | main 形态 | 差异 | 故意? |
|---|---|---|---|---|
| A（`✓`/`✗` + 中文「全部通过」） | gap_disclosure / listener_rehang / now_clock / pending_reply / bubble_cap / multigroup_isolation | `try/except` 包 fn()，`got=False` 兜底，`✓ fn.__name__` / `✗ fn.__name__: exc` | now_clock 多 `build_db()` 前置；multigroup 对 `got is True` 与 `got is str` 分档 | 前置/分档是业务需要，循环骨架同 |
| B（`PASS`/`FAIL` + 英文 OK） | alias_visibility / image_visibility / quote_visibility / wake_dossier | 同骨架，输出词不同 | 仅措辞 | 漂移 |
| C（无 try/except，裸跑） | reply_context | `got=fn()` 不 catch，`✓`/`✗` + fn.__name__ | 不 catch——fn 自己保证不抛 | 设计选择（reply_context 的 fn 全是断言式） |
| D（fixture_server 包裹） | reply_image / reply_send_failure | CASES 循环在 `with fixture_server()` 内 | 需要活服务器 URL 才能跑 | 业务需要 |
| 异类 | stop_stack / wake_ledger | `(name, case)` 元组列表，不是 `@case` 装饰器 + CASES list | 结构不同 | 不强行合 |

**结论**：A+B+C 的 11 个文件循环骨架可合到 `run_cases(CASES)`，`run_cases` 支持 `prefix=`（`✓`/`✗` vs `PASS`/`FAIL`）、`catch=`（是否 try/except）、`setup=`（前置 build）。D 的 fixture 包裹留在各文件 main 里（只合循环），`stop_stack`/`wake_ledger` 不动。

## PR 切分

PE 一次性收 154+15+74+13 处改 90+ 文件，太大。**拆 3 个 PR**：
- **PE-1**：`bootstrap_paths()` + `run_cases()` + `temp_dir()` 推广 + `management_http()` 删除（只动 check_fixture_support 与迁移点，不改业务断言）。
- **PE-2**：13 处 `ThreadingHTTPServer` → `fixture_server()`（_mcp 分片，单独 PR 便于与既有红签名对照）。
- **PE-3**（视 PE-1 体量）：若 PE-1 仍 >40 文件，再拆 path 注入与 tempdir 迁移。

先做 PE-1。

## 回归

- `check_gate_manifest.py`：`bootstrap_paths()` 是新导出名，进 `check_fixture_support` 的公开面，manifest 不动（manifest 钉的是 `scripts/check_*.py` 文件级，不钉函数）。
- `check_module_size.py`：`check_listener_rehang.py` 仍走 PA 的 EXEMPTIONS（PF 才拆），PE 不碰。
- 全 5 分片 CI。
