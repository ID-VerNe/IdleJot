"""车盈网班次插件：``#班次``（issue #103）。

取数在 ``busboss.py``（留在 ``daemon/``，按「文件不搬迁」只搬注册方式）。本文件
只做三件事：把配置归一成运行期形状、把 handler 挂上指令表、声明域异常。

``default_enabled=False``：凭证是部署者本机抓一次包取得的，没配的时候指令
必然答不上来。默认关着比「合入即静默落空」诚实——``#帮助`` 也不会列出它。
"""
from __future__ import annotations

from typing import Mapping

import busboss

from plugins.base import CAP_COMMANDS, Command, Hooks, Plugin, positive


def _handle(args: str, ctx: dict) -> str:
    """``#班次 起点 终点 [日期] [上车站]``。异常由宿主统一翻成回复文案。"""
    return busboss.answer(args, ctx.get("busboss") or {})


# 宿主按**函数属性**判定要不要走绑定链路（commands.py:491）。群友不需要绑定
# 米游社账号也能查班次，所以这里显式关掉，否则未绑定的人会收到绑定提示。
_handle.need_ck = False


def _precheck(ctx: dict, settings: Mapping) -> str | None:
    """把归一后的插件配置交给 handler（与 sixty 同款：ctx 里带域配置）。"""
    ctx["busboss"] = dict(PLUGIN.options(settings))
    return None


def _normalize(options: Mapping) -> dict:
    """配置归一：数字类走 ``positive`` 兜底，坏值不炸也不静默变 0。"""
    return {
        "timeout": positive(options.get("timeout"), 0),
        "cache_seconds": positive(options.get("cache_seconds"), 60),
        "max_classes": int(positive(options.get("max_classes"), 200)) or 200,
        "max_lines": int(positive(options.get("max_lines"), 8)) or 8,
    }


class _BusbossPlugin(Plugin):
    def normalize(self, options: Mapping) -> dict:
        return _normalize(options)


PLUGIN = _BusbossPlugin(
    name="busboss",
    version="1.0",
    commands=(
        Command(name="班次", desc="城际班次查询（车盈网，含票价与余票）",
                handler=_handle, needs_credential=False),
    ),
    requires=frozenset({CAP_COMMANDS}),
    error_types=(busboss.BusbossError,),
    hooks=Hooks(precheck=_precheck),
    default_enabled=False,
    options_defaults={"timeout": 0, "cache_seconds": 60,
                      "max_classes": 200, "max_lines": 8},
)
