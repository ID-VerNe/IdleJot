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
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def config_path() -> Path:
    """config.json 的路径；``IDLEJOT_WG_CONFIG`` / ``TULPA_WG_CONFIG`` 可覆盖。

    覆盖与 ``wg_paths`` 的 state/db 隔离变量同一族：门禁在干净 checkout 上跑
    ``watch.py`` / ``listen.py`` 时没有 config.json（被 gitignore），#51 之后缺
    self_account 会拒绝启动——没有这个覆盖，门禁要么红在错误的病因上，要么往
    仓库树里写夹具文件（崩在中间就给开发机留一个占位配置）。
    """
    override = os.environ.get("IDLEJOT_WG_CONFIG") or os.environ.get("TULPA_WG_CONFIG")
    return Path(override) if override else ROOT / "water-group" / "config.json"


CONFIG = config_path()

# issue #51：配置缺失曾经整条链路静默退化——self_account 读空让 NapCat 注入少传
# 账号参数只能弹二维码、onebot_token 读空让 --status 误报「OneBot 无响应」、
# wake_reason 的 @ 判定永假让监听永远唤不醒人，而三者表面都「正常」。problems()
# 是把「缺什么」说出来的单一事实来源；config() 的 {} 兜底保持不变——依赖默认值
# 的调用方（commands.load_settings 等）不该被这次修复连带回归。
REQUIRED_KEYS = ("self_account", "onebot_token")
# 与 watchdog.onebot_token()（#17）同一组环境变量：只用 env token 的部署不能
# 被算成「token 未设置」。
TOKEN_ENV_KEYS = ("IDLEJOT_ONEBOT_TOKEN", "TULPA_ONEBOT_TOKEN")


def path() -> Path:
    return CONFIG


def config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def problems() -> list[str]:
    """配置健康度：缺失/占位/缺 token 都被说出来，不静默（issue #51）。

    返回问题列表，空列表 = 健康。只读，不起任何进程、不写任何文件——
    ``up.py --status`` 与注入前的拒绝判定共用这一份。
    """
    if not CONFIG.exists():
        # 一条说全：文件缺失时 self_account / onebot_token 随同缺失，三个下游
        # 症状（注入弹二维码、@ 判不中、--status 误报）都从这一行长出来。带上
        # 路径，值守的人不用猜要去哪恢复。
        return [f"config.json 缺失（{CONFIG} 不存在：self_account 与 onebot_token"
                " 随同缺失——注入只能弹二维码、@ 判不中、--status 误报）"]
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["config.json 不是合法 JSON（解析失败）"]
    if not isinstance(data, dict):
        return ["config.json 顶层不是对象"]
    issues: list[str] = []
    account = str(data.get("self_account") or "").strip()
    if not account:
        issues.append("self_account 未设置（NapCat 无法快速登录，@ 唤醒判不中）")
    elif not account.isdigit():
        # config.example.json 的占位符走这一支：文件在、值是样板，比「缺失」
        # 更隐蔽——看着配过了。
        issues.append(f"self_account 不是 QQ 号（{account!r}，疑似占位符）")
    if not any((os.environ.get(key) or "").strip() for key in TOKEN_ENV_KEYS):
        if not str(data.get("onebot_token") or "").strip():
            issues.append("onebot_token 未设置（OneBot 探针会被拒，--status 误报无响应）")
    return issues
