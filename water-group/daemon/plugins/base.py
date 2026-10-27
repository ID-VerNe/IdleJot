"""插件契约 v1：可选能力的统一形状（issue #4 PA）。

三条边界写死在契约里，改动前先读：

* **插件 = 可选行为；平台 = 必需通路。** 进程生命周期（``up.py`` / ``supervisor`` /
  ``watchdog``）、OneBot 连接（``collector``）、MCP 装配不在这里。想把 ``up.py``
  做成插件，是把启动链拆了。
* **文件不搬迁。** 适配器放 ``daemon/plugins/``，取数与传输模块留在原位；「插件化」
  改的是注册方式，不是目录结构。
* **静态 import 的显式注册表。** ``scripts/check_hot_reload.py`` 要求 watch 目录下
  每个 ``.py`` 都能从 5 个入口的 AST 闭包到达；运行时扫目录发现的插件既会让门禁红，
  也让 ``reload`` 无法把改动映射到进程。所以本包内部**一律绝对 import**
  （``import plugins.base``）——相对 import 在非 ``chatlocal`` 包里会把路径算错
  （``reload._resolve_import`` 用文件系统 parts 拼模块名），而且是静默算错。

协议有两个扩展点，别混用：``commands`` 是**指令型**能力（``#`` 开头的 handler），
``Hooks.on_event`` 是**事件型**能力（追问窗口一类——宿主在通用流程点派发命名事件，
issue #4 PD2a）。指令型走 ``Registry.commands()`` 合成表；事件型没有表，宿主直接
``emit(event, ctx)``。

宿主（``Host``）不是插件，是本进程能给插件的能力集合；``Plugin.requires`` 必须被
宿主满足，否则注册期就失败并留下原因（fail-fast + 可见），不许像旧的
``try/except`` 那样静默置空。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

API_VERSION = 1

# 宿主能力名。插件声明需要的，宿主持有的取交集校验。
CAP_COMMANDS = "commands"      # 能注册 # 指令（commands 宿主）
CAP_SEND = "send"              # 能主动发群消息（扫码结果回推一类）
CAP_STORE = "store"            # 有独立的持久化命名空间
CAP_WAKE = "wake"              # 能触发 Agent 唤醒（app / collector 宿主）
CAP_TOOLS = "tools"            # 能注册 MCP 工具（app 宿主）
CAP_CREDENTIAL = "credential"  # 有凭证解析器（米游社一类）

Settings = Mapping[str, Any]
Ctx = dict[str, Any]
Reply = Any


def positive(value: Any, fallback: float) -> float:
    """非负数解析：坏值/负数回落 fallback（配置里手写的 0 是有效值）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= 0 else fallback


class PluginError(RuntimeError):
    """注册期错误：重名、api 不兼容、能力缺失。一律 fail-fast，不静默覆盖。"""


@dataclass(frozen=True)
class Command:
    """一条 ``#`` 指令。

    ``gate`` 是**按指令**的门控（settings, group_id -> bool）。60s 资讯的
    ``allow`` 白名单就是按指令的，插件级 ``enabled`` 表达不了它；``#帮助`` 与
    ``resolve_reply`` 共用同一个 gate，才不会出现「列了却不回」。
    """

    name: str
    desc: str
    handler: Callable[[str, Ctx], Reply]
    needs_credential: bool = True
    gate: Callable[[Settings, str], bool] | None = None

    def available(self, settings: Settings, group_id: str = "") -> bool:
        return self.gate is None or bool(self.gate(settings, group_id))


@dataclass(frozen=True)
class CredentialProvider:
    """凭证来源：谁能用全局 CK、全局 CK 是什么。

    读取群友自己的绑定是宿主的事（``user_store`` 与平台无关）；这里只回答
    「管理员白名单」和「兜底的全局凭证」两个插件域问题。
    """

    admins: Callable[[Settings], set[str]]
    global_credentials: Callable[[Settings], tuple[list[str], str]]


@dataclass(frozen=True)
class Hooks:
    """插件往宿主通用流程里插的口子。缺省全是 None，宿主跳过。

    名字刻意用通用词（precheck / cooldown / note_error），不放 ``risk`` /
    ``ck`` 这类米游社词——协议一旦带域语义，第三个插件就得照着米游社写。
    """

    credentials: Callable[[Settings], CredentialProvider | None] | None = None
    # 执行 handler 前：填自己的 ctx 字段，或返回非空文案直接回给用户
    precheck: Callable[[Ctx, Settings], str | None] | None = None
    # 冷却期内拦截（``commands._risk_blocked`` 的落点）
    cooldown: Callable[[Ctx], str] | None = None
    # handler 抛错后的副作用 + 可选主动通知文案
    note_error: Callable[[Ctx, Settings, BaseException], str | None] | None = None
    # 取数前的顺带动作（续期一类），不得阻塞
    on_resolved: Callable[[str, Mapping], None] | None = None
    # 多步确认未完成的指令名：这些指令豁免冷却，否则第二步被 5s 闸门静默丢弃
    # （issue #66 的 #解绑 就是这么踩的）
    pending: Callable[[str], frozenset[str]] | None = None
    # 回复没发出去时的回滚（撤销会话 / 退还额度），ctx 带 key/user_id/group_id
    on_send_failed: Callable[[Ctx, Settings], None] | None = None
    # --status 里这一行插件的附加说明
    status: Callable[[Settings], str] | None = None
    # 事件型能力入口（issue #4 PD2a）：宿主在通用流程点派发命名事件，
    # 返回 None = 不接，宿主继续走自己的默认分支。事件是否受三态门控由插件
    # 自决（窗口类门控、DB 兜底类不限）——语义属于插件，不属于协议。
    on_event: Callable[[str, Ctx], Any] | None = None


