"""60 秒资讯插件：14 条指令 + 按指令的白名单门控（issue #4 PA / PB）。

``sixty.allowed`` 本来就是**按指令**判定（``allow=[]`` 全禁、缺省用
``DEFAULT_ALLOW``），所以它落在 ``Command.gate`` 上而不是插件级 ``enabled``。
PB 之后插件级 ``enabled`` 是三态（总开关 / 群白名单 / 群级关闭），两者串联：
插件关掉 = 全禁；插件开着 = 再看 ``allow``。组合只写一次（``_allow_cfg``），
``#帮助`` 与 ``resolve_reply`` 共用同一个 gate，「列了却不回」的两处特判一起消失。

配置默认值与归一化也在本文件（``options_defaults`` / ``normalize``）：宿主
``settings_from`` 不再认识 ``base_url`` / ``allow`` 这些字段（issue #4 PB）。
"""
from __future__ import annotations

from typing import Mapping

import sixty

from plugins.base import CAP_COMMANDS, Command, Hooks, Plugin, positive


def _allow_cfg(settings: Mapping, group_id: str = "") -> dict:
    """拼出 ``sixty.allowed`` 认的形状：三态开关 + 归一后的 allow。

    保持 ``sixty.allowed`` 是白名单语义的唯一实现——它的 5 条断言在
    ``check_sixty.py`` 里，另写一份判定就会「测的是一套、跑的是另一套」。
    """
    return {"enabled": PLUGIN.enabled(settings, group_id),
            "allow": PLUGIN.options(settings).get("allow")}


def _gate(name: str):
    def gate(settings: Mapping, group_id: str = "") -> bool:
        return sixty.allowed(name, _allow_cfg(settings, group_id))
    return gate


def _precheck(ctx: dict, settings: Mapping) -> str | None:
    """把运行期配置交给 handler（旧 ``run_handler`` 里硬塞的 ``ctx["sixty"]``）。"""
    ctx["sixty"] = dict(PLUGIN.options(settings))
    return None


def _normalize(options: Mapping) -> dict:
    """旧 ``slash_commands.sixty`` 的归一化搬到这里（issue #4 PB）。

    ``allow`` 三态：缺省（``None``）= 用 ``sixty.DEFAULT_ALLOW``、``[]`` = 全禁、
    非空 = 白名单。空串一律丢掉——``allow: ["天气", ""]`` 不该让人以为放行了一个
    叫空字符串的指令。``max_items=0`` 回落到 5（和旧 ``or 5`` 一致），
    ``timeout=0`` 保留（表示用 ``sixty.TIMEOUT``）。
    """
    allow = options.get("allow")
    if allow is not None:
        allow = [str(k).strip() for k in allow if str(k).strip()]
    return {
        "base_url": str(options.get("base_url") or "").strip(),
        "timeout": positive(options.get("timeout"), 0),
        "max_items": int(positive(options.get("max_items"), 5)) or 5,
        "default_city": str(options.get("default_city") or "").strip(),
        "allow": allow,
    }


class _SixtyPlugin(Plugin):
    def normalize(self, options: Mapping) -> dict:
        return _normalize(options)


PLUGIN = _SixtyPlugin(
    name="sixty",
    version="1.0",
    commands=tuple(
        Command(name=name, desc=desc, handler=fn,
                needs_credential=getattr(fn, "need_ck", False), gate=_gate(name))
        for name, (desc, fn) in sixty.HANDLERS.items()
    ),
    requires=frozenset({CAP_COMMANDS}),
    error_types=(sixty.SixtyError,),
    hooks=Hooks(precheck=_precheck),
    # 旧行为：PR 合入但没开配置时指令静默落空。默认关，不是忘了开。
    default_enabled=False,
    options_defaults={"base_url": "", "timeout": 0, "max_items": 5,
                      "default_city": "", "allow": None},
)
