import os
import sys
import socket
import argparse
import json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

# bootstrap.py 是水群一键后台启动的编排器（issue #277 R1 三拆后）：
#   - bootstrap_win32.py：win32 沙箱/崩溃框工具 + start_process_detached（零耦合）
#   - bootstrap_napcat.py：QQ.exe 解析 + launcher patch + 直/bat 启动 + WMI 进程跟踪 + ensure_napcat
#   - bootstrap_tulpa.py：Tulpa MCP HTTP JSON-RPC 健康探针与就绪屏障
# 本文件留 is_port_open / ensure_config / get_python_exe / configured_account /
# run_sync / main（CLI/argparse/persona 卡/ready-session printout），并 re-export
# 三个拆分模块的公共函数，保持 `from water_group import bootstrap; bootstrap.<func>`
# 与 `from water_group.bootstrap import <func>` 两条公共面不变（零 caller churn）。

# When launched as a script (`python water-group/bootstrap.py`) this file is
# __main__, so the repository root is not on sys.path yet and the bridge
# package cannot be found. Importing through the bridge is what keeps every
# water-group file loaded under exactly one module name.
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT_DIR))
# 本地数据根（~/.water-group）的单一事实来源在 daemon/wg_paths.py，这边 insert
# 后 flat import，避免第二份路径规则各自漂移。
sys.path.insert(0, str(Path(__file__).resolve().parent / "daemon"))
from wg_config import config_path  # noqa: E402  (config.json 路径单一规则，issue #51)
from wg_paths import logs_dir, personas_dir, state_dir

NAPCAT_DIR = ROOT_DIR / "tools" / "napcat-shell"
# 与 wg_config 同一份路径规则（env 覆盖同族）：本文件自己读 config.json 的两处
# （configured_account / ensure_config）不许各写一份路径。
CONFIG_FILE = config_path()
CONFIG_EXAMPLE = ROOT_DIR / "water-group" / "config.example.json"
STATUS_FILE = state_dir() / "status.json"
LOGS_DIR = logs_dir()

# 三拆的私有子模块先拉起（water_group 桥接包经 __init__.py 的 __path__ 扩展把
# water-group 加进来，故 water_group.bootstrap_win32 等全限定名在此可达）。
# bootstrap_win32 是 DAG 叶子（is_port_open / get_python_exe / start_process_detached /
# sandbox_dll / silence_crash_dialogs），bootstrap_napcat 与 bootstrap_tulpa 从它取
# 低层工具，避免反向 import 本模块的循环依赖。本模块 re-export 三个拆分模块的公共
# 函数与常量，保持 `from water_group import bootstrap; bootstrap.<name>` 与
# `from water_group.bootstrap import <name>` 两条公共面不变（零 caller churn）。
from water_group.bootstrap_win32 import (  # noqa: E402
    SEM_FAILCRITICALERRORS,
    SEM_NOGPFAULTERRORBOX,
    silence_crash_dialogs,
    sandbox_dll,
    start_process_detached,
    is_port_open as _is_port_open_impl,
    get_python_exe as _get_python_exe_impl,
)
from water_group.bootstrap_napcat import (  # noqa: E402
    QQ_INSTALL_CANDIDATES,
    LAUNCH_FILE,
    LAST_LAUNCH,
    registry_qq_exe,
    resolve_qq_exe,
    wait_napcat_ports,
    wait_napcat_login,
    ports_up_login_missing,
    LAUNCHER_OLD_SWITCH,
    LAUNCHER_NEW_SWITCH,
    ensure_launcher_patch,
    launch_napcat_direct,
    qq_extra_args,
    _record_launch,
    last_launch,
    _qq_processes,
    _launch_instance_pids,
    _load_launch_record,
    read_launch_record,
    injected_instance_pids,
    count_injected_instances,
    kill_stale_instances,
    ensure_napcat,
)
from water_group.bootstrap_tulpa import (  # noqa: E402
    check_tulpa_mcp_health,
    wait_tulpa_mcp_ready,
    ensure_tulpa,
)


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
    return _is_port_open_impl(port, host, timeout)

def ensure_config():
    if not CONFIG_FILE.exists():
        if CONFIG_EXAMPLE.exists():
            import shutil
            shutil.copy2(CONFIG_EXAMPLE, CONFIG_FILE)
            print(f"[Init] 已从 config.example.json 创建初始配置文件: {CONFIG_FILE}")
        else:
            print(f"[Warn] 未找到配置文件模板: {CONFIG_EXAMPLE}")


def get_python_exe():
    return _get_python_exe_impl()