@dataclass(frozen=True)
class Plugin:
    name: str
    version: str = "1.0"
    api: int = API_VERSION
    commands: Sequence[Command] = ()
    requires: frozenset[str] = frozenset()
    error_types: tuple[type[BaseException], ...] = ()
    hooks: Hooks = field(default_factory=Hooks)
    # 缺省是否启用。60s 资讯设为 False（旧行为：合入但没开配置就静默落空）。
    default_enabled: bool = True
    # 插件私有配置的默认值；宿主不再知道任何 provider 字段（issue #4 PB）。
    options_defaults: Mapping[str, Any] = field(default_factory=dict)
    # 旧版顶层配置段名（issue #4 PD2a）：宿主把 ``cfg[legacy_section]`` 当旧键
    # 平铺读——``enabled`` 进三态、其余键进 options。只有真的有过一个顶层段的
    # 插件才声明（followup 的 ``"followup"``）；新插件留空。
    legacy_section: str = ""
    # 这个插件靠 ``commands`` 进程的 WS 事件流吃饭（issue #155）：那个进程的
    # 总闸是 ``slash_commands.enabled``，关掉就整个不启动，插件会跟着静默死掉
    # 且一个字都不说。声明这一位的插件能在指令关掉时单独把事件流拉起来。
    # 只给「必须看全量群事件」的插件用（目前只有 repeater）；追问窗口由
    # reply/watch/up 三个一次性 CLI 派发，不依赖这个进程，所以不声明。
    needs_event_loop: bool = False

    def enabled(self, settings: Settings, group_id: str = "") -> bool:
        """三态门控：``enabled`` 总开关 → ``groups`` 白名单 → 群级 ``group_off``。

        配置由 ``settings_from`` 归一（见 ``commands._plugins_from``），插件不解析
        原始 JSON；群级覆盖写成 ``groups.<id>.plugins.<name>: false``。

        ``group_id`` 为空表示「不问具体群」（``--status`` 那类全局查询）：只判总
        开关，不套群级覆盖——否则配了 ``groups`` 白名单的插件会在状态里显示停用。
        """
        cfg = (settings.get("plugins") or {}).get(self.name) or {}
        if not bool(cfg.get("enabled", self.default_enabled)):
            return False
        if not group_id:
            return True
        scoped = cfg.get("groups") or ()
        if scoped and str(group_id) not in scoped:
            return False
        return str(group_id) not in (cfg.get("group_off") or ())

    def options(self, settings: Settings) -> Mapping[str, Any]:
        """本插件的归一化配置（``settings_from`` 已套过 ``normalize``）。"""
        entry = (settings.get("plugins") or {}).get(self.name) or {}
        return entry.get("options") or {}

    def normalize(self, options: Mapping[str, Any]) -> dict[str, Any]:
        """把原始 ``options`` 归一成运行期形状。默认原样复制。"""
        return dict(options)

    def load(self, host: "Host") -> None:
        """宿主装配完成后的初始化。不得在这里做网络请求。"""

    def start(self, host: "Host") -> None:
        """后台线程 / 轮询（qr_login 的 poller 是典型）。PA 没有插件用到。"""

    def stop(self) -> None:
        """与 start 对称。"""


class Host(Protocol):
    """宿主能力面。每个进程实现一份，插件只依赖这里，不依赖具体进程。

    ``settings()`` / ``send()`` / ``notify()`` 是 PC 补的（issue #4）：前两轮只有
    ``log``，插件拿不到最新配置、也不能主动发消息。三个方法都只做**传输与寻址**，
    不带域语义——``send`` 的 payload 就是 handler 的返回值（``str`` 或
    ``{"text","image"}``），怎么变成 OneBot 段是宿主的事。
    """

    name: str
    capabilities: frozenset[str]

    def log(self, message: str) -> None: ...

    def settings(self) -> Settings:
        """当前设置（热重载后的最新值）。"""

    def send(self, group_id: str, payload: Reply) -> None:
        """主动发到群。``payload`` 与 handler 返回值同形状。"""

    def notify(self, group_id: str, qq: str, text: str) -> None:
        """@ 某人 + 文本，发到群里（扫码结果回推用）。"""

    def store(self, plugin: str, name: str) -> Any: ...


def check_capabilities(plugin: Plugin, capabilities: frozenset[str]) -> None:
    missing = plugin.requires - set(capabilities)
    if missing:
        raise PluginError(
            f"{plugin.name} 需要宿主不具备的能力: {', '.join(sorted(missing))}")
