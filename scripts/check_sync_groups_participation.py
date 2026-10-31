"""issue #234 gate: sync_groups 不得用派生缺省改写会话参与度。

症状（issue #234）：群配置只写了 ``enabled`` 时，``sync_groups.inspect_and_sync()``
每次启动都把该群 ``chat_sessions.participation`` 改写成非法的 ``mention_only``，
并且每群必然打一条假的「🔄 [热重载]」。

根因在 ``sync_groups.py`` 的两处缺省互不相干：

  * ``participation`` 缺省是 ``natural``（提示词 MODES 之一）；
  * ``trigger_mode`` 缺省是 ``mention_only``（**不是**参与度取值）；
  * 旧代码拿后者覆盖前者写进 ``participation`` 列 —— 值落在 ``prompts.MODES``
    之外，提示词层回落 ``natural``，而用户显式配的 ``active`` 就这样被抹掉。

``trigger_mode`` 本来不需要落进会话表：``wg_up_wake.trigger_mode()`` 每次现读
config，``status.json`` 的 ``trigger_mode`` 由 ``sync_groups`` 独立派生。

本门禁钉五件事，每组带对照组（只断言「不再改写」会被「干脆什么都不写」的坏
实现骗过）：

  1. 群只写了 ``enabled``：三轮同步后 ``participation`` 原值不变、库值仍在
     ``prompts.MODES`` 里、输出里不出现「🔄 [热重载]」与「🔄 [配置同步]」；
  2. 群显式写 ``participation``：按配置对齐一次，日志用「🔄 [配置同步]」而不是
     「[热重载]」，第二轮起安静（对照 1：证明不是「什么都不写」）；
  3. 群显式写的值不在 ``MODES`` 里：打 ``[Warn]`` 且**不写库**（fail-loud，
     不拿非法值覆盖已有会话）；
  4. 人格真的变了时「🔄 [热重载]」照旧出现（对照 1：证明拆日志没有把真改写
     一起静音）；
  5. ``status.json`` 的 ``trigger_mode`` 仍按 config 派生（``mention_only``），
     即「不写进会话表」不等于「不再报告」。

**已知边界**：不迁移库里已有的历史 ``mention_only``（issue 明确不做）；不连真
实 OneBot（``get_onebot_groups`` / 成员同步全部打桩）。

    .venv/Scripts/python.exe scripts/check_sync_groups_participation.py
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)
from check_fixture_support import pin_state_env, run_cases, scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False)

# 数据根 / 状态目录 / 会话库全部钉进临时目录，且在 import 任何 water_group 模块
# **之前**完成：memory_store 的 WG_HOME / DEFAULT_DB_PATH 是 import 期求值。
# state 两腿钉合走 pin_state_env（#281）。
SCRATCH = scratch_dir(prefix="wg_sync_participation_")
STATE = SCRATCH / "state"
DATA = SCRATCH / "data"
STATE.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)
CONFIG = SCRATCH / "config.json"
DB = DATA / "mcp-access.sqlite3"
os.environ["WATER_GROUP_HOME"] = str(SCRATCH)
pin_state_env(STATE)
os.environ["IDLEJOT_WG_DB"] = str(DB)

ACCOUNT = "10002"
GROUP = "333"
CONV = f"{ACCOUNT}:group:{GROUP}"
SESSION_ID = "sess-234"

import water_group.sync_groups as sg  # noqa: E402
from chatlocal import mcp_chat_prompts as prompts  # noqa: E402

# 落点在 import 期就钉死，不等 setup()：否则用例可能先读到仓库里的真实 config.json
# 与实机库（本门禁第一版就这样假绿过一次）。
sg.CONFIG_PATH = CONFIG
sg.DB_PATH = str(DB)


class _NullEngine:
    """成员/名片同步是无关路径，打桩掉，不让它碰真实记忆库。"""

    def sync_onebot_members(self, *args, **kwargs):
        pass

    def sync_member_mappings(self, *args, **kwargs):
        pass


def write_config(gcfg, **extra):
    payload = {"self_account": ACCOUNT, "onebot_http_url": "http://127.0.0.1:1",
               "onebot_token": "", "default_persona": "little_whale",
               "groups": {GROUP: gcfg}}
    payload.update(extra)
    CONFIG.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def seed_session(participation, *, persona=None):
    """重建会话表，写入一行；persona 默认取真实解析值，避免误触人格路径。"""
    resolved = prompts.resolve_persona_details(preset="little_whale")
    with sqlite3.connect(str(DB)) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS chat_sessions (
            id TEXT PRIMARY KEY, conversation_id TEXT, name TEXT, active INTEGER,
            persona_preset TEXT, persona_name TEXT, persona TEXT,
            participation TEXT, updated REAL)""")
        conn.execute("DELETE FROM chat_sessions")
        conn.execute("INSERT INTO chat_sessions VALUES(?,?,?,?,?,?,?,?,?)",
                     (SESSION_ID, CONV, "夹具群", 1, "little_whale", resolved["name"],
                      resolved["prompt"] if persona is None else persona,
                      participation, time.time()))
        conn.commit()


def current_participation():
    with sqlite3.connect(str(DB)) as conn:
        row = conn.execute(
            "SELECT participation FROM chat_sessions WHERE id=?", (SESSION_ID,)).fetchone()
    return row[0] if row else None


