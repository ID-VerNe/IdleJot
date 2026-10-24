import json
import sys
import time
from pathlib import Path

import requests

ROOT_DIR = Path(__file__).resolve().parents[1]

# When launched as a script this file is __main__, so the repository root is
# not on sys.path yet and the bridge package cannot be found.
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT_DIR))
# 数据根与日志走 ~/.water-group（daemon/wg_paths.py 是单一事实来源）；无条件
# insert，被 import 时同样解析得到。
sys.path.insert(0, str(ROOT_DIR / "water-group" / "daemon"))
from wg_paths import data_dir, state_dir

from water_group.memory_engine import get_memory_engine
from chatlocal import mcp_chat_prompts as prompts
from chatlocal.sqlite_util import connect as sqlite_connect

CONFIG_PATH = ROOT_DIR / "water-group" / "config.json"
DB_PATH = str(data_dir() / "mcp-access.sqlite3")

def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    example_path = ROOT_DIR / "water-group" / "config.example.json"
    if example_path.exists():
        print(f"[Warn] 未找到 {CONFIG_PATH}，加载 {example_path} 默认模板。请创建并配置 config.json。")
        with open(example_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "onebot_http_url": "http://127.0.0.1:43000",
        "onebot_token": "YOUR_ONEBOT_TOKEN",
        "self_account": "YOUR_QQ_ACCOUNT",
        "self_names": ["小鲸鱼", "鲸鱼"],
        "default_persona": "little_whale",
        "default_participation": "natural",
        "default_trigger_mode": "mention_only",
        "groups": {}
    }

def get_onebot_groups(url, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = requests.get(f"{url}/get_group_list", headers=headers, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("data", [])
    except Exception as e:
        print(f"[Warn] 获取 OneBot 群列表失败: {e}")
    return []

def get_onebot_group_members(url, token, group_id):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = requests.post(f"{url}/get_group_member_list", headers=headers, json={"group_id": int(group_id)}, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("data", [])
    except Exception as e:
        print(f"[Warn] 获取群 {group_id} 成员列表失败: {e}")
    return []

def get_active_sessions(db_path):
    if not Path(db_path).exists():
        return {}
    try:
        with sqlite_connect(db_path, timeout=5) as conn:
            rows = conn.execute("SELECT id, conversation_id, name, active, persona_preset, persona_name, persona, participation FROM chat_sessions WHERE active=1").fetchall()
            return {r["conversation_id"]: dict(r) for r in rows}
    except Exception as e:
        print(f"[Info] 会话数据库尚未初始化或无活动会话: {e}")
        return {}


def inspect_and_sync():
    config = load_config()
    ob_groups = get_onebot_groups(config["onebot_http_url"], config["onebot_token"])
    active_sessions = get_active_sessions(DB_PATH)

    account = config.get("self_account", "")
    result = {
        "total_onebot_groups": len(ob_groups),
        "ready_groups": [],
        "pending_activation_groups": []
    }

    print(f"=== QQ 小号多群状态报告 ===")
    print(f"OneBot 当前加入群数: {len(ob_groups)}")

    for g in ob_groups:
        gid = str(g.get("group_id"))
        gname = g.get("group_name", "")
        cid = f"{account}:group:{gid}"

        gcfg = config.get("groups", {}).get(gid, {})
        if gcfg.get("enabled", True) is False:
            print(f"- [已禁用] 群 {gid} ({gname})")
            continue

        persona = gcfg.get("persona") or config.get("default_persona", "little_whale")
        participation = gcfg.get("participation") or config.get("default_participation", "natural")
        trigger_mode = gcfg.get("trigger_mode") or config.get("default_trigger_mode", "mention_only")
        reply_addressing = gcfg.get("reply_addressing") or config.get("default_reply_addressing", "quote")
        member_mappings = gcfg.get("member_mappings") or config.get("member_mappings", {})

        # 同步 OneBot 成员群名片与昵称
        try:
            mem_engine = get_memory_engine()
            ob_members = get_onebot_group_members(config["onebot_http_url"], config["onebot_token"], gid)
            if ob_members:
                mem_engine.sync_onebot_members(ob_members, group_id=gid)
            if member_mappings:
                mem_engine.sync_member_mappings(member_mappings, group_id=gid)
        except Exception as e:
            print(f"[Warn] 同步群 {gid} 成员档案或名片异常: {e}")

        if cid in active_sessions:
            sess = active_sessions[cid]
            # 检查人格或模式是否发生变更，支持热重载
            needs_update = False
            update_fields = {}
            try:
                resolved = prompts.resolve_persona_details(preset=persona)
                if sess.get("persona_preset") != persona or sess.get("persona") != resolved["prompt"] or sess.get("persona_name") != resolved["name"]:
                    needs_update = True
                    update_fields["persona_preset"] = persona
                    update_fields["persona_name"] = resolved["name"]
                    update_fields["persona"] = resolved["prompt"]
            except Exception as e:
                print(f"[Warn] 解析人格 {persona} 失败: {e}")

            if sess.get("participation") != trigger_mode:
                needs_update = True
                update_fields["participation"] = trigger_mode

            if needs_update:
                set_clause = ", ".join(f"{k}=?" for k in update_fields.keys()) + ", updated=?"
                vals = list(update_fields.values()) + [time.time(), sess["id"]]
                with sqlite_connect(DB_PATH, timeout=5) as conn:
                    conn.execute(f"UPDATE chat_sessions SET {set_clause} WHERE id=?", vals)
                sess.update(update_fields)
                print(f"🔄 [热重载] 群 {gid} ({gname}) 已更新 -> 人格: {sess['persona_name']} ({persona}) | 模式: {trigger_mode}")

            mode_desc = "仅@触发" if trigger_mode == "mention_only" else "自然参与"
            addr_desc = "QQ原生引用" if reply_addressing == "quote" else ("定向@" if reply_addressing == "at" else "双重提醒" if reply_addressing == "both" else "自然")
            print(f"✓ [监听就绪] 群 {gid} ({gname}) | 模式: {mode_desc} | 寻址: {addr_desc} | 人格: {sess['persona_name']} | Session: {sess['id']}")
            if member_mappings:
                annotated_summary = ", ".join(f"{v.get('name')}({k})" for k, v in member_mappings.items())
                print(f"  └─ 已标注核心群友: {annotated_summary}")
            result["ready_groups"].append({
                "group_id": gid,
                "group_name": gname,
                "conversation_id": cid,
                "session_id": sess["id"],
                "persona": sess["persona_name"],
                "persona_preset": sess.get("persona_preset", persona),
                "trigger_mode": trigger_mode,
                "reply_addressing": reply_addressing,
                "member_mappings": member_mappings,
                "active": True
            })
        else:
            print(f"! [未激活Session] 群 {gid} ({gname}) | 拟分配人格: {persona}")
            result["pending_activation_groups"].append({
                "group_id": gid,
                "group_name": gname,
                "conversation_id": cid,
                "persona_preset": persona,
                "participation": participation,
                "trigger_mode": trigger_mode,
                "reply_addressing": reply_addressing
            })

    status_file = str(state_dir() / "status.json")
    with open(status_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"状态已更新至: {status_file}")
    print("===========================")
    return result

if __name__ == "__main__":
    inspect_and_sync()