def configured_account():
    """Bot account from config.json, used for NapCat quick login."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            account = str(json.load(f).get("self_account", "")).strip()
    except (OSError, ValueError):
        return ""
    return account if account.isdigit() else ""

def run_sync():
    try:
        from water_group.sync_groups import inspect_and_sync
        return inspect_and_sync()
    except Exception as e:
        print(f"[Warn] 同步群聊状态失败: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description="Water-Group 一键环境就绪与后台启动器")
    parser.add_argument("--group", "-g", help="指定目标 QQ 群号")
    parser.add_argument("--status-only", action="store_true", help="仅检查服务状态，不自动拉起")
    parser.add_argument("--allow-injected", action="store_true",
                        help="明知本进程带沙箱注入仍然起栈（会把 QQ 拖进崩溃循环，仅调试用）")
    args = parser.parse_args()

    # 从这里起的栈会把 tsbx.dll 一路继承到 QQ，二十秒后必崩。宁可现在就拒，
    # 也不要给群里留一个每 20 秒崩一次、还不断重注入的实例——那条路今天已经
    # 把桌面堆满过崩溃框。AGENTS.md §0 有正确入口。
    # --status-only 只探端口和健康检查，不拉起任何进程，放行。
    injected = sandbox_dll()
    if injected and not (args.allow_injected or args.status_only):
        print(f"[ABORT] 本进程带着 {injected}（代理沙箱注入）。")
        print("        从这里起的栈会把注入继承给 QQ：约 20 秒后崩 0xC06D007F，")
        print("        watchdog 会无限重注入，桌面上每个实例留一个崩溃框。")
        print("        改用 explorer 代启动：")
        print('          .venv/Scripts/python.exe -c "import subprocess,pathlib; '
              "subprocess.Popen(['explorer.exe', "
              "str(pathlib.Path('water-group/start-bot.pyw').resolve())])\"")
        print("        只查状态不受影响：bootstrap.py --status-only")
        print("        确需在当前进程里继续，加 --allow-injected。")
        return 2

    print("========================================================================")
    print("           [WATER-GROUP] 一键后台服务自检与自动启动")
    print("========================================================================")

    ensure_config()

    if args.status_only:
        napcat_ok = is_port_open(43000)
        tulpa_port_ok = is_port_open(48777) and is_port_open(47860)
        tulpa_ready, tulpa_msg = check_tulpa_mcp_health(48777) if tulpa_port_ok else (False, "端口未开放")
        tulpa_ok = tulpa_port_ok and tulpa_ready
    else:
        napcat_ok, napcat_msg = ensure_napcat()
        tulpa_ok, tulpa_msg = ensure_tulpa()

    print(f"1. NapCat OneBot  : {'[OK] 在线' if napcat_ok else '[FAIL] 未运行'} (HTTP: 43000, WS: 43001, WebUI: 46099)")
    print(f"2. Tulpa Backend  : {'[OK] ' + tulpa_msg if tulpa_ok else '[FAIL] 未就绪 (' + tulpa_msg + ')'} (Web: 47860, MCP: 48777)")

    if not (napcat_ok and tulpa_ok):
        print("\n[ERROR] 核心后台服务未完全就绪，请检查上述报错。")
        sys.exit(1)

    if args.status_only:
        # 真的只查状态：下面的群列表同步、人设卡装载和「MCP 极速启动指南」都属于
        # 接管流程，跑在这里既多余又会把 Agent 引向已经不用的 wait_chat_messages。
        return 0

    print("\n4. 正在同步群列表与角色卡...")
    sync_res = run_sync()

    print("\n5. 正在装载群友动态小本本记忆库...")
    try:
        from water_group.memory_engine import get_memory_engine
        mem_engine = get_memory_engine()
        mem_engine.seed_from_persona()
        mem_count = mem_engine.get_member_count()
        print(f"✓ 群友动态记忆库就绪 : 已建立 {mem_count} 位核心群友档案，支持小本本自动记录与检索")
    except Exception as e:
        print(f"[Warn] 加载群友记忆库异常: {e}")

    ready_groups = []
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                sdata = json.load(f)
                ready_groups = sdata.get("ready_groups", [])
        except Exception:
            pass

    target_sess = None
    if args.group:
        for g in ready_groups:
            if str(g.get("group_id")) == str(args.group):
                target_sess = g
                break
    elif ready_groups:
        target_sess = ready_groups[0]

    print("------------------------------------------------------------------------")
    if target_sess:
        gid = target_sess.get("group_id")
        gname = target_sess.get("group_name")
        sid = target_sess.get("session_id")
        persona_name = target_sess.get("persona")
        persona_preset = target_sess.get("persona_preset") or "little_whale"
        mode = target_sess.get("trigger_mode", "mention_only")
        reply_addressing = target_sess.get("reply_addressing", "quote")
        mention_only_bool = (mode == "mention_only")

        # 查找对应人设卡文件（私有卡在 ~/.water-group/personas，仓库只留公开模板）
        persona_file = None
        for cand in [
            personas_dir() / f"{persona_preset}.md",
            personas_dir() / f"{persona_name}.md",
            ROOT_DIR / "water-group" / "personas" / f"{persona_preset}.md",
            ROOT_DIR / "water-group" / "personas" / f"{persona_name}.md",
        ]:
            if cand.exists():
                persona_file = cand
                break

        persona_content = ""
        if persona_file and persona_file.exists():
            try:
                persona_content = persona_file.read_text(encoding="utf-8")
            except Exception:
                pass

        print(f"✓ 目标群聊      : {gid} ({gname})")
        print(f"✓ 生效 Session : {sid}")
        print(f"✓ 当前装载人设 : {persona_name} ({persona_preset})")
        if persona_file:
            try:
                shown = persona_file.relative_to(ROOT_DIR)
            except ValueError:
                shown = persona_file  # 私有卡在 ~/.water-group 下，不在仓库里
            print(f"✓ 人设卡路径   : {shown}")
        print(f"✓ 触发唤醒模式 : {mode} ({'仅被@或点名唤醒，大群防烧 Token' if mention_only_bool else '自然插话'})")
        print(f"✓ 回复寻址模式 : {reply_addressing} ({'首个气泡带 quote: true 开启 QQ 原生引用回复，精准定位原话' if reply_addressing == 'quote' else '普通回复'})")
        member_mappings = target_sess.get("member_mappings") or {}
        if member_mappings:
            annotated_summary = ", ".join(f"{v.get('name')}({k})" for k, v in member_mappings.items())
            print(f"✓ 核心群友标注 : {annotated_summary} (号码与名片已绑定，发言自动精准识别)")
        print("------------------------------------------------------------------------")
        if persona_content:
            print("【已加载人设卡核心内容 - 主脑必须严格以此设定进行思考与发言】：")
            print(persona_content.strip())
            print("------------------------------------------------------------------------")
        print("【Agent 极速启动行动指南 - 严禁进行任何多余探索，直接调用 MCP】：")
        print("  1. 必须牢记上述人设卡中的所有群友档案（花名、外号、梗、雷点）及说话风格，严禁凭空瞎猜胡编事实！")
        print("  2. 发起长轮询 wait_chat_messages 前向用户输出一句就绪提示")
        print("  3. 调用 MCP 工具 wait_chat_messages 挂起监听：")
        print(f"     MCP Server : \"tulpa\"")
        print(f"     MCP Tool   : \"wait_chat_messages\"")
        print(f"     Arguments  : {{")
        print(f"       \"session_id\": \"{sid}\",")
        print(f"       \"timeout_seconds\": 150,")
        print(f"       \"mention_only\": {str(mention_only_bool).lower()}")
        print(f"     }}")
        print("  4. 动态小本本记忆已挂载：发言群友的精简画像会自动出现在 chat_guidance.member_dossiers 中；群友说“记在小本本上”会自动入库；随时可调用 query_member_dossier 查询群友档案与专属八卦；回复时多结合对方外号与历史黑料调侃！")
        print("  5. QQ 原生引用回复：回复具体群友时，send_chat_reply 的首个文字气泡必须带上 \"quote\": true，自动开启 QQ 原生引用回复卡片精准定位原话！")
        print("  6. 群成员精准标注：核心群友号码与名片已强绑定，上下文画像已透传 QQ/名片，务必结合其专属外号与核心狠料精准怼人！群里发“@机器人 标注 <QQ/@> 是 <人名>”可秒级入库新群友！")
        print("------------------------------------------------------------------------")

        output_payload = {
            "status": "ready",
            "target_group": gid,
            "session_id": sid,
            "persona": persona_name,
            "persona_preset": persona_preset,
            "persona_file": str(shown) if persona_file else None,
            "trigger_mode": mode,
            "reply_addressing": reply_addressing,
            "member_mappings": member_mappings,
            "next_call": {
                "server": "tulpa",
                "tool": "wait_chat_messages",
                "arguments": {
                    "session_id": sid,
                    "timeout_seconds": 150,
                    "mention_only": mention_only_bool
                }
            }
        }
        print(f"\n<!-- BOOTSTRAP_RESULT: {json.dumps(output_payload, ensure_ascii=False)} -->")
    else:
        print("[Warn] 暂无就绪会话。请检查 config.json 中的 groups 配置与 QQ 加入的群。")
        sys.exit(1)

    print("========================================================================")

if __name__ == "__main__":
    sys.exit(main())
