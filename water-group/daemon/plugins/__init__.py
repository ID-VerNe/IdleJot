"""插件宿主包（issue #4 PA）。

用法只有两行，其余都在 ``plugins.base`` 的契约里：

    PLUGINS = plugins.load_all(log=log)
    HANDLERS = PLUGINS.commands()

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


# 名字在这里出现一次，是为了让失败也能报到具体插件上（note_error(name)）。
LOADERS = (
    ("miyoushe", _load_miyoushe),
    ("sixty", _load_sixty),
)


def load_all(log=None, capabilities=None):
    """按 ``LOADERS`` 顺序加载；单个插件失败只记一行，不影响其他插件。

    返回注册表本身。``capabilities`` 缺省是 commands 宿主的能力集，测试可以传
    一个更小的集合来验证 ``requires`` 校验。
    """
    REGISTRY.reset()
    caps = COMMANDS_CAPABILITIES if capabilities is None else frozenset(capabilities)
    host = CommandsHost(log=log)
    for name, loader in LOADERS:
        try:
            plugin = loader()
            plugin.load(host)
            REGISTRY.register(plugin, caps)
        except Exception as exc:  # noqa: BLE001  坏一个插件不拖垮其余指令
            REGISTRY.note_error(name, exc)
    return REGISTRY
