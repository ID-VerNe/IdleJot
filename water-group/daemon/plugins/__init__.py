"""插件宿主包（issue #4 PA）。

用法只有两行，其余都在 ``plugins.base`` 的契约里：

    PLUGINS = plugins.load_all(log=log)
    HANDLERS = PLUGINS.commands()

事件型能力（追问窗口一类，没有 ``#`` 指令）另有一条路：一次性 CLI 用
``plugins.emit(event, ctx)`` 派发命名事件，commands 进程照旧走 ``load_all``
（issue #4 PD2a）。两条路同一个 loader、同一个 PLUGIN 对象。

``LOADERS`` 是**静态 import 的显式清单**：``check_hot_reload`` 要求每个 ``.py``
都能从 5 个入口的 AST 闭包到达，运行时扫目录既过不了门禁，也让 ``reload`` 无法
把插件改动映射到进程。新增插件 = 在 ``LOADERS`` 里加一行 + 在 ``plugins/`` 加一个
文件，不改 ``commands.py``。
"""
from __future__ import annotations

from plugins.base import (  # noqa: F401  (对外 re-export)
    API_VERSION,
    CAP_COMMANDS,
    CAP_CREDENTIAL,
    CAP_SEND,
    CAP_STORE,
    CAP_TOOLS,
    CAP_WAKE,
    Command,
    CredentialProvider,
    Hooks,
    Host,
    Plugin,
    PluginError,
)
from plugins.host import CAPABILITIES as COMMANDS_CAPABILITIES
from plugins.host import BIND_HINT, CommandsHost  # noqa: F401
from plugins.registry import REGISTRY, register, status_line  # noqa: F401


def _load_miyoushe() -> Plugin:
    import plugins.miyoushe_plugin as module
    return module.PLUGIN


def _load_sixty() -> Plugin:
    import plugins.sixty_plugin as module
    return module.PLUGIN


def _load_followup() -> Plugin:
    import plugins.followup_plugin as module
    return module.PLUGIN


# 名字在这里出现一次，是为了让失败也能报到具体插件上（note_error(name)）。
LOADERS = (
    ("miyoushe", _load_miyoushe),
    ("sixty", _load_sixty),
    ("followup", _load_followup),
)

# 事件型插件（issue #4 PD2a）：没有 ``#`` 指令，宿主在通用流程点派发命名事件。
# 与 LOADERS 共用同一个 loader、同一个模块级 PLUGIN——commands 进程注册表里的
# 和 CLI 派发到的是同一个对象，不存在两份状态。
EVENT_LOADERS = (("followup", _load_followup),)


def load_all(log=None, capabilities=None, settings_provider=None, sender=None):
    """按 ``LOADERS`` 顺序加载；单个插件失败只记一行，不影响其他插件。

    返回注册表本身。``capabilities`` 缺省是 commands 宿主的能力集，测试可以传
    一个更小的集合来验证 ``requires`` 校验。``settings_provider`` / ``sender``
    由宿主延迟绑定（``load_all`` 在 import 期跑，发送函数通常还没定义）。
    """
    REGISTRY.reset()
    caps = COMMANDS_CAPABILITIES if capabilities is None else frozenset(capabilities)
    host = CommandsHost(log=log, settings_provider=settings_provider, sender=sender)
    REGISTRY.host = host
    for name, loader in LOADERS:
        try:
            plugin = loader()
            plugin.load(host)
            REGISTRY.register(plugin, caps)
        except Exception as exc:  # noqa: BLE001  坏一个插件不拖垮其余指令
            REGISTRY.note_error(name, exc)
    return REGISTRY


def emit(event: str, ctx: dict | None = None):
    """一次性 CLI 的事件派发口（issue #4 PD2a）。

    reply / watch / up 不是 commands 进程：没有宿主、没有指令表，也不该为了
    一条追问窗口把米游社取数层拖进进程。这里只过 ``EVENT_LOADERS``，不建
    ``CommandsHost``、不碰发送通道；拿到的是与 ``LOADERS`` 同一个 loader、
    同一个模块级 PLUGIN——commands 注册表里注册的也是它。

    单个事件插件加载失败时静默跳过（返回 None）：CLI 路径不记 load_error，
    插件坏了的可见性由 commands 进程的 ``--status`` 负责，那是它的本职。
    """
    ctx = ctx or {}
    for _name, loader in EVENT_LOADERS:
        try:
            plugin = loader()
        except Exception:  # noqa: BLE001  CLI 降级：没有事件型能力照旧跑
            continue
        fn = plugin.hooks.on_event
        if fn is None:
            continue
        result = fn(event, ctx)
        if result is not None:
            return result
    return None
