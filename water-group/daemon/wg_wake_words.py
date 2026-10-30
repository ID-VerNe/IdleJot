"""self-name / wake-word 合并的唯一出处（issue #279）。

之前仓内两套口径：

* ``chatlocal/mcp_chat_wait.py`` 的 ``wait()`` 手搓全合并——顶层 ``self_names`` +
  per-group ``self_names``/``wake_words`` + 人格名（含 split）+ account；
* daemon ``wg_up_wake.wake_words()`` 只取顶层 ``self_names``。

同一个概念两套判定，per-group ``wake_words`` 配了但顶层没配的群，daemon 不认、
server 认（issue #279 的复现命令就是 ``w.wake_words.__doc__ or
'returns config().get(self_names) only'``）。本模块把合并规则提到一个两边都能
import 的层：daemon 脚本走同目录 flat import（``import wg_wake_words``，与
``import wg_aliases`` / ``import wg_config`` 同范式），chatlocal 走延迟 import
（同 ``wg_text.connect_ro`` 门面：``chatlocal/config.py`` 在 import 期建数据目录，
daemon 不能模块级 import chatlocal，反过来 chatlocal import daemon 模块要延迟）。

**合并规则**（与 ``mcp_chat_wait.py:140-159`` 逐字对齐，casefold set）：

1. 顶层 ``cfg.get("self_names")``（list）逐项 str；
2. per-group ``group_cfg.get("self_names") or group_cfg.get("wake_words")``（``or``
   短路：per-group self_names 优先，否则 wake_words），逐项 str；
3. 人格名 ``persona_name``：非空且 ``!= "自定义人格"`` 时加入，并 ``split()`` 后
   每个非空 token 加入；
4. ``account``（= ``conversation_id`` 的账号段）非空加入；
5. ``cfg_account = cfg.get("self_account")``：非空且**不以 ``YOUR_`` 开头**时加入
   （占位符不算，与 ``check_config_fail_loud`` 把 ``YOUR_QQ_ACCOUNT`` 当占位符
   同口径）；
6. 全部 ``k.casefold()``，空串过滤。

**为什么不在 ``wg_aliases.py``**：那个模块已耦合记忆引擎（``alias_map`` 延迟
import ``water_group.memory_engine``），把纯配置解析塞进去会让它背上记忆引擎的
依赖面，违反关注点分离。本模块只依赖 ``wg_config``，零额外耦合——与
``wg_config.py`` 同级、同性质（都是「配置的唯一读取/派生口」）。

**daemon 侧 persona 缺口**：``chatlocal`` 的 ``persona_name`` 来自
``chat_sessions.persona_name``（DB 行，``prompts.persona_name(row)`` 解析人格卡
标题）。daemon 事件流（``wg_events.jsonl``）不带 persona_name，``wake_reason``
拿不到。daemon 侧 persona token 的来源只能是 ``config.groups.<gid>.persona`` /
``default_persona`` 的 preset id（如 ``little_whale``）。preset id 是文件名，不会
出现在群友正文里，所以这一支在 daemon 侧实际几乎不命中——issue 的真实差异在
**per-group ``self_names``/``wake_words``**，那才是 daemon 漏认的部分。persona
对齐到「preset id 进词表」是形式对齐，运行上对 daemon 几乎无新增唤醒；不读卡
标题（读卡每条事件跑一次人格目录扫描，wake 链路不可接受）。
"""
from __future__ import annotations

import wg_config


def _config(cfg):
    """读 config：传入的 cfg 优先（调用方已显式构造/打桩），否则委托 wg_config。

    不能只 ``return cfg or wg_config.config()``：``cfg`` 可能是测试打的空 dict
    ``{}``（合法、表示「没有配置」），``{}`` 是 falsy 会被当成「没传」再回落到
    wg_config——那样测试的桩就被绕过了。
    """
    return cfg if cfg is not None else wg_config.config()


def _group_cfg(cfg, group_id):
    """``cfg.groups.<gid>``；不是 dict / 没有该群返回 ``{}``。"""
    gid = str(group_id or "").strip()
    if not gid:
        return {}
    item = (cfg.get("groups") or {}).get(gid)
    return item if isinstance(item, dict) else {}


def wake_keywords(*, cfg=None, group_id="", persona_name="", account=""):
    """合并后的唤醒词集合（casefold ``set[str]``）。

    单一出处：``mcp_chat_wait.wait()`` 与 ``wg_up_wake.wake_words()`` 都委派到这。
    见模块 docstring 的合并规则。``cfg`` 缺省读 ``wg_config.config()``，与
    ``wg_up_wake.config()`` 同一来源（``IDLEJOT_WG_CONFIG`` 在测试里同样生效）。
    """
    cfg = _config(cfg)
    names = []
    cfg_names = cfg.get("self_names")
    if isinstance(cfg_names, list):
        names.extend(str(n) for n in cfg_names if n)
    group_cfg = _group_cfg(cfg, group_id)
    grp_names = group_cfg.get("self_names") or group_cfg.get("wake_words")
    if isinstance(grp_names, list):
        names.extend(str(n) for n in grp_names if n)
    p_name = str(persona_name or "")
    if p_name and p_name != "自定义人格":
        names.append(p_name)
        for part in p_name.split():
            if part:
                names.append(part)
    acc = str(account or "")
    if acc:
        names.append(acc)
    cfg_account = str(cfg.get("self_account") or "")
    if cfg_account and not cfg_account.startswith("YOUR_"):
        names.append(cfg_account)
    return {k.casefold() for k in names if k}


def wake_words(*, cfg=None, group_id="", persona_name="", account=""):
    """``wake_keywords`` 的有序 list 视图——``wake_reason`` 与既有调用方
    （``for word in wg_up_wake.wake_words()``）拿到的仍是 list。

    顺序按顶层 → per-group → persona → account 的插入序，casefold 后过滤空串。
    与改前 ``wg_up_wake.wake_words()`` 返回 ``list[str]`` 同形状；唯一区别是
    内容现在含 per-group / persona / account。
    """
    seen = []
    seen_set = set()
    for kw in wake_keywords(cfg=cfg, group_id=group_id,
                            persona_name=persona_name, account=account):
        if kw not in seen_set:
            seen_set.add(kw)
            seen.append(kw)
    return seen