def sync_once():
    """跑一轮 inspect_and_sync()，返回 (输出, status.json 内容)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = sg.inspect_and_sync()
    out = buf.getvalue()
    status_path = STATE / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    return out, result, status


def setup():
    # 隔离断言：库与配置必须落在临时目录里，够不着仓库 config.json 与实机库。
    assert Path(sg.DB_PATH).resolve() == DB.resolve(), f"DB_PATH 未钉住: {sg.DB_PATH}"
    assert Path(sg.CONFIG_PATH).resolve() == CONFIG.resolve(), \
        f"CONFIG_PATH 未钉住: {sg.CONFIG_PATH}"
    sg.get_onebot_groups = lambda url, token: [{"group_id": int(GROUP), "group_name": "夹具群"}]
    sg.get_onebot_group_members = lambda url, token, gid: []
    from water_group import memory_engine
    memory_engine.get_memory_engine = lambda *args, **kwargs: _NullEngine()


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def fail(msg):
    """失败时把说明打进输出并返回 None。

    ``run_cases`` 按 truthy 判定通过，**不能**返回说明字符串——非空字符串在
    那里等于绿灯（本门禁第一版就这么假绿过一次，issue #234）。
    """
    print(f"    {msg}")
    return None


# ---------- 1：只配 enabled 的群，会话不许被动 ----------


@case
def 未配参与度时不再改写会话():
    """只写 enabled：三轮同步后 participation 仍是原值 active，且不出现任何改写日志。

    这是 issue #234 的原始症状：旧代码第一轮就写成 'mention_only' 并打热重载。
    """
    write_config({"enabled": True})
    seed_session("active")
    outs = []
    for _ in range(3):
        out, _result, _status = sync_once()
        outs.append(out)
        if current_participation() != "active":
            return fail(f"participation 被改写成 {current_participation()!r}"
                        f"（期望 'active'）\n{outs[-1]}")
    after = current_participation()
    if after not in prompts.MODES:
        return fail(f"库值 {after!r} 不在 MODES {sorted(prompts.MODES)} 里")
    noisy = [o for o in outs if "🔄 [热重载]" in o or "🔄 [配置同步]" in o]
    return True if not noisy else fail(f"未配参与度却打了改写日志：\n{noisy[0]}")


# ---------- 2：显式配置照旧对齐（对照 1） ----------


@case
def 显式参与度按配置对齐且日志不冒充热重载():
    """群显式写 participation='quiet'：第一轮同步成 quiet，日志是「配置同步」，
    第二轮安静。同时证明 1 不是「干脆什么都不写」。"""
    write_config({"enabled": True, "participation": "quiet"})
    seed_session("active")
    first_out, _r, _s = sync_once()
    after_first = current_participation()
    if after_first != "quiet":
        return fail(f"第一轮 participation={after_first!r}（期望 'quiet'）\n{first_out}")
    if "🔄 [配置同步]" not in first_out:
        return fail(f"缺 [配置同步] 日志：\n{first_out}")
    if "🔄 [热重载]" in first_out:
        return fail(f"参与度对齐不该报成热重载：\n{first_out}")
    second_out, _r, _s = sync_once()
    if "🔄 [配置同步]" in second_out or "🔄 [热重载]" in second_out:
        return fail(f"第二轮仍在改写（不幂等）：\n{second_out}")
    return True


# ---------- 3：非法值 fail-loud 且不写库 ----------


@case
def 非法参与度告警且不写库():
    """显式写 participation='mention_only'（不是参与度取值）：打 [Warn]，库值保持原样。

    这正是旧代码会写进库的那个值，所以本条钉的是「不许把非法值写进会话」。
    """
    write_config({"enabled": True, "participation": "mention_only"})
    seed_session("active")
    out, _result, _status = sync_once()
    after = current_participation()
    if after != "active":
        return fail(f"非法值被写进库：participation={after!r}（期望保持 'active'）\n{out}")
    if "[Warn]" not in out or "mention_only" not in out:
        return fail(f"缺 [Warn] 告警：\n{out}")
    return True


# ---------- 4：人格真的变了仍报热重载（对照 1） ----------


@case
def 人格变更仍报热重载():
    """会话里的人格与配置不符时，「🔄 [热重载]」照旧出现。

    对照 1：证明拆分日志没有把真改写一起静音；参与度字段同时保持不动。
    """
    write_config({"enabled": True})
    seed_session("active", persona="stale-prompt")
    out, _result, _status = sync_once()
    if "🔄 [热重载]" not in out:
        return fail(f"人格变更后缺 [热重载] 日志：\n{out}")
    if current_participation() != "active":
        return fail(f"人格路径把参与度带改成了 {current_participation()!r}\n{out}")
    return True


# ---------- 5：status.json 仍派生 trigger_mode ----------


@case
def 状态报告仍派生触发模式():
    """不写进会话表 ≠ 不再报告：status.json 的 trigger_mode 仍按 config 派生为
    'mention_only'（缺省），显式配 natural 时随配置变。"""
    write_config({"enabled": True})
    seed_session("active")
    _out, result, status = sync_once()
    groups = status.get("ready_groups") or []
    if not groups:
        return fail(f"status.json 缺 ready_groups：{status}")
    if groups[0].get("trigger_mode") != "mention_only":
        return fail(f"缺省 trigger_mode={groups[0].get('trigger_mode')!r}"
                    f"（期望 'mention_only'）")
    if (result.get("ready_groups") or [{}])[0].get("trigger_mode") != "mention_only":
        return fail(f"返回值的 trigger_mode 与 status.json 不一致：{result.get('ready_groups')}")
    write_config({"enabled": True, "trigger_mode": "natural"})
    _out, _result, status_nat = sync_once()
    got = (status_nat.get("ready_groups") or [{}])[0].get("trigger_mode")
    return True if got == "natural" else fail(f"显式 natural 时 trigger_mode={got!r}")


def main():
    return run_cases(CASES, setup=setup)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(SCRATCH, ignore_errors=True)
