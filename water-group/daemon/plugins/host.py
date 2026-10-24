"""宿主侧通用逻辑：凭证解析（issue #4 PA）。

为什么凭证解析留在宿主、而不是塞进米游社插件：``check_qr_login.py`` 会往
``commands.HANDLERS`` 里塞探针 handler（不在注册表里），却要求它照样拿到
``ctx.ck/uid`` 并触发 ``BIND_HINT``。所以「读群友绑定 → 管理员兜底 → 提示绑定」
这条链路是宿主契约，插件只提供两个域问题：管理员白名单、全局凭证是什么。

``user_store`` 是平台模块（非 Windows 可能缺失），导入失败时退回旧行为：
把全局凭证填进 ctx，别把取数全掐死。
"""
from __future__ import annotations

from typing import Any, Mapping

from plugins.base import Plugin, PluginError, Settings

try:
    import user_store
except Exception as exc:  # noqa: BLE001  非 Windows / 依赖缺失：退回全局凭证
    user_store = None
    _USER_STORE_ERROR = f"{type(exc).__name__}: {exc}"
else:
    _USER_STORE_ERROR = ""

# §4.2：未绑定群友的取数指令统一回这句，不再拿管理员的全局 CK 顶。
BIND_HINT = "该指令需要绑定：发 #登录 扫码绑定米游社账号（凭证只存本机，#解绑 可删）"

CAPABILITIES = frozenset({"commands", "credential", "send"})


def user_store_error() -> str:
    return _USER_STORE_ERROR


def _global(provider, settings: Settings) -> tuple[list[str], str]:
    if provider is not None:
        return provider.global_credentials(settings)
    return [], ""


def resolve_credentials(ctx: dict[str, Any], settings: Settings, user_id: str,
                        need_ck: bool, plugin: Plugin | None = None,
                        provider=None) -> str | None:
    """把 ck/uid 填进 ctx；返回非空文案表示直接回给用户。

    优先级与改前一致：本人绑定 → 全局 CK（仅 admin 白名单 / 合成事件）→ 绑定提示。
    ``need_ck`` 为假时（``#公告`` 一类免登录接口）不看绑定也不发全局 CK。
    """
    if provider is None and plugin is not None and plugin.hooks.credentials is not None:
        provider = plugin.hooks.credentials(settings)
    record = (user_store.load_user(user_id)
              if (user_store is not None and user_id) else None)
    if record:
        ck = user_store.ck_string(record)
        ctx["ck"] = [ck] if ck else []
        ctx["uid"] = user_store.active_uid(record)
        if plugin is not None and plugin.hooks.on_resolved is not None:
            plugin.hooks.on_resolved(user_id, record)
        return None
    if user_store is None:
        ctx["ck"], ctx["uid"] = _global(provider, settings)
        return None
    if not need_ck:
        return None
    admins = provider.admins(settings) if provider is not None else set()
    if user_id and user_id != "0" and user_id not in admins:
        return BIND_HINT
    ctx["ck"], ctx["uid"] = _global(provider, settings)
    return None


class CommandsHost:
    """``commands`` 进程的宿主面：日志 + 主动发送（issue #4 PC）。

    主动发送走 ``sender``——插件不碰 ``http_url``/``token``，也绕不过宿主的引用、
    编码与失败处理。``settings_provider`` / ``sender`` 由 ``commands`` 用**延迟
    绑定**传进来：``load_all`` 在 import 期跑，那时 ``send_group_msg`` 还没定义。
    """

    name = "commands"
    capabilities = CAPABILITIES

    def __init__(self, log=None, settings_provider=None, sender=None) -> None:
        self._log = log or (lambda message: None)
        self._settings = settings_provider or (lambda: {})
        self._sender = sender

    def log(self, message: str) -> None:
        self._log(message)

    def settings(self) -> Mapping:
        return self._settings()

    def send(self, group_id: str, payload: Any) -> None:
        if self._sender is None:
            raise PluginError("commands 宿主没有发送通道")
        self._sender(self.settings(), str(group_id), payload)

    def notify(self, group_id: str, qq: str, text: str) -> None:
        """@ + 文本的形状只写这一处（旧 ``commands.notify_login`` 的落点）。"""
        self.send(group_id, [{"type": "at", "data": {"qq": str(qq)}},
                             {"type": "text", "data": {"text": " " + str(text)}}])

    def store(self, plugin: str, name: str) -> Any:
        raise NotImplementedError("commands 宿主不提供独立存储")
