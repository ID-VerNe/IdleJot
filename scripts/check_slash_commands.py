"""钉住 # 指令守护的纯逻辑：过滤、冷却、去重、帮助与消息段构造。

这些规则每条都对应一种群里真实会发生的双击式故障（自己发的 # 被当指令、
@机器人 #体力 双响应、未开群泄入、同一条消息重连后回两次），不能靠手点群消息
验证，所以在这里用合成事件全量钉住。

    .venv/Scripts/python.exe scripts/check_slash_commands.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import commands  # noqa: E402
import up  # noqa: E402  (唤醒侧的 `#` 闸门，issue #70)
import wg_up_wake  # noqa: E402  (issue #37 PC: config 的真实调用点；up.config 只是门面引用)
from check_fixture_support import temp_dir  # noqa: E402

# 守护的 log() 写死运维日志；检查进程不该往里混行，整体改道临时目录。
commands.LOG = Path(tempfile.gettempdir()) / "wg_commands_check.log"

FAILED = []
SELF = "100000000"
SELF_IDS = {SELF}
CFG = {
    "self_account": SELF,
    "groups": {
        "123456789": {"enabled": True},
        "987654321": {"enabled": False},
    },
    "slash_commands": {
        "enabled": True,
        "commands": {
            "ping": {"reply": "pong"},
            "空": {"desc": "空回复", "reply": ""},
        },
        "miyoushe": {"ck": [], "uid": ""},
    },
}
S = commands.settings_from(CFG)


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def event(**over):
    base = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 123456789,
        "user_id": 100000002,
        "message_id": 1001,
        "message": [{"type": "text", "data": {"text": "#体力"}}],
    }
    base.update(over)
    return base


def at_seg(qq):
    return {"type": "at", "data": {"qq": qq}}


# ---- settings_from：缺省与兜底 -------------------------------------------------
check("settings_from: 缺 slash_commands 段时默认关闭",
      commands.settings_from({"groups": {}})["enabled"] is False)
check("settings_from: 只收 enabled 群",
      commands.settings_from(CFG)["groups"] == {"123456789"})
check("settings_from: cooldown 负数回落默认值",
      commands.settings_from({"slash_commands": {"cooldown_seconds": -3}})["cooldown_seconds"] == 5)
check("settings_from: cooldown 非数字回落默认值",
      commands.settings_from({"slash_commands": {"cooldown_seconds": "abc"}})["cooldown_seconds"] == 5)

# ---- command_key：前缀解析 ----------------------------------------------------
check("command_key: '#体力' → ('体力','')", commands.command_key("#体力", "#") == ("体力", ""))
check("command_key: '#帮助 体力' → ('帮助','体力')",
      commands.command_key("#帮助 体力", "#") == ("帮助", "体力"))
check("command_key: '#  体力' 容忍前缀后空白", commands.command_key("#  体力", "#") == ("体力", ""))
check("command_key: 单独 '#' → None", commands.command_key("#", "#") is None)
check("command_key: 无前缀 → None", commands.command_key("体力", "#") is None)
check("command_key: 空前缀串 → None", commands.command_key("", "#") is None)
check("command_key: 自定义前缀 '!' 生效", commands.command_key("!ping", "!") == ("ping", ""))

# ---- 与唤醒闸门一致：command_key 是唯一契约（issue #70） ----------------------
_real_config = wg_up_wake.config
PREFIX_CASES = [
    ({}, "#"),
    ({"slash_commands": {}}, "#"),
    ({"slash_commands": {"prefix": ""}}, "#"),
    ({"slash_commands": {"prefix": None}}, "#"),
    ({"slash_commands": {"prefix": "!"}}, "!"),
    ({"slash_commands": {"prefix": "!!"}}, "!!"),
]
try:
    for cfg, want in PREFIX_CASES:
        wg_up_wake.config = lambda cfg=cfg: cfg
        check(f"command_prefix: {cfg} → {want!r}", up.command_prefix() == want)
        check(f"command_prefix 与 settings_from 一致: {cfg}",
              up.command_prefix() == commands.settings_from(cfg)["prefix"])
    TEXTS = ["#ping", "#帮助 体力", "#  体力", "#", "#   ", " #ping",
             "C#", "第#3名", "https://x/#frag", "!ping", ""]
    wg_up_wake.config = lambda: {"slash_commands": {"prefix": "#"}}
    for text in TEXTS:
        check(f"闸门一致: {text!r}",
              up.is_command(text) == (commands.command_key(text, "#") is not None))
    wg_up_wake.config = lambda: {"slash_commands": {"prefix": "!"}}
    for text in ("!ping", "#ping", "!!ping"):
        check(f"闸门一致(prefix=!): {text!r}",
              up.is_command(text) == (commands.command_key(text, "!") is not None))
finally:
    wg_up_wake.config = _real_config

# ---- plain_text：两种事件形状 -------------------------------------------------
check("plain_text: 数组段拼接",
      commands.plain_text(event(message=[at_seg("1"), {"type": "text", "data": {"text": "#体力"}}])) == "#体力")
check("plain_text: raw_message 剥 CQ 码",
      commands.plain_text({"raw_message": "[CQ:at,qq=1] #体力"}) == " #体力")

# ---- should_handle：四层过滤 --------------------------------------------------
check("should_handle: 正常 #指令 放行", commands.should_handle(event(), S, SELF_IDS))
check("should_handle: 自身回灌拦截",
      not commands.should_handle(event(user_id=SELF), S, SELF_IDS))
check("should_handle: @自己 的混合消息让给 Agent",
      not commands.should_handle(event(message=[at_seg(SELF), {"type": "text", "data": {"text": "#体力"}}]), S, SELF_IDS))
check("should_handle: @全体 的混合消息让给 Agent",
      not commands.should_handle(event(message=[at_seg("all"), {"type": "text", "data": {"text": "#体力"}}]), S, SELF_IDS))
check("should_handle: 未启用群拦截",
      not commands.should_handle(event(group_id=987654321), S, SELF_IDS))
check("should_handle: 私聊拦截", not commands.should_handle(event(message_type="private"), S, SELF_IDS))
check("should_handle: 非 message 事件拦截",
      not commands.should_handle(event(post_type="notice"), S, SELF_IDS))
check("should_handle: 无 # 前缀拦截",
      not commands.should_handle(event(message=[{"type": "text", "data": {"text": "体力看看"}}]), S, SELF_IDS))
lenient = dict(S, ignore_at_self=False)
check("should_handle: ignore_at_self=false 时 @自己 也处理",
      commands.should_handle(event(message=[at_seg(SELF), {"type": "text", "data": {"text": "#体力"}}]), lenient, SELF_IDS))

# ---- cooldown_ok：同群同关键词冷却 --------------------------------------------
last = {}
check("cooldown: 首次放行", commands.cooldown_ok(last, "g:k", 100.0, 5))
last["g:k"] = 100.0
check("cooldown: 窗口内拦截", not commands.cooldown_ok(last, "g:k", 104.0, 5))
check("cooldown: 到点放行", commands.cooldown_ok(last, "g:k", 105.0, 5))
check("cooldown: 别的关键词互不影响", commands.cooldown_ok(last, "g:other", 101.0, 5))
check("cooldown: seconds<=0 不限流", commands.cooldown_ok(last, "g:k", 100.5, 0))

# ---- mark_handled：有界去重 ----------------------------------------------------
state = commands.new_state()
check("mark_handled: 新 id 放行", commands.mark_handled(state, 1001))
check("mark_handled: 同 id 第二次拦截", not commands.mark_handled(state, 1001))
for mid in range(1002, 1002 + commands.SEEN_MAX + 5):
    commands.mark_handled(state, mid)
check("mark_handled: seen 集合不随窗口无限增长", len(state["seen"]) == commands.SEEN_MAX)
check("mark_handled: 超出窗口后最早的 id 可重新处理", commands.mark_handled(state, 1001))
check("mark_handled: 窗口内 id 仍被去重", not commands.mark_handled(state, 1002 + commands.SEEN_MAX))

# ---- match_handler：最长前缀切分（#米游社胡桃 粘手输入） -----------------------
check("match_handler: 整词命中", commands.match_handler("公告") == ("公告", ""))
check("match_handler: 粘手输入切前缀", commands.match_handler("米游社胡桃") == ("米游社", "胡桃"))
check("match_handler: 长词优先（绝区零体力不切成绝区零）",
      commands.match_handler("绝区零体力") == ("绝区零体力", ""))
check("match_handler: 原石带月份", commands.match_handler("原石10月") == ("原石", "10月"))
check("match_handler: 完全不认识的词不匹配", commands.match_handler("不存在词") == (None, ""))
_real_handlers = commands.HANDLERS
commands.HANDLERS = {}
try:
    check("match_handler: 无 handler 时全部落空", commands.match_handler("公告") == (None, ""))
finally:
    commands.HANDLERS = _real_handlers
# ASCII 键大小写不敏感（#KFC 的存在意义）：手机英文键盘常是小写
_ci_handlers = dict(commands.HANDLERS, KFC=("疯狂星期四", None))
commands.HANDLERS = _ci_handlers
try:
    check("match_handler: 小写命中大写键", commands.match_handler("kfc") == ("KFC", ""))
    check("match_handler: 大小写混写 + 粘手参数",
          commands.match_handler("Kfc今天") == ("KFC", "今天"))
    check("match_handler: 确切大小写仍优先", commands.match_handler("KFC") == ("KFC", ""))
finally:
    commands.HANDLERS = _real_handlers

# ---- resolve_reply / help_text -------------------------------------------------
check("resolve_reply: 帮助关键词走内置列表",
      "可用指令" in commands.resolve_reply("帮助", S))
check("resolve_reply: 已配置关键词", commands.resolve_reply("ping", S) == "pong")
check("resolve_reply: 未配置且 unknown_reply 为空 → None",
      commands.resolve_reply("不存在", S) is None)
noisy = dict(S, unknown_reply="没有这个指令")
check("resolve_reply: unknown_reply 有值时回复它",
      commands.resolve_reply("不存在", noisy) == "没有这个指令")
check("resolve_reply: 空 reply 条目不回复", commands.resolve_reply("空", S) is None)
check("resolve_reply: 动态 handler 优先于静态（#体力 无 UID 先提示用法，不联网）",
      "请带上游戏 UID" in commands.resolve_reply("体力", S, None, ""))
check("resolve_reply: #体力 带 UID 无 CK 时提示配置",
      "未配置米游社 CK" in commands.resolve_reply("体力", S, None, "100000000"))
text = commands.help_text(S)
check("help_text: 列出已配置关键词", "#ping" in text)
check("help_text: 动态 handler 也列出来", "#体力" in text and "#公告" in text)
check("help_text: 内置帮助关键词也列出来", "#帮助" in text)
check("help_text: 扫码登录指令始终列出", "#登录" in text and "#解绑" in text)
_real_handlers = commands.HANDLERS
commands.HANDLERS = {}
try:
    check("help_text: 无配置无 handler 时的兜底文案",
          commands.help_text(dict(S, commands={}, help_keywords=[])) == "暂无可用指令")
finally:
    commands.HANDLERS = _real_handlers

# ---- build_message：与 message_sender 同形状 -----------------------------------
segs = commands.build_message("pong", 55)
check("build_message: 引用段在前", segs[0] == {"type": "reply", "data": {"id": "55"}})
check("build_message: 文本段在后", segs[1] == {"type": "text", "data": {"text": "pong"}})
check("build_message: 无 quote 时只有文本段",
      commands.build_message("pong") == [{"type": "text", "data": {"text": "pong"}}])

# ---- serve() 守门：enabled=false 不得碰网络 ------------------------------------
original = commands.load_settings
commands.load_settings = lambda: commands.settings_from({})
try:
    code = commands.serve()
finally:
    commands.load_settings = original
check("serve: enabled=false 直接退出不联网", code == 0)

# ---- 单实例锁：两个守护同时在线会把每条 # 指令回两遍 ---------------------------
real_lock = commands.LOCK
with temp_dir() as tmp:
    commands.LOCK = tmp / "wg_commands.lock"
    try:
        check("lock: 首个实例拿到锁", commands.acquire_lock())
        check("lock: 持锁进程还活着，第二个实例被拒", not commands.acquire_lock())
        commands.release_lock()
        check("lock: 释放后可再拿", commands.acquire_lock())
        commands.release_lock()
        commands.LOCK.write_text("999999", encoding="utf-8")  # 早已死掉的 pid
        check("lock: 陈锁被接管", commands.acquire_lock())
        commands.release_lock()
        commands.LOCK.write_text("garbage", encoding="utf-8")  # 锁内容损坏
        check("lock: 损坏的锁按陈锁接管", commands.acquire_lock())
        commands.release_lock()
    finally:
        commands.LOCK = real_lock
check("pid_alive: 本进程判活", commands.pid_alive(os.getpid()))
check("pid_alive: 不存在的 pid 判死", not commands.pid_alive(999999))
check("pid_alive: 非法 pid 判死", not commands.pid_alive(0))

print()
if FAILED:
    print(f"{len(FAILED)} 项失败: {', '.join(FAILED)}")
    sys.exit(1)
print(f"全部通过（{commands.SEEN_MAX} 条去重窗口）")
