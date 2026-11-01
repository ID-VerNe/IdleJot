# 控制台开窗门禁（`check_no_console_child`）的扫描边界

本文件说明 `scripts/check_no_console_child/`（入口 `_entry.py`）为什么扫哪些、不扫哪些（issue #323
源自 #39 / #319；原 vendored 扫描范围已随微信读取路径在 #350 一并撤销，见 §3）。
门禁自身的规则原文在模块 docstring 里，这里只讲边界与取舍。

## 1. 这个门禁防什么

无控制台的父进程（`pythonw`、`DETACHED_PROCESS`、`CREATE_NO_WINDOW`）拉起一个**控制台**子进程、
又不给 `CREATE_NO_WINDOW` 时，Windows 会给那个子进程新开一个控制台窗口——桌面上闪几百毫秒的黑框。
读代码看不出来，只能靠窗口钩子抓（`scripts/check_console_window.py`）。所以唯一可长期防守的办法是
**静态规则 + 唯一一处拼写标志**。

### 1.1 抑制开窗是两件事，不是一件（issue #319 未根治的部分）

| 维度 | 谁管 | 缺了会怎样 |
|---|---|---|
| **分配** | `creationflags=CREATE_NO_WINDOW` | 无控制台的父进程拉起控制台子进程 → 子进程得到**一个新的**控制台窗口 |
| **显示** | `STARTUPINFO.dwFlags |= STARTF_USESHOWWINDOW` + `wShowWindow = SW_HIDE` | 进程**已经拥有**控制台时，显示系统仍可以把它显示出来 |

2026-11-01 实测：所有 spawn 点都带了标志，用户仍能看到一闪而过的 `powershell.exe` 窗口
（`ConsoleWindowClass`，标题是 `…\.venv\Scripts\pythonw.exe`）。标志回答的是"子进程要不要
分配自己的控制台"，桌面上那个窗口问的是另一个问题。`_with_no_window` / `_hidden_kwargs`
现在同时给两半，门禁 `check_hidden_show_state()` 直接调合并函数校验结果，而不是看源码字符串。

同一台机器上的对照（`scratch/probe_si_timeout.py` 一类）说明：**这个 sanity case 区分不出两臂**
（带不带 `SW_HIDE` 都是 0 命中），所以显示状态是针对"钩子用例覆盖不到的形状"的安全带，
不是标志的替代品。真判据只能是窗口钩子在**真实门禁链**上跑。


## 2. 扫描范围为什么是这三块

| 范围 | 是不是运行时 | 为什么不扩大 |
|---|---|---|
| `water-group/` / `water_group/` / `chatlocal/` / `server_wg.py` | 是 | — |
| `scripts/` 下**后端运行时会拉起**的少数 worker（`RUNTIME_WORKERS`，issue #319；#329 起由 AST 从运行时树派生，不再手工维护） | 是（只是住在 scripts/ 外面） | 整个 `scripts/` 大多数是 CI 与人工诊断，人工跑的时候弹个黑框不是 bug |
| `tools/` 下的 `VENDORED_SOURCES`（issue #323，已在 #350 撤销） | 不再扫 | 见 §3 |
| **门禁链本身**：`run_gates.py` 拉起的每一条门禁，以及那几条门禁自己直调的 `git` / `gh` / `powershell` / `taskkill`（issue #353） | 是（门禁链常在无控制台父进程下跑） | 不再豁免——见 §2.1 / §2.2 |

判据只认"名字"：`creationflags` 里必须出现 `NO_WINDOW` / `CREATE_NO_WINDOW`，`0` 和
`CREATE_NEW_PROCESS_GROUP` 都不算。`getattr(subprocess, "CREATE_NO_WINDOW", 0)` 也算——它是
「没有 helper 可用时」的唯一正确写法。

### 2.1 `scripts/` 的豁免被门禁链打破了（issue #353）

