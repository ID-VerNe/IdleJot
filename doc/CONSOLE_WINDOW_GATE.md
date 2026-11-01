# 控制台开窗门禁（`check_no_console_child`）的扫描边界

本文件说明 `scripts/check_no_console_child/`（入口 `_entry.py`）为什么扫哪些、不扫哪些（issue #323
源自 #39 / #319；原 vendored 扫描范围已随微信读取路径在 #350 一并撤销，见 §3）。
门禁自身的规则原文在模块 docstring 里，这里只讲边界与取舍。

## 1. 这个门禁防什么

无控制台的父进程（`pythonw`、`DETACHED_PROCESS`、`CREATE_NO_WINDOW`）拉起一个**控制台**子进程、
又不给 `CREATE_NO_WINDOW` 时，Windows 会给那个子进程新开一个控制台窗口——桌面上闪几百毫秒的黑框。
读代码看不出来，只能靠窗口钩子抓（`scripts/check_console_window.py`）。所以唯一可长期防守的办法是
**静态规则 + 唯一一处拼写标志**。

## 2. 扫描范围为什么是这三块

| 范围 | 是不是运行时 | 为什么不扩大 |
|---|---|---|
| `water-group/` / `water_group/` / `chatlocal/` / `server_wg.py` | 是 | — |
| `scripts/` 下**后端运行时会拉起**的少数 worker（`RUNTIME_WORKERS`，issue #319；#329 起由 AST 从运行时树派生，不再手工维护） | 是（只是住在 scripts/ 外面） | 整个 `scripts/` 大多数是 CI 与人工诊断，人工跑的时候弹个黑框不是 bug |
| `tools/` 下的 `VENDORED_SOURCES`（issue #323，已在 #350 撤销） | 不再扫 | 见 §3 |

判据只认"名字"：`creationflags` 里必须出现 `NO_WINDOW` / `CREATE_NO_WINDOW`，`0` 和
`CREATE_NEW_PROCESS_GROUP` 都不算。`getattr(subprocess, "CREATE_NO_WINDOW", 0)` 也算——它是
「没有 helper 可用时」的唯一正确写法。

## 3. vendored 扫描范围已撤销（#350）

#323 曾经把 `tools/wechat-reader/wechatauto/media.py` 登记进 `VENDORED_SOURCES`，并为它写了一份
安装期补丁 `patch_wechat_media`。**这两样都随微信读取路径一起删掉了**（#350）：微信通道不再是本版
的一部分，`tools/wechat-reader` 不再被安装、不再被调用，去扫一段永远不会执行的第三方字节没有意义。

随之撤销的有：三态输出（已扫-干净 / 已扫-违规 / **未扫**）、`VENDORED_SOURCES` 表、`scan_vendored()`、
补丁表 `reader_patches.VENDORED_PATCHES`、驱动 `scripts/apply_reader_patches.py`，以及门禁自测里
「清单与补丁表双向对账」那一段。「没扫必须说出来」这条教训没有失效，只是**前提没了**——没有需要扫的
第三方树，就不存在「没扫」这种状态。

## 4. 判据没有跟着范围一起退休

范围撤了，**规则本身还在**：`creationflags` 仍必须写出 `NO_WINDOW` / `CREATE_NO_WINDOW`，
`0` 与 `CREATE_NEW_PROCESS_GROUP` 仍不算。门禁自测里保留了那条正例夹具——一段形状与 #323 那次
一模一样的「无 helper 可用、就地拼 `getattr`」写法——它钉的是**判据**，不是那个文件。将来若再引入
第三方树，判据现成，只需把新范围接回扫描入口。

反过来，这个门禁现在**也不再**有任何 `path: 补丁` 的对应关系要维护：`reader_patches.py` 里剩下的
QQ 补丁不属于本门禁的扫描范围，改动它们不会、也不应该让这个门禁变红。

`RUNTIME_WORKERS` 里的 `scripts/list_client_accounts.py` 也随 #350 一起删了条目（原注释指向的
`chatlocal/client_accounts.py:39` 已不存在）。这条是**派生对账**兜住的：登记了却没人再起会报
`unstarted`，把那一行加回去今天就会红——门禁自己会拒绝一个过期的名单，不需要人手记住。

## 5. 已知未覆盖的边界

- **`tools/qq-reader` 的 spawn 不判**：`qq_db.py`、`_snapshot.py`、`_macos.py` 里的未加固 spawn
  都在 Linux / macOS 上游路径上，Windows 走不到。强行判定会给将来真正的 POSIX 移植制造假红；
  真要判，应当在那条路径真正启用时用 `# console-gate: posix-only` 之外的方式单独处理。
- **`RUNTIME_WORKERS` 由 AST 派生、双向对账**（#329）：代码里新起一个未登记的 worker、登记了却
  不再起、路径求不出来，三种都红。撤销 vendored 范围后，这里不再有「手工维护清单、漏登记就静默不扫」
  的第二张表。
- **只扫 `.py` / `.pyw`**：二进制、批处理不在此列。
