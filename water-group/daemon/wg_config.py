"""``water-group/config.json`` 的唯一读取口（issue #4 PA0）。

为什么单独一个文件：``commands.load_settings()`` 原来 ``import up`` 只为读一份
配置，结果把整个 guard 树拖进了指令守护的 AST 闭包——实测 26 个文件，含
``supervisor`` / ``bootstrap`` / ``followup`` / ``memory_engine``，于是「改记忆引擎」
会连带重启 ``commands``（``reload.Planner`` 实测
``memory_engine.py -> ['app','commands','watchdog']``）。

配置读取只依赖 ``pathlib`` 与 ``json``，不依赖任何守护模块。``up.config()``
改为委托这里，保证只有一份实现。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "water-group" / "config.json"


def path() -> Path:
    return CONFIG


def config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
