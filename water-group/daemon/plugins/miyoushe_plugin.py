"""米游社插件：17 条取数指令 + 4 条登录指令 + 凭证域知识（issue #4 PA / PB / PC）。

搬进来的只有「米游社才知道」的东西——管理员白名单、全局 CK、风控冷却、
凭证失效提醒、半个月续期，以及扫码登录的四条指令与轮询线程（PC）。**通用链路**
（读群友绑定、绑定提示、免登录判断、发送与引用）仍在宿主，因为测试探针 handler
也要走那条链路。

PC 的三个结论（协议压力测试的结果，改之前先读）：

* **图片回复不用协议支持**：``#登录`` 直接返回 ``{"text","image"}``，宿主
  ``segments_for`` 认这个形状；
* **多步确认不用协议支持**：``#解绑`` 的两步窗口在 ``qr_login.unbind`` 里，
  有状态交互不必上升为协议概念；
* **后台线程要生命周期**：``start(host)`` 起 poller、``stop()`` 停，且
  ``start()`` 幂等——``load()`` 在 import 期跑，那时还没有设置与发送通道。
"""
from __future__ import annotations

import threading
from typing import Mapping

import miyoushe

from plugins.base import (CAP_CREDENTIAL, CAP_SEND, Command, CredentialProvider, Hooks,
                          Plugin, positive)

try:
    import qr_login
except Exception as exc:  # noqa: BLE001  登录模块坏了不该拖垮取数
    qr_login = None
    _LOGIN_ERROR = f"{type(exc).__name__}: {exc}"
else:
    _LOGIN_ERROR = ""

# 米游社的「未登录 / CK 失效」一族（issue #2 §13.1）。命中就给本人发一次重扫提醒。
CK_INVALID_CODES = (-1, -100, 1001, 10001, 10103)

# 扫码登录的四条指令（issue #6 §4.5）。旧代码是 commands.LOGIN_DESCS 常量，
# PC 起与取数指令同源，`#帮助` 不再有第二处特判。
LOGIN_DESCS = {
    "登录": "扫码绑定米游社账号（凭证只存本机）",
    "登录状态": "查看自己的绑定情况",
    "绑定": "绑定/切换游戏 UID（需先 #登录）",
    "解绑": "解绑米游社账号（再发一次即确认）",
}

_LOGIN_DOWN = "登录功能暂不可用（模块加载失败），稍后再试"


def _noop(message: str) -> None:
    return None


_LOG = _noop  # 由 load(host) 换成宿主的日志（测试改 commands.LOG 时跟着走）

_POLLER = {"running": False}


def login_error() -> str:
    return _LOGIN_ERROR


def _commands() -> tuple[Command, ...]:
    return tuple(
        Command(name=name, desc=desc, handler=fn,
                needs_credential=getattr(fn, "need_ck", True))
        for name, (desc, fn) in miyoushe.HANDLERS.items()
    )


def _login_commands() -> tuple[Command, ...]:
    return tuple(Command(name=key, desc=desc, handler=_login_handler(key),
                         needs_credential=False)
                 for key, desc in LOGIN_DESCS.items())


def _admins(settings: Mapping) -> set[str]:
    return set(PLUGIN.options(settings).get("admin_qq") or [])


def _global_credentials(settings: Mapping) -> tuple[list[str], str]:
    options = PLUGIN.options(settings)
    return list(options.get("ck") or []), str(options.get("uid") or "")


def _normalize(options: Mapping) -> dict:
    """旧 ``slash_commands.miyoushe`` 的归一化搬到这里（issue #4 PB）。

    ``ck`` 允许写成单个字符串（手改配置时最常见），``admin_qq`` 允许标量；
    ``qr_login`` 三个键有缺省值，且 ``daily_limit=0`` 表示不限流，不能被
    ``or`` 吃掉。
    """
    ck = options.get("ck") or []
    if isinstance(ck, str):
        ck = [ck] if ck.strip() else []
    admins = options.get("admin_qq") or []
    if isinstance(admins, (str, int)):
        admins = [admins]
    qr = options.get("qr_login") or {}
    return {
        "ck": [str(c) for c in ck if str(c).strip()],
        "uid": str(options.get("uid") or ""),
        "admin_qq": [str(qq).strip() for qq in admins if str(qq).strip()],
        "qr_login": {
            "enabled": bool(qr.get("enabled", True)),
            "daily_limit": int(positive(qr.get("daily_limit"), 5)),
            "session_ttl": positive(qr.get("session_ttl"), 300),
        },
    }


def _login_options(settings: Mapping) -> Mapping:
    """扫码登录的运行期配置（PB 起长在米游社的 options 里，同域）。"""
    return PLUGIN.options(settings).get("qr_login") or {}


# ---------------------------------------------------------------- 登录指令


def _login_handler(key: str):
    def handler(args: str, ctx: Mapping):
        return _login_reply(key, args, ctx)
    handler.need_ck = False   # type: ignore[attr-defined]
    return handler