上面第二行的"整个 `scripts/` 不扫"曾经是对的：人工跑诊断时弹个黑框不算 bug。但它默认了
**跑门禁的人有一个控制台**。实际上门禁链经常跑在无控制台的父进程下（编辑器任务、
`pythonw` 起的后端、CI 的作业包装），这时 `run_gates.py` 拉起的 19 条门禁各自是一个
控制台程序，裸 `subprocess.run` 会**给每一条开一个窗口**——在用户桌面上就是连续闪 19 次。

2026-11-01 用 `SetWinEventHook(EVENT_OBJECT_SHOW)` 在真实门禁链上抓到现场：

```
GATE   t=1.14   [2/19] check_gate_manifest.py 通过
...（无任何 SHOW 事件：修复后）
```

修复前同一条链上有两波、每波两个窗口，标题直接写着凶手：

```
SHOW   t=3.94   pid=55008  CASCADIA_HOSTING_WINDOW_CLASS title='Terminal'
SHOW   t=4.70   pid=17648  PseudoConsoleWindow          title=''
SHOW   t=9.49   pid=55008  CASCADIA_HOSTING_WINDOW_CLASS title='C:\Program Files\Git\mingw64\bin\git.exe'
```

标题里那个 `git.exe` 对应 `check_gate_change_review.py` 里的裸 `subprocess.run(['git', ...])`。
所以范围是这样补的（**不是**把整个 `scripts/` 拉进 AST 扫描——那会让 42 个人工诊断脚本
全部变成门禁负担）：

1. `run_gates.py` 用 `winconsole.run_hidden` 拉起门禁（一处收口，19 条一起治）；
2. 门禁自己直调控制台程序的地方（`git` / `gh` / `powershell`）改用
   `winconsole.run_hidden` / `check_output_hidden`。已有：`check_publication.py`、
   `check_gate_change_review.py`、`check_repo_settings.py`、`check_pr_parity.py`、
   `check_pr_parity_isolation.py`、`upgrade_installation.py`；`diagnose_qq_accounts.py`
   因为要在**目标安装**的 Python 下运行（那里未必有 `water-group/daemon/winconsole.py`），
   本地拼了两半抑制而不 import。

### 2.2 判据收窄：只有「裸调**外部控制台程序**」才是一扇窗（issue #353）

§2.1 修完 `run_gates.py` 与那 6 条门禁之后，#319 的窗口**还在闪**。用
`scratch/audit_console_spawn.py` 把 `scripts/` 全树过一遍，裸 spawn 有 67 处 ——
但真正开窗的没那么多。`scratch/probe_tool_arms.py` 拿 `pythonw` 当调用方（**无控制台**，
即门禁链最常见的形状）做 A/B：

| 工具 | 裸调 | `run_hidden` |
|---|---|---|
| `git` | **2 次 SHOW** | 0 |
| `taskkill` | **2 次 SHOW** | 0 |
| `powershell` | **2 次 SHOW** | 0 |
| `python.exe`（`sys.executable`） | **0** | 0 |

最后一行是关键：**`sys.executable` 起的子进程不开窗**。所以「裸 spawn」这个说法太宽，
67 处里绝大多数本来无害（门禁用例拉起 `watch.py` / `listen.py` 全是这一类）。真正的判据是：

> **裸调一个外部控制台程序**（`git` / `taskkill` / `powershell` / `cmd` / `gh` / `where` …），
> 且父进程自己没有控制台 → 子进程新分配一个控制台 → 桌面上闪一扇窗。

按这条判据重新过一遍（`scratch/narrow_console_spawn.py`），67 处收窄成 **9 处 / 2 类工具**，
全部在 `scripts/check_listener_rehang/` 包里：

```
== git  x3
   scripts/check_listener_rehang/ownership.py:457   # worktree add
   scripts/check_listener_rehang/ownership.py:488   # worktree remove
   scripts/check_listener_rehang/ownership.py:490   # worktree prune
== taskkill  x6
   scripts/check_listener_rehang/budget.py:824
   scripts/check_listener_rehang/ledger.py:148
   scripts/check_listener_rehang/ownership.py:141 / 341 / 432 / 486
```

