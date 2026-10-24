"""显式注册表：谁注册了什么，重名立刻炸（issue #4 PA）。

不做目录扫描、不做 import 副作用收集——理由见 ``plugins.base`` 的模块文档。
注册表的三个职责：

1. **合成 handler 表**（``commands()``），供 ``commands.HANDLERS`` 在 import 期取一次；
2. **回答归属**（``owner`` / ``available``），让 ``#帮助`` 与 ``resolve_reply``
   共用同一套门控，不再各自特判；
3. **留痕**（``load_errors`` / ``status_line``），插件坏掉在 ``up.py --status``
   里有一行，而不是群里表现为「这个指令没了」。
"""
from __future__ import annotations

from typing import Callable, Iterable

from plugins.base import (
    API_VERSION,
    Command,
    CredentialProvider,
    Plugin,
    PluginError,
    Settings,
    check_capabilities,
)


class Registry:
    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}
        self._owners: dict[str, str] = {}
        self._commands: dict[str, Command] = {}
        self.load_errors: dict[str, str] = {}
        # 由 load_all 填：start_all/stop_all 用它把宿主交给插件（issue #4 PC）。
        self.host = None

    # ---- 写侧 ---------------------------------------------------------------
    def reset(self) -> None:
        self._plugins.clear()
        self._owners.clear()
        self._commands.clear()
        self.load_errors.clear()
        self.host = None

    def register(self, plugin: Plugin, capabilities: Iterable[str] | None = None) -> Plugin:
        """注册一个插件。任何冲突都在写入前抛错，不留半注册状态。"""
        if plugin.api != API_VERSION:
            raise PluginError(
                f"{plugin.name} 的 api={plugin.api}，宿主只认 {API_VERSION}")
        if plugin.name in self._plugins:
            raise PluginError(f"插件重名: {plugin.name}")
        if capabilities is not None:
            check_capabilities(plugin, frozenset(capabilities))
        for command in plugin.commands:
            owner = self._owners.get(command.name)
            if owner is not None:
                raise PluginError(
                    f"指令重名: #{command.name}（{owner} 与 {plugin.name}）")
        self._plugins[plugin.name] = plugin
        for command in plugin.commands:
            self._owners[command.name] = plugin.name
            self._commands[command.name] = command
        return plugin

    def note_error(self, name: str, exc: BaseException) -> None:
        """记一条加载失败。名字保留下来，``--status`` 才知道该期待谁。"""
        self.load_errors[name] = f"{type(exc).__name__}: {exc}"

    def start_all(self) -> None:
        """宿主装配完成后启动插件（后台线程/轮询，issue #4 PC）。

        **不在 ``load()`` 里起线程**：``load()`` 在 import 期跑，那时还没有设置，
        也没有发送通道。失败只记一行——一个插件的线程起不来，不该拖垮指令守护。
        """
        for plugin in self._plugins.values():
            try:
                plugin.start(self.host)
            except Exception as exc:  # noqa: BLE001
                self.note_error(f"{plugin.name} (start)", exc)

    def stop_all(self) -> None:
        """与 ``start_all`` 对称。逆序停，后起的先停。"""
        for plugin in reversed(tuple(self._plugins.values())):
            try:
                plugin.stop()
            except Exception as exc:  # noqa: BLE001
                self.note_error(f"{plugin.name} (stop)", exc)

    # ---- 读侧 ---------------------------------------------------------------
    def get(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def plugins(self) -> tuple[Plugin, ...]:
        return tuple(self._plugins.values())

    def command(self, name: str) -> Command | None:
        return self._commands.get(name)

    def owner(self, name: str) -> Plugin | None:
        owner = self._owners.get(name)
        return self._plugins.get(owner) if owner else None

    def commands(self) -> dict[str, tuple[str, Callable]]:
        """``{关键词: (描述, handler)}``，与旧 ``HANDLERS`` 同形状。

        顺序 = 注册顺序 = ``#帮助`` 的顺序；模块级 dict 可被测试整体替换，
        所以这里返回普通 dict 而不是只读视图。
        """
        return {name: (cmd.desc, cmd.handler) for name, cmd in self._commands.items()}

    def error_types(self) -> tuple[type[BaseException], ...]:
        out: list[type[BaseException]] = []
        for plugin in self._plugins.values():
            for exc_type in plugin.error_types:
                if exc_type not in out:
                    out.append(exc_type)
        return tuple(out)

    def hook(self, name: str) -> Callable | None:
        """第一个实现了该钩子的插件。供保留符号（``_risk_blocked`` 等）转发。"""
        for plugin in self._plugins.values():
            fn = getattr(plugin.hooks, name, None)
            if fn is not None:
                return fn
        return None

    def credential_provider(self, settings: Settings) -> CredentialProvider | None:
        for plugin in self._plugins.values():
            make = plugin.hooks.credentials
            if make is None:
                continue
            provider = make(settings)
            if provider is not None:
                return provider
        return None

    def available(self, name: str, settings: Settings, group_id: str = "") -> bool:
        """这条指令现在能不能回。

        没有插件归属的关键词（测试探针、``slash_commands.commands`` 静态条目）
        一律放行——宿主只负责插件门控，不替静态配置做主。
        """
        command = self._commands.get(name)
        if command is None:
            return True
        plugin = self.owner(name)
        if plugin is None:
            return True
        return plugin.enabled(settings, group_id) and command.available(settings, group_id)

    def status_line(self, settings: Settings | None = None) -> str:
        parts: list[str] = []
        legacy = set((settings or {}).get("plugins_legacy") or ())
        for name, plugin in self._plugins.items():
            total = len(plugin.commands)
            if settings is None:
                text = f"{name} 已注册({total} 指令)"
            else:
                live = sum(
                    1 for cmd in plugin.commands
                    if plugin.enabled(settings) and cmd.available(settings))
                text = f"{name} {'已启用' if live else '停用'}({live} 指令)"
            if name in legacy:
                # 旧键还能用，但归一化结果与新键等价；提示一句免得两处配置打架。
                text += f"（仍用旧键 slash_commands.{name}，可迁到 plugins.{name}）"
            extra = plugin.hooks.status(settings) if (plugin.hooks.status and settings is not None) else ""
            if extra:
                text += f" {extra}"
            parts.append(text)
        for name, error in self.load_errors.items():
            parts.append(f"{name} 加载失败: {error}")
        return " / ".join(parts) or "无"


REGISTRY = Registry()


def register(plugin: Plugin, capabilities: Iterable[str] | None = None) -> Plugin:
    return REGISTRY.register(plugin, capabilities)


def status_line(settings: Settings | None = None) -> str:
    return REGISTRY.status_line(settings)
