"""60 秒资讯插件：14 条指令 + 按指令的白名单门控（issue #4 PA）。

``sixty.allowed`` 本来就是**按指令**判定（``allow=[]`` 全禁、缺省用
``DEFAULT_ALLOW``、``enabled=false`` 全关），所以它落在 ``Command.gate`` 上而不是
插件级 ``enabled``。``#帮助`` 与 ``resolve_reply`` 共用这个 gate，「列了却不回」
的两处特判一起消失。
"""
from __future__ import annotations

from typing import Mapping

import sixty

from plugins.base import CAP_COMMANDS, Command, Hooks, Plugin


def _gate(name: str):
    def gate(settings: Mapping, group_id: str = "") -> bool:
        return sixty.allowed(name, settings.get("sixty") or {})
    return gate


def _precheck(ctx: dict, settings: Mapping) -> str | None:
    """把运行期配置交给 handler（旧 ``run_handler`` 里硬塞的 ``ctx["sixty"]``）。"""
    ctx["sixty"] = settings.get("sixty") or {}
    return None


PLUGIN = Plugin(
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
)
