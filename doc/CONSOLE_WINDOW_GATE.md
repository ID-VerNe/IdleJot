# 控制台开窗门禁（`check_no_console_child.py`）的扫描边界

本文件说明 `scripts/check_no_console_child.py` 为什么扫哪些、不扫哪些，尤其解释
**第三方 vendored 源码（`tools/`）** 这件事（issue #323，源自 #39 / #319）。
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
| `scripts/` 下**后端运行时会拉起**的少数 worker（`RUNTIME_WORKERS`，issue #319） | 是（只是住在 scripts/ 外面） | 整个 `scripts/` 大多数是 CI 与人工诊断，人工跑的时候弹个黑框不是 bug |
| `tools/` 下的 `VENDORED_SOURCES`（issue #323） | 是（安装后由后端调用） | 见 §3 |

判据只认"名字"：`creationflags` 里必须出现 `NO_WINDOW` / `CREATE_NO_WINDOW`，`0` 和
`CREATE_NEW_PROCESS_GROUP` 都不算。`getattr(subprocess, "CREATE_NO_WINDOW", 0)` 也算——
vendored 代码不能 import 本仓的 helper，这就是它唯一的正确写法（`db.py` 早就这么写）。

## 3. 为什么 `tools/` 不能进 CI 判红范围

`tools/` 整树受 `.gitignore` 保护，`git ls-files tools` 是 0。

1. **CI 上它根本不存在**。CI 的 checkout 里没有 `tools/`，`check_pr_parity.py` 的干净
   worktree 里也没有。任何"文件不存在就红"的写法都会让门禁在 CI 上永远红。
2. **它是第三方快照，不是我们维护的代码**。补丁在安装期用 `scripts/reader_patches.py`
   打到本地副本上，仓库里只有补丁，没有打完补丁的文件。

所以门禁采取**三态**，而不是二态（干净 / 红）：

| 状态 | 什么时候 | 输出 | 退出码 |
|---|---|---|---|
| 已扫-干净 | 文件在，且每个 spawn 都带抑制标志 | `✓ vendored 树已扫（同一套判据）：<路径>` | 0 |
| 已扫-违规 | 文件在，有裸 spawn | 逐条列出 `路径:行号  代码行` | 1 |
| **未扫** | 文件不在（CI、干净 worktree、没装 reader） | `· vendored 树未扫：<路径>（<打它的补丁>）—— 本机没有这份第三方源码……不是「已检查干净」` | 0 |

第三行是这个 issue 的核心：**"没扫"必须说出来，不能表现得像"扫过且干净"**。
#323 的病根就是门禁对 `tools/` 从来没有意见，而"没有意见"在输出里和"干净"长得一模一样。

## 4. vendored 代码为什么就地拼 `getattr`，而不走 helper

`water-group/daemon/winconsole.py` 与 `chatlocal/platform_process.py` 是本仓的模块。第三方树
（`tools/wechat-reader/wechatauto/`）是上游代码的副本，`import chatlocal...` 在那里既装不上、
也会让补丁与上游差异变得不可审计。因此**规则 3（`CREATE_NO_WINDOW` 只在两个 helper 里拼写）
只作用于运行时树**，vendored 文件豁免；代价是补丁必须写死这一行形状，并由门禁的 vendored 扫描
（§3 第二行）保证它确实在。

对应补丁：`scripts/reader_patches.py: patch_wechat_media`，只改
`tools/wechat-reader/wechatauto/media.py` 里 `_wxgf_to_jpg` 的 ffmpeg 调用，加一行
`creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),`，并在文件头加一行
`# Tulpa console-window flag v1 (scripts/reader_patches.py).` 作幂等哨兵
（重复安装不会重复插入；锚点漂移则抛 `ValueError`，不猜）。

## 5. 已知未覆盖的边界

- **`tools/qq-reader` 的 spawn 不判**：`qq_db.py`、`_snapshot.py`、`_macos.py` 里的未加固 spawn
  都在 Linux / macOS 上游路径上，Windows 走不到。强行判定会给将来真正的 POSIX 移植制造假红；
  真要判，应当在那条路径真正启用时用 `# console-gate: posix-only` 之外的方式单独处理。
- **`VENDORED_SOURCES` 是显式清单，不扫目录**：与 `RUNTIME_WORKERS` 同理。新增 vendored 文件
  需要手工登记一行；漏登记的表现是"未扫"里**没有**它（等于又回到 #323 之前的状态），因此
  登记新第三方树时请同时确认它在不在清单里。
- **只扫 `.py` / `.pyw`**：vendored 的二进制、批处理不在此列。
