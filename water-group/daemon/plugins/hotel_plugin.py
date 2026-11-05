"""飞猪酒店价格插件：``#酒店``（issue #139 P2）。

取数在 ``flyai.py``（`daemon/flyai.py`，按「文件不搬迁」留在原位）。本文件
只做三件事：把配置归一成运行期形状、把 handler 挂上指令表、声明域异常。

``default_enabled=False``：没配 Key 时 CLI 会用内置体验 key 返回脱敏价
（`¥2x`），且目的地无效时静默降级到别的城市——两种静默错误都需要有人
先确认配置。默认关着比「合入即假装能查」诚实。
"""
from __future__ import annotations

from typing import Mapping

import flyai

from plugins.base import CAP_COMMANDS, Command, Hooks, Plugin, positive


def _handle(args: str, ctx: dict) -> str:
    """``#酒店 目的地 [景点] [入住日期] [退房日期]``。异常由宿主统一翻成回复文案。"""
    usage = ("用法：#酒店 目的地 [景点] [入住日期] [退房日期]\n"
             "例：#酒店 上海 11-01 11-02")
    if not str(args or "").strip():
        raise flyai.HotelError(usage)
    return flyai.answer(args, ctx.get("hotel") or {})


# 宿主按**函数属性**判定要不要走绑定链路（commands.py:491）。群友不需要绑定
# 米游社账号也能查酒店，所以显式关掉，否则未绑定的人会收到绑定提示。
_handle.need_ck = False


def _precheck(ctx: dict, settings: Mapping) -> str | None:
    """把归一后的插件配置交给 handler（与 busboss 同款：ctx 里带域配置）。"""
    ctx["hotel"] = dict(PLUGIN.options(settings))
    return None


def _normalize(options: Mapping) -> dict:
    """配置归一：数字类走 ``positive`` 兜底，坏值不炸也不静默变 0。"""
    sort = str(options.get("sort") or "").strip()
    if sort and sort not in flyai.SORT_VALUES:
        sort = ""
    return {
        "api_key": str(options.get("api_key") or "").strip(),
        "bundle": str(options.get("bundle") or "").strip(),
        "timeout": positive(options.get("timeout"), 0),
        "cache_seconds": positive(options.get("cache_seconds"), 300),
        "max_hotels": int(positive(options.get("max_hotels"), 8)) or 8,
        "hotel_stars": str(options.get("hotel_stars") or "").strip(),
        "hotel_bed_types": str(options.get("hotel_bed_types") or "").strip(),
        "hotel_types": str(options.get("hotel_types") or "").strip(),
        "keywords": str(options.get("keywords") or "").strip(),
        "max_price": int(positive(options.get("max_price"), 0)),
        "sort": sort,
    }


class _HotelPlugin(Plugin):
    def normalize(self, options: Mapping) -> dict:
        return _normalize(options)


PLUGIN = _HotelPlugin(
    name="hotel",
    version="1.0",
    commands=(
        Command(name="酒店", desc="酒店价格查询（飞猪，含起价 / 地址 / 星级）",
                handler=_handle, needs_credential=False),
    ),
    requires=frozenset({CAP_COMMANDS}),
    error_types=(flyai.HotelError,),
    hooks=Hooks(precheck=_precheck),
    default_enabled=False,
    options_defaults={"api_key": "", "bundle": "", "timeout": 0, "cache_seconds": 300,
                      "max_hotels": 8, "hotel_stars": "", "hotel_bed_types": "",
                      "hotel_types": "", "keywords": "", "max_price": 0, "sort": ""},
)