def _login_reply(key: str, args: str, ctx: Mapping):
    """``#登录`` / ``#登录状态`` / ``#绑定`` / ``#解绑``（issue #6 §4.5）。

    ``QrLoginError`` 不在这里接：它进 ``error_types``，由宿主的 ``run_handler``
    统一翻成回复文案（协议的统一出口，别在插件里各写一份）。

    两步确认在 ``qr_login.unbind`` 里收口：窗口内再发一次裸 ``#解绑`` 也算确认。
    旧代码在这里硬比 ``args == "确认"``，而提示文案让人「再发一次 #解绑」，
    于是死循环（#解绑 踩过）。
    """
    if qr_login is None:
        _LOG(f"login unavailable: {_LOGIN_ERROR}")
        return _LOGIN_DOWN
    user_id = str(ctx.get("user_id") or "")
    if not user_id or user_id == "0":
        return "这条指令要群友本人在群里发（合成测试事件没有真实 QQ）"
    settings = ctx.get("settings") or {}
    if key == "绑定":
        return qr_login.bind_uid(user_id, args)
    if key == "登录":
        if not _login_options(settings).get("enabled", True):
            return "扫码登录未开启（config → plugins.miyoushe.options.qr_login.enabled）"
        session = qr_login.start_login(settings, user_id, ctx.get("group_id"))
        return {"text": "5 分钟内用米游社 App 扫这张码\n"
                        "凭证只加密存本机，机器人不会发出去；随时 #解绑 可删",
                "image": qr_login.qr_image_segment(session["url"])}
    if key == "登录状态":
        return qr_login.status_text(user_id)
    return qr_login.unbind(user_id, args)


def _notify(host, group_id, qq, text) -> None:
    """扫码结果回推。通知失败只记一行，不能杀死轮询线程。"""
    try:
        host.notify(group_id, qq, text)
    except Exception as exc:  # noqa: BLE001
        _LOG(f"qr notify failed group={group_id}: {type(exc).__name__}: {exc}")


def _start(host) -> None:
    """起扫码轮询（issue #6 §4.3）。幂等：``start_all`` 重入不该起第二个线程。"""
    if qr_login is None or _POLLER["running"]:
        return
    qr_login.start_poller(lambda group_id, qq, text: _notify(host, group_id, qq, text),
                          cfg_getter=lambda: _login_options(host.settings()))
    _POLLER["running"] = True
    _LOG("qr-login poller started")


def _stop() -> None:
    if qr_login is not None:
        qr_login.stop_poller()
    _POLLER["running"] = False


def _status(settings: Mapping) -> str:
    if qr_login is None:
        return f"（登录模块加载失败：{_LOGIN_ERROR}）"
    return "登录轮询 运行中" if _POLLER["running"] else "登录轮询 未启动"


# ---------------------------------------------------------------- 取数域钩子


def _blocked(ctx: Mapping, settings: Mapping | None = None) -> str:
    """风控冷却期内不发请求，直接回文案（#55-C）。

    米游社的 1034/5003 是**账号级**状态，继续重试只会拖长风控时间
    （miao-plugin #396 的做法就是「把出验证的 ck 暂时屏蔽掉」）。
    返回空串表示放行。

    登录指令（``needs_credential=False``）没有 ck，直接放行——风控是取数的事。
    """
    ck = (ctx.get("ck") or [""])[0]
    if not ck:
        return ""
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


def _pending(user_id: str) -> frozenset[str]:
    """多步确认未完成的指令名（``#解绑`` 的 60s 窗口）。

    宿主拿它豁免冷却：否则 5s 闸门会把第二步静默丢掉（issue #66）。
    """
    if qr_login is None or not user_id:
        return frozenset()
    return qr_login.pending_confirmation_keys(user_id)


def _send_failed(ctx: Mapping, settings: Mapping) -> None:
    """二维码没发出去：撤销会话并退还本次额度（#41-E2）。

    不撤的话用户要白等 5 分钟才收到「已过期」，当天 5 次额度还白扣一次。
    """
    if qr_login is None or ctx.get("key") != "登录":
        return
    qr_login.discard_session(ctx.get("user_id"))


def _credentials(settings: Mapping) -> CredentialProvider:
    return CredentialProvider(admins=_admins, global_credentials=_global_credentials)


class _MiyoushePlugin(Plugin):
    """``load()`` 拿日志句柄；``normalize`` 收口旧配置；``start/stop`` 管轮询。"""

    def load(self, host) -> None:
        global _LOG
        _LOG = host.log

    def normalize(self, options: Mapping) -> dict:
        return _normalize(options)

    def start(self, host) -> None:
        _start(host)

    def stop(self) -> None:
        _stop()


PLUGIN = _MiyoushePlugin(
    name="miyoushe",
    version="1.0",
    commands=_commands() + _login_commands(),
    # CAP_SEND：扫码结果要主动 @ 本人回推（PC 补的宿主能力）。
    requires=frozenset({CAP_CREDENTIAL, CAP_SEND}),
    # 取数域异常 + 登录域异常：宿主统一 `str(exc)` 当回复。
    error_types=((miyoushe.MiyousheError, qr_login.QrLoginError) if qr_login
                 else (miyoushe.MiyousheError,)),
    hooks=Hooks(
        credentials=_credentials,
        precheck=_blocked,
        cooldown=_blocked,
        note_error=_note_error,
        on_resolved=_on_resolved,
        pending=_pending,
        on_send_failed=_send_failed,
        status=_status,
    ),
)
