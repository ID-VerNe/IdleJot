"""米游社取数插件：17 条指令 + 凭证域知识（issue #4 PA）。

搬进来的只有「米游社才知道」的东西——管理员白名单、全局 CK、风控冷却、
凭证失效提醒、半个月续期。**通用链路**（读群友绑定、绑定提示、免登录判断、
发送与引用）仍在宿主，因为测试探针 handler 也要走那条链路。
"""
from __future__ import annotations

import threading
from typing import Mapping

import miyoushe

from plugins.base import CAP_CREDENTIAL, Command, CredentialProvider, Hooks, Plugin

try:
    import qr_login
except Exception as exc:  # noqa: BLE001  登录模块坏了不该拖垮取数
    qr_login = None
    _LOGIN_ERROR = f"{type(exc).__name__}: {exc}"
else:
    _LOGIN_ERROR = ""

# 米游社的「未登录 / CK 失效」一族（issue #2 §13.1）。命中就给本人发一次重扫提醒。
CK_INVALID_CODES = (-1, -100, 1001, 10001, 10103)


def _noop(message: str) -> None:
    return None


_LOG = _noop  # 由 load(host) 换成宿主的日志（测试改 commands.LOG 时跟着走）


def login_error() -> str:
    return _LOGIN_ERROR


def _commands() -> tuple[Command, ...]:
    return tuple(
        Command(name=name, desc=desc, handler=fn,
                needs_credential=getattr(fn, "need_ck", True))
        for name, (desc, fn) in miyoushe.HANDLERS.items()
    )


def _admins(settings: Mapping) -> set[str]:
    section = settings.get("miyoushe") or {}
    return {str(qq).strip() for qq in (section.get("admin_qq") or []) if str(qq).strip()}


def _global_credentials(settings: Mapping) -> tuple[list[str], str]:
    section = settings.get("miyoushe") or {}
    return list(section.get("ck") or []), str(section.get("uid") or "")


def _blocked(ctx: Mapping, settings: Mapping | None = None) -> str:
    """风控冷却期内不发请求，直接回文案（#55-C）。

    米游社的 1034/5003 是**账号级**状态，继续重试只会拖长风控时间
    （miao-plugin #396 的做法就是「把出验证的 ck 暂时屏蔽掉」）。
    返回空串表示放行。
    """
    ck = (ctx.get("ck") or [""])[0]
    left = miyoushe.risk_wait(ck)
    if not left:
        return ""
    return (f"这个号刚被米游社风控挡住，还要等约 {max(1, left // 60)} 分钟；"
            "反复查只会更久。去米游社 App「我的 → 我的角色」过一次验证可以立刻解开")


def _note_error(ctx: Mapping, settings: Mapping, exc: BaseException) -> str | None:
    """记风控冷却 + 凭证失效提醒（issue #2 §13.1 / #55-C）。"""
    code = getattr(exc, "code", None)
    ck = (ctx.get("ck") or [""])[0]
    if code in miyoushe.RISK_CODES and miyoushe.note_risk(ck, code):
        _LOG(f"risk cooldown code={code} bucket={miyoushe.fp_bucket(ck)[:6]}…"
             f" {miyoushe.RISK_COOLDOWN // 60}min")
    if qr_login is None or code not in CK_INVALID_CODES:
        return None
    qq = str(ctx.get("user_id") or "")
    if not qq or qq == "0":
        return None
    if qr_login.mark_invalid(qq):
        return "你的米游社凭证失效了（可能过期或被解绑），发 #登录 重新扫码"
    return None


def _on_resolved(qq: str, record: Mapping) -> None:
    """取数前顺手续期（#6 §6 P2）：半个月没换过票就后台换一次，不阻塞本条指令。"""
    if qr_login is None or not qr_login.should_renew(record):
        return

    def worker():
        try:
            qr_login.renew(qq, record)
        except Exception as exc:  # noqa: BLE001  续期失败不能冒到 WS 循环
            _LOG(f"renew failed qq={qq}: {type(exc).__name__}")

    threading.Thread(target=worker, name="qr-renew", daemon=True).start()


def _credentials(settings: Mapping) -> CredentialProvider:
    return CredentialProvider(admins=_admins, global_credentials=_global_credentials)


class _MiyoushePlugin(Plugin):
    """只为拿一个可覆写的 ``load()``（``Plugin`` 是 frozen dataclass，实例不能改）。"""

    def load(self, host) -> None:
        global _LOG
        _LOG = host.log


PLUGIN = _MiyoushePlugin(
    name="miyoushe",
    version="1.0",
    commands=_commands(),
    requires=frozenset({CAP_CREDENTIAL}),
    error_types=(miyoushe.MiyousheError,),
    hooks=Hooks(
        credentials=_credentials,
        precheck=_blocked,
        cooldown=_blocked,
        note_error=_note_error,
        on_resolved=_on_resolved,
    ),
)
