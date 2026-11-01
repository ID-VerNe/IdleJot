"""B 站字幕指令插件：``#字幕``（issue #339）。

指令型（``Command``），**不是**事件型：群消息每一条都触发网络请求，会撞上
``plugins/base.py`` 里 ``on_event`` 的耗时契约——``commands.py:51 EMIT_TIMEOUT = 1``
秒内拿不到结果就静默跳过。字幕是「有人问了才取」，只有指令型说得清这件事。

``default_enabled=False``：会连外网，而且匿名接口下大概率给不出字幕（离线实测
见 ``bilibili.py`` 模块 docstring）。合入即静默去连外网属于「看着装了其实没装」，
与本仓 sixty / busboss / repeater 同一口径——要开就显式开。

``needs_credential=False``：查个公开字幕不该要求绑定米游社账号（``_handle.need_ck``
与 ``Command.needs_credential`` 两处都要关，前者是 handler 的运行时判据，后者是
``#帮助`` 与注册表的声明）。
"""
from __future__ import annotations

from typing import Mapping

import bilibili

from plugins.base import CAP_COMMANDS, Command, Plugin, positive


def _handle(args, ctx):
    """``#字幕 <b23.tv 短链 | bilibili.com/video/BV… | BV 号> [语言]``。

    空参数时回一句用法，不回「指令执行出错」——那是 handler 炸了的意思。
    """
    question = str(args or "").strip()
    if not question:
        raise bilibili.BilibiliError(
            "用法：#字幕 <B 站视频链接或 BV 号> [语言]\n"
            "例：#字幕 https://b23.tv/xxxxxxx\n"
            "例：#字幕 BV1xx411c7mD zh-CN")
    return bilibili.answer(question, PLUGIN.options(ctx.get("settings") or {}))


_handle.need_ck = False


def _normalize(options: Mapping) -> dict:
    """配置归一：数字类走 ``positive`` 兜底，坏值不炸也不静默变 0。"""
    return {
        "timeout": positive(options.get("timeout"), 0),
        "max_chars": int(positive(options.get("max_chars"), 3000)) or 3000,
        "lang": str(options.get("lang") or "").strip(),
    }


class _BilibiliPlugin(Plugin):
    def normalize(self, options: Mapping) -> dict:
        return _normalize(options)


PLUGIN = _BilibiliPlugin(
    name="bilibili",
    version="1.0",
    commands=(
        Command(name="字幕", desc="取 B 站视频的 CC/AI 字幕（匿名接口）",
                handler=_handle, needs_credential=False),
    ),
    requires=frozenset({CAP_COMMANDS}),
    error_types=(bilibili.BilibiliError,),
    default_enabled=False,
    options_defaults={"timeout": 0, "max_chars": 3000, "lang": ""},
)