`taskkill` 那一族更隐蔽：它是**收尾**动作，五个用例各自写一遍
`subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)`，
只在失败路径上跑，跑得快、窗口一闪而过，日志里什么都看不到。

修法是给这个包留两个唯一入口（`_common.py`），而不是就地改九处：

```python
def kill_tree(pid, timeout=60):      # taskkill /T /F，不开窗
def git_hidden(args, **kwargs):      # git 子命令，不开窗
```

（行号是**改之前**的行号 —— 改完这批调用点自己也会移位，这是它只能当定位线索、
不能当判据的原因。）

修后同一命令输出 **0 处 / 0 类工具**，`python scratch/probe_listener_window.py`（窗口钩子 +
0.2s 进程表采样）拿到 `gate rc=0 elapsed=88.8s` 与 `SHOW events: 0`。

为什么一定要看 **chain**、不能只看窗口标题：`probe_real.txt` 里三条事件中两条 `pid=55008`
是 **Windows Terminal 宿主**（`CASCADIA_HOSTING_WINDOW_CLASS`，不是新控制台，是终端在换标签页
标题），只有 `ConsoleWindowClass` 那条才是用户截到的黑框。事后
`QueryFullProcessImageNameW` 对**已退出**的 pid 一律返回 `<no handle>`，所以祖先链必须在
0.2s 采样线程里当场把 `szExeFile` 存下来 —— 否则每条链看起来都只有一层。

反面教材记在这里：`run_gates.py` 的 `import winconsole` **不能硬依赖**。
`check_run_gates_parallel.py` 会搭一棵只含 `scripts/` 的假树来跑它，硬 import 让那个门禁
当场 `ModuleNotFoundError`（实测红了 12 条断言）。跑门禁是本职、抑制是加固，拿不到 helper
就退回裸调用——但**真树里必须真的走 helper**，否则这个加固是装饰。这两条各有一个探针钉着。

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
- **门禁链的加固是「调用点改了就改了」，没有 AST 判据兜着**（issue #353 留下的边界）。
  §2.1 / §2.2 那批 `scripts/` 下的调用点是**人工**改成走 helper 的，而 AST 门禁的
  `SCAN_DIRS` 仍不含 `scripts/`——所以新写一条门禁、里面裸调 `git` 或 `taskkill` 时，
  **没有任何门禁会红**。这与 §2 第二行"整个 `scripts/` 不扫"是同一个取舍（42 个人工诊断
  脚本不该逐个上秤），代价是：**新增门禁请自查有没有直调控制台程序**。真要机器管，得先
  想清楚"人工诊断脚本"和"门禁链成员"怎么在 AST 层分开——`gates.json` 里那份名单是现成的
  依据，但那要另开一个 issue，不在 #353 的范围里。自查可以借 §2.2 那两个 scratch 探针
  （`audit_console_spawn.py` 看全量、`narrow_console_spawn.py` 只看会开窗的那 9 类），
  它们的输出是「N 处 / K 类工具」——这个数字回到 0 才算干净。
- **sanity case 分不出「只给 flag」与「flag + `SW_HIDE`」**（§1.1 已述，这里是明确承认）。
  `scripts/check_console_window.py --self-test` 的四组（`visible` / `hidden` /
  `detached-cmd` / `nowindow-grandchild`）在这个维度上全 0 命中；试过用「有控制台的父进程」
  造一个能分辨的形状（`scratch/probe_showstate_ab.py`，四臂），同样全 0 ——
  **该形状不成立**。所以显示状态目前只有两道保险：`check_hidden_show_state()` 直接校验
  合并函数产出的 `STARTUPINFO` 字段（源码级，不看行为），以及 §2.2 那个真实门禁链上的
  窗口钩子。别把自检的"全绿"当成"显示状态已被行为验证"。
- **`run_gates.py` 的抑制依赖运行时树在场**。假树夹具（只有 `scripts/`）里它退回裸调用。
  这不是漏，是分工：夹具树里没有窗口问题的前提，硬依赖反而会让夹具门禁全红。代价是真树
  被裁到不含 `water-group/` 时抑制静默失效——那种安装本来也跑不起门禁。
