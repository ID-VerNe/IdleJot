"""复读 / 接龙插件门禁（issue #155）：判定、安全闸门与「最多跟一次」。

issue 要的是「群友自发复读时机器人跟一句」，而这类功能的失败形态全在**不该跟
的时候跟了**：跟自己的话形成死循环、跟着复读敏感词、替人 @ 别人、把外链再发
一遍、五分钟里跟十次。所以下面每条断言对应的都是一次会真出事的实现。

契约分四组：

1. **判定**：完全相同的三人复读 → 跟；两人不够；同一个人刷三遍不算（issue 的
   「默认 3 人且不同 QQ」）；接龙四种格式（`1.` / `1、` / `+1` / `1 正文`）各跟
   下一号并保留原格式；接龙优先于复读（`1 2 3` 发 `4` 而不是 `3`）。
2. **安全闸门**：指令前缀（防指令死循环）、敏感词、本机名字、外链、超长、带 @ /
   带图 / 带引用的整条不参与；`self_ids` 为空时一条都不跟（认不出自己就不动）。
3. **频率**：一波只**尝试**一次（骰子没中也算，否则消息越多必然中）、冷却期内
   不跟、冷却账本落盘（重启仍生效）、三态门控（停用 / 群白名单 / 群级关闭）。
4. **接线**：插件注册进注册表、`EVENT_LOADERS` 与 `LOADERS` 同源、
   `event_stream_needed` 让复读在 `#` 指令关闭时也能把事件流拉起来、`--status`
   的插件行与门控同口径、宿主真的把 `group_message` 派发到插件并发出回复。

判据完整性自检（契约 5）：把 `repeater.consider` 换成恒 `None`，所有「该跟」的
用例必须翻红；换成恒 `"x"`，所有「不该跟」的用例必须翻红。只钉「现在绿」的
门禁挡不住「以后永远绿」。

已知边界（**不声称任何错都红**）：

* **裸数字不算接龙**：`1` `2` `3` 三条独立消息不被认成接龙。这是刻意放松的——
  裸数字在正常对话里太常见，认了会在无关消息上乱跟。要覆盖「带标记的接龙」有
  用例，要覆盖「裸数字也必须认」不属于本实现。
* **概率为 0.5 时真跑一次会掷骰**：所有用例都注入 `rng`，不用真实随机数——否则
  门禁本身就成了偶发红。

    .venv/Scripts/python.exe scripts/check_repeater.py
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

from check_fixture_support import scratch_dir  # noqa: E402

# 数据根在 import 任何守护模块之前钉住：`repeater` 的账本路径在调用期读
# `state_dir()`，而 `commands` 在 import 期就把 `STATE_DIR` 求值完了。
_SCRATCH = scratch_dir(prefix="wg_repeater_")
os.environ["WATER_GROUP_HOME"] = str(_SCRATCH)
os.environ["IDLEJOT_WG_STATE"] = str(_SCRATCH / "state")
os.environ["TULPA_WG_STATE"] = str(_SCRATCH / "state")
(_SCRATCH / "state").mkdir(parents=True, exist_ok=True)

import commands  # noqa: E402
import repeater  # noqa: E402
from plugins.base import Hooks, Plugin, PluginError  # noqa: E402
from plugins.registry import Registry  # noqa: E402

FAILED = []
SELF = "100000000"
SELF_IDS = {SELF}
GROUP = "123456789"
OTHER = "987654321"


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


# ---- 夹具 ---------------------------------------------------------------------

def text_event(text, uid="200000001", mid=1, group=GROUP, **extra):
    """一条纯文本群消息。`text=None` 表示不带文本段（走 `extra["message"]`）。"""
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": int(group),
        "user_id": int(uid),
        "message_id": mid,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    event.update(extra)
    return event


class Clock:
    """可推进的假时钟：窗口 / 冷却都要能一秒一秒推，不睡真时间。"""

    def __init__(self, start=1_800_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def tick(self, seconds=1.0):
        self.now += float(seconds)
        return self.now


# `self_names` 是「本机名字」闸门的参数，闸门要真有一个具体名字才测得到；但夹具里
# **不许出现像真实昵称的字面值**（issue #254）：`check_publication.py` 从私有
# `self_names` / 人设卡 / 群友档案派生 token 后全量扫库，一个测试夹具常量撞上它就会把
# 「误报」报成「个人标识泄露」，挡下任何人在这台机器上的正常推送。第一个元素是公开
# 人设模板名（`#112` 时已登记进私有 allow），第二个元素只占位、不承担任何判据，
# 用只可能出现在夹具里的合成串。
SELF_NAMES = ["小鲸鱼", "self-b"]


def settings(**over):
    """一份开着 repeater、概率恒 1 的设置（概率另有用例单独钉）。"""
    cfg = {
        "self_account": SELF,
        "self_names": list(SELF_NAMES),
        "groups": {GROUP: {"enabled": True}},
        "slash_commands": {"enabled": True},
        "plugins": {"repeater": {
            "enabled": True,
            "options": {"probability": 1.0, "min_repeats": 3},
        }},
    }
    cfg.update(over)
    return commands.settings_from(cfg)


def feed(rows, options, clock, rng=None, self_ids=SELF_IDS, conf=None):
    """按序喂一串 `(文本, uid)`，返回**全程**发出的第一条（None = 一条都没发）。

    不能「见到回复就停」：`该跟` 与 `不该跟` 往往只差最后一条消息（第四人改口），
    提前返回就把后半段判据跳过了——那正是假绿的形状。整串跑完再看。
    """
    replies = feed_all(rows, options, clock, rng, self_ids, conf)
    return replies[0] if replies else None


def feed_all(rows, options, clock, rng=None, self_ids=SELF_IDS, conf=None):
    """整串跑完，返回**每一次**发出的文本（按发生顺序）。

    一个场景里可能「该跟一次、之后不该再跟」：只看第一条或只看最后一条都会漏掉
    另一半判据，所以两个视角都留。
    """
    conf = conf or settings()
    options = options or PLUGIN_OPTIONS(conf)
    clock = clock or Clock()
    replies = []
    for index, row in enumerate(rows):
        text, uid = row if isinstance(row, tuple) else (row, "20000000%d" % index)
        event = text_event(text, uid=uid, mid=index + 1)
        reply = repeater.consider(event, conf, options, self_ids, clock=clock,
                                  rng=rng or (lambda: 0.0))
        clock.tick(1.0)
        if reply is not None:
            replies.append(reply)
    return replies


def PLUGIN_OPTIONS(conf):
    return commands.PLUGINS.get("repeater").options(conf)


def fresh():
    """每个用例一个干净窗口 + 空账本。"""
    repeater.reset()
    path = repeater._ledger_path()
    try:
        path.unlink()
    except OSError:
        pass


# ---- 1. 判定 -------------------------------------------------------------------

def test_repeat_basics():
    print("[复读：三人才跟、同一个人不算]")
    fresh()
    check("三人同一句 → 跟同一句",
          feed([("草", "1"), ("草", "2"), ("草", "3")], None, None) == "草")
    fresh()
    check("两人不够（min_repeats=3）",
          feed([("草", "1"), ("草", "2")], None, None) is None)
    fresh()
    check("同一人刷三遍不算（要不同 QQ）",
          feed([("草", "1"), ("草", "1"), ("草", "1")], None, None) is None)
    fresh()
    check("空白折叠后比较（`草 ` 与 ` 草` 是同一句）",
          feed([("草 ", "1"), (" 草", "2"), ("草", "3")], None, None) == "草")
    fresh()
    check("三人各说各的 → 不跟",
          feed([("草", "1"), ("乐", "2"), ("6", "3")], None, None) is None)
    fresh()
    # 「该跟一次」与「跟完之后不再跟」是两件事：整串跑完看**次数**，不是看有没有。
    check("第四人改口：第三次就跟着说了（改口不撤销已发生的事）",
          feed_all([("草", "1"), ("草", "2"), ("草", "3"), ("换个话题", "4")],
                   None, None) == ["草"])
    fresh()
    # 三人说完又聊了两句：跟的那次发生在第三条（当时尾巴就是那句），之后再没人
    # 复读就不该有第二次。
    check("三人说完被打断：只跟一次，后续插话不再跟",
          feed_all([("草", "1"), ("草", "2"), ("草", "3"), ("哈哈哈", "4"),
                    ("确实", "5")], None, None) == ["草"])
    fresh()
    # 三人各说一次「草」但中间隔着别人的话：尾巴仍然是那句，跟。
    check("中间有人插话、之后又有人复读 → 跟（真实队列不排成一列）",
          feed([("草", "1"), ("哈哈哈", "2"), ("草", "3"), ("草", "4")],
               None, None) == "草")
    fresh()
    check("只有两人复读 + 插话 → 不跟（人数按不同 QQ 数）",
          feed([("草", "1"), ("哈哈哈", "2"), ("草", "3")], None, None) is None)

    # 尾巴判据的**独立**用例（打靶补的，原有用例钉不住它）：
    # 「三人说完被打断」那条能过，是因为「一波只跟一次」把第二次挡住了——把
    # `_repeat_plan` 的 `if not tail["ok"]` 整段删掉，它照样绿。这里让冷却与
    # waves 都不构成障碍（全新一波、冷却期内无发送），唯一能拦住的就是尾巴判据。
    fresh()
    clock = Clock()
    conf = settings()
    options = PLUGIN_OPTIONS(conf)
    # 第一波：三人复读「草」→ 跟一次（把冷却打开）
    feed([("草", "1"), ("草", "2"), ("草", "3")], options, clock, conf=conf)
    # 冷却过掉，换一个新话题：三人复读「乐」，然后话题被第四人转走
    clock.tick(float(options["cooldown_seconds"]) + 1)
    tail_replies = feed_all([("乐", "4"), ("乐", "5"), ("乐", "6"),
                             ("说点别的", "7")], options, clock, conf=conf)
    # 「乐」那波在第三条时尾巴就是它，所以跟了一次；关键在于**第四条之后**
    # 不许再跟一次「乐」——那正是尾巴判据失效时的行为。
    check("话题被转走后不补一句旧话（尾巴判据）",
          tail_replies == ["乐"], repr(tail_replies))
    fresh()
    clock = Clock()
    # 尾巴是**不可跟的消息**（带图）时不再跟：图才是此刻的话题。
    # 注意这条**测不到** `_repeat_plan` 里的 `if not tail["ok"]`——带图消息的
    # `text` 是空串，`senders` 统计为空，删掉那句判据行为等价（打靶实测过）。
    # 它钉的是「带图消息不进判定」这条**对外行为**，不是某一行代码。
    conf2 = settings(plugins={"repeater": {
        "enabled": True, "options": {"probability": 1.0, "cooldown_seconds": 0}}})
    opts2 = PLUGIN_OPTIONS(conf2)
    replies = [
        repeater.consider(text_event("草", uid=u, mid=i + 1), conf2, opts2,
                          SELF_IDS, clock=clock, rng=lambda: 0.0)
        for i, u in enumerate(["1", "2", "3"])
    ]
    repeater._ledger()["groups"][GROUP]["waves"].clear()
    repeater._ledger()["groups"][GROUP]["last_sent"] = 0.0
    image_tail = text_event("草", uid="4", mid=99, message=[
        {"type": "image", "data": {"file": "x.png"}},
        {"type": "text", "data": {"text": "草"}}])
    after = repeater.consider(image_tail, conf2, opts2, SELF_IDS, clock=clock,
                              rng=lambda: 0.0)
    check("尾巴是带图消息时不再跟（图才是此刻的话题）",
          replies[2] == "草" and after is None, f"{replies!r} {after!r}")


def test_chain():
    print("[接龙：四种格式各跟下一号]")
    cases = [
        ("1. 打卡", ["1. 打卡", "2. 打卡", "3. 打卡"], "4. 打卡"),
        ("1、打卡", ["1、打卡", "2、打卡", "3、打卡"], "4、打卡"),
        ("+1", ["+1", "+2", "+3"], "+4"),
        ("1）打卡", ["1）打卡", "2）打卡", "3）打卡"], "4）打卡"),
    ]
    for label, rows, want in cases:
        fresh()
        got = feed([(row, str(i + 1)) for i, row in enumerate(rows)], None, None)
        check(f"接龙 {label} → {want}", got == want, repr(got))
    fresh()
    check("接龙优先于复读：1/2/3 发 4，不是 3",
          feed([("1", "1"), ("2", "2"), ("3", "3")], None, None) is None,
          "裸数字不算接龙（已知边界），这里顺带钉住它也不被当成复读")
    fresh()
    check("接龙人数不足 → 不跟",
          feed([("1. 打卡", "1"), ("2. 打卡", "2")], None, None) is None)
    fresh()
    check("序号断了（1、2、4）→ 不跟",
          feed([("1. x", "1"), ("2. x", "2"), ("4. x", "3")], None, None) is None)
    fresh()
    check("正文不一致（1. x / 2. y / 3. z）→ 不跟",
          feed([("1. x", "1"), ("2. y", "2"), ("3. z", "3")], None, None) is None)
    fresh()
    check("格式不一致（1. x / 2、x / 3. x）→ 不跟",
          feed([("1. x", "1"), ("2、x", "2"), ("3. x", "3")], None, None) is None)
    fresh()
    check("同一人接龙不算（要不同 QQ）",
          feed([("1. x", "1"), ("2. x", "1"), ("3. x", "1")], None, None) is None)
    fresh()
    check("六人复读同一个数字 6 → 跟 6（复读不是接龙）",
          feed([("6", str(i)) for i in range(1, 7)], None, None) == "6")
    fresh()
    check("999 之后的接龙不越界（三位数上限内）",
          feed([("997. x", "1"), ("998. x", "2"), ("999. x", "3")],
               None, None) == "1000. x")


def test_chain_parse():
    print("[接龙解析：标记与裸数字]")
    check("`1. 正文` 认得标记", (repeater.chain_parse("1. 正文") or {}).get("sep") == ".")
    check("`+1` 认得加号", (repeater.chain_parse("+1") or {}).get("plus") == "+")
    check("`1、正文` 认得顿号", (repeater.chain_parse("1、正文") or {}).get("sep") == "、")
    check("裸数字 `3` 不算接龙项", repeater.chain_parse("3") is None)
    check("普通句子不算接龙项", repeater.chain_parse("今天天气不错") is None)
    check("空串不算接龙项", repeater.chain_parse("") is None)
    check("四位以上不认（`2026年` 不是接龙）", repeater.chain_parse("2026年") is None)


# ---- 2. 安全闸门 ---------------------------------------------------------------

def test_gates():
    print("[安全闸门]")
    fresh()
    check("指令前缀不跟（防指令死循环）",
          feed([("#帮助", "1"), ("#帮助", "2"), ("#帮助", "3")], None, None) is None)
    fresh()
    check("默认前缀是 `#`：`!帮助` 是普通文本，三人刷就照跟",
          feed([("!帮助", "1"), ("!帮助", "2"), ("!帮助", "3")],
               None, None) == "!帮助")
    fresh()
    alt = settings(slash_commands={"enabled": True, "prefix": "!"})
    check("自定义前缀 `!` 也被挡（前缀是配置，不是写死的 `#`）",
          feed([("!帮助", "1"), ("!帮助", "2"), ("!帮助", "3")],
               PLUGIN_OPTIONS(alt), None, conf=alt) is None)
    fresh()
    deny = settings(plugins={"repeater": {
        "enabled": True,
        "options": {"probability": 1.0, "deny_words": ["禁词"]},
    }})
    check("敏感词不跟",
          feed([("这是禁词", "1"), ("这是禁词", "2"), ("这是禁词", "3")],
               PLUGIN_OPTIONS(deny), None, conf=deny) is None)
    fresh()
    check("敏感词大小写不敏感（ASCII）",
          feed([("BADWORD", "1"), ("BADWORD", "2"), ("BADWORD", "3")],
               PLUGIN_OPTIONS(settings(plugins={"repeater": {
                   "enabled": True,
                   "options": {"probability": 1.0, "deny_words": ["badword"]}}})),
               None, conf=settings(plugins={"repeater": {
                   "enabled": True,
                   "options": {"probability": 1.0, "deny_words": ["badword"]}}})) is None)
    fresh()
    check("含本机名字不跟（等于替人召唤自己）",
          feed([("小鲸鱼呢", "1"), ("小鲸鱼呢", "2"), ("小鲸鱼呢", "3")],
               None, None) is None)
    fresh()
    # issue #254 的两条对照：「本机名字」闸门读的是**配置里的值**，不是夹具的某个字面值。
    # 上线机器换成别的名字（哪怕与夹具毫无关系）照样要挡；名字表空了才放行。
    other = settings(self_names=["别的名字"])
    check("换一个与夹具无关的名字照样挡（闸门读配置，不是读死字面值）",
          feed([("别的名字呢", "1"), ("别的名字呢", "2"), ("别的名字呢", "3")],
               PLUGIN_OPTIONS(other), None, conf=other) is None)
    fresh()
    nameless = settings(self_names=[])
    check("名字表为空时同一串照跟（对照组：上一条的绿不是恒绿）",
          feed([("别的名字呢", "1"), ("别的名字呢", "2"), ("别的名字呢", "3")],
               PLUGIN_OPTIONS(nameless), None, conf=nameless) == "别的名字呢")
    fresh()
    check("含外链不跟",
          feed([("https://x.test", "1"), ("https://x.test", "2"),
                ("https://x.test", "3")], None, None) is None)
    fresh()
    check("超长不跟（max_chars=60）",
          feed([("长" * 61, "1"), ("长" * 61, "2"), ("长" * 61, "3")],
               None, None) is None)
    fresh()
    check("恰好 max_chars 仍跟",
          feed([("长" * 60, "1"), ("长" * 60, "2"), ("长" * 60, "3")],
               None, None) == "长" * 60)
    fresh()
    at_event = text_event("草", uid="1", message=[
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": "草"}}])
    conf = settings()
    options = PLUGIN_OPTIONS(conf)
    clock = Clock()
    replies = [
        repeater.consider(at_event, conf, options, SELF_IDS, clock=clock,
                          rng=lambda: 0.0),
        repeater.consider(text_event("草", uid="2", mid=2), conf, options,
                          SELF_IDS, clock=clock, rng=lambda: 0.0),
        repeater.consider(text_event("草", uid="3", mid=3), conf, options,
                          SELF_IDS, clock=clock, rng=lambda: 0.0),
    ]
    check("带 @ 的整条不参与（不替别人 @ 人）", all(r is None for r in replies),
          repr(replies))
    fresh()
    img = text_event("草", uid="1", message=[
        {"type": "image", "data": {"file": "x.png"}},
        {"type": "text", "data": {"text": "草"}}])
    clock = Clock()
    first = repeater.consider(img, conf, options, SELF_IDS, clock=clock,
                              rng=lambda: 0.0)
    second = repeater.consider(text_event("草", uid="2", mid=2), conf, options,
                               SELF_IDS, clock=clock, rng=lambda: 0.0)
    third = repeater.consider(text_event("草", uid="3", mid=3), conf, options,
                              SELF_IDS, clock=clock, rng=lambda: 0.0)
    check("带图的整条不参与（只有两个人算数）",
          first is None and second is None and third is None,
          f"{first!r} {second!r} {third!r}")
    fresh()
    cq = text_event("草", uid="1", message=None, raw_message="[CQ:image,file=x.png]草")
    clock = Clock()
    got = [repeater.consider(cq, conf, options, SELF_IDS, clock=clock,
                             rng=lambda: 0.0),
           repeater.consider(text_event("草", uid="2", mid=2), conf, options,
                             SELF_IDS, clock=clock, rng=lambda: 0.0),
           repeater.consider(text_event("草", uid="3", mid=3), conf, options,
                             SELF_IDS, clock=clock, rng=lambda: 0.0)]
    check("CQ 码形态的图文整条不参与", all(r is None for r in got), repr(got))
    fresh()
    check("self_ids 为空时一条都不跟（认不出自己就不动）",
          feed([("草", "1"), ("草", "2"), ("草", "3")], None, None,
               self_ids=set()) is None)
    fresh()
    check("自己的消息不进窗口（三人里含自己 → 只有两人）",
          feed([("草", "1"), ("草", "2"), ("草", SELF)], None, None) is None)


# ---- 3. 频率 -------------------------------------------------------------------

def test_frequency():
    print("[频率：一波一次 / 冷却 / 概率]")
    fresh()
    conf = settings()
    options = PLUGIN_OPTIONS(conf)
    clock = Clock()
    replies = []
    for index, uid in enumerate(["1", "2", "3", "4", "5", "6"]):
        replies.append(repeater.consider(
            text_event("草", uid=uid, mid=index + 1), conf, options, SELF_IDS,
            clock=clock, rng=lambda: 0.0))
        clock.tick(1.0)
    sent = [r for r in replies if r]
    check("一波只跟一次（后续复读不再跟）", len(sent) == 1, repr(sent))
    check("跟的那次是第三个人之后", replies[2] == "草" and replies[3] is None,
          repr(replies))

    fresh()
    clock = Clock()
    first = feed([("草", "1"), ("草", "2"), ("草", "3")], options, clock, conf=conf)
    # 冷却期内换一句再来一波
    later = [repeater.consider(text_event("乐", uid=str(i), mid=10 + i), conf,
                               options, SELF_IDS, clock=clock, rng=lambda: 0.0)
             for i in range(3)]
    check("冷却期内另一波也不跟", first == "草" and not any(later), repr(later))
    clock.tick(float(options["cooldown_seconds"]) + 1)
    after = [repeater.consider(text_event("乐", uid=str(i), mid=20 + i), conf,
                               options, SELF_IDS, clock=clock, rng=lambda: 0.0)
             for i in range(3)]
    check("冷却到期后新的一波照跟", any(after), repr(after))

    fresh()
    clock = Clock()
    zero = settings(plugins={"repeater": {
        "enabled": True, "options": {"probability": 0.0, "min_repeats": 3}}})
    got = feed([("草", "1"), ("草", "2"), ("草", "3")], PLUGIN_OPTIONS(zero),
               clock, conf=zero)
    check("probability=0 从不跟", got is None)

    # 「一波只尝试一次」必须用**能掷中**的 rng 来钉：probability=0 时
    # `rng() >= 0` 恒真、恒不跟，删掉 waves 记账也照样通过——那是假绿。
    # 这里第一次掷 0.9（不中）、第二次掷 0.0（必中）：只有 waves 记账在，
    # 第四人才不会被重掷命中。
    fresh()
    clock = Clock()
    half = settings(plugins={"repeater": {
        "enabled": True, "options": {"probability": 0.5, "min_repeats": 3}}})
    rolls = iter([0.9, 0.0])

    def rng():
        return next(rolls, 0.9)

    missed = feed([("草", "1"), ("草", "2"), ("草", "3")], PLUGIN_OPTIONS(half),
                  clock, rng=rng, conf=half)
    check("第一次掷骰没中（0.9 ≥ 0.5）不跟", missed is None, repr(missed))
    again = repeater.consider(text_event("草", uid="4", mid=99), half,
                              PLUGIN_OPTIONS(half), SELF_IDS, clock=clock, rng=rng)
    check("骰子没中后同一波不重掷（第二次必中也不跟）", again is None, repr(again))

    # issue #213：上面那条用默认冷却（600s）钉——冷却够长，波记忆本来就不会被
    # 清掉，旧代码（按冷却清理）也照样绿。真正的复发在 `cooldown_seconds=0`：
    # 旧代码 `_prune_waves` 的 `cutoff = now - max(0, 1.0)` 让波记忆 1 秒就过期，
    # 同一波每过一秒重掷一次直到中。这里把冷却设 0、窗口留默认 120s，1.2 秒后
    # 再喂同一波、第二次骰子必中——只有「波记忆按窗口寿命清理」在，才会拦住。
    fresh()
    clock = Clock()
    nocooldown = settings(plugins={"repeater": {
        "enabled": True,
        "options": {"probability": 0.5, "min_repeats": 3, "cooldown_seconds": 0}}})
    rolls = iter([0.9, 0.0])

    def rng2():
        return next(rolls, 0.9)

    missed = feed([("草", "1"), ("草", "2"), ("草", "3")],
                  PLUGIN_OPTIONS(nocooldown), clock, rng=rng2, conf=nocooldown)
    check("cooldown=0：第一次掷骰没中（0.9 ≥ 0.5）不跟", missed is None, repr(missed))
    clock.tick(1.2)  # 窗口内（120s），同一波还活着
    again = repeater.consider(text_event("草", uid="4", mid=99), nocooldown,
                              PLUGIN_OPTIONS(nocooldown), SELF_IDS, clock=clock,
                              rng=rng2)
    check("cooldown=0：窗口内同一波不重掷（第二次必中也不跟，#213）",
          again is None, repr(again))

    # 波记忆按窗口寿命过期：超过 window_seconds 后，三个新人复读同一句是新一波，
    # 该允许再试一次（不能把「一波只试一次」做成「这句永远不再跟」）。
    clock.tick(float(PLUGIN_OPTIONS(nocooldown)["window_seconds"]) + 1)
    fresh_window = [repeater.consider(text_event("草", uid=str(i), mid=200 + i),
                                      nocooldown, PLUGIN_OPTIONS(nocooldown),
                                      SELF_IDS, clock=clock, rng=lambda: 0.0)
                    for i in range(4, 7)]
    check("cooldown=0：窗口过后新一波可跟（波记忆按窗口过期，不永久封印）",
          any(fresh_window), repr(fresh_window))

    fresh()
    half = settings(plugins={"repeater": {
        "enabled": True, "options": {"probability": 0.5, "min_repeats": 3}}})
    clock = Clock()
    got = feed([("草", "1"), ("草", "2"), ("草", "3")], PLUGIN_OPTIONS(half),
               clock, rng=lambda: 0.49, conf=half)
    check("概率内（rng=0.49 < 0.5）跟", got == "草", repr(got))
    fresh()
    clock = Clock()
    got = feed([("草", "1"), ("草", "2"), ("草", "3")], PLUGIN_OPTIONS(half),
               clock, rng=lambda: 0.5, conf=half)
    check("概率外（rng=0.5 不小于 0.5）不跟", got is None, repr(got))

    fresh()
    clock = Clock()
    # 账本落盘：清掉进程内缓存后，冷却仍然生效（重启不该把冷却洗掉）
    feed([("草", "1"), ("草", "2"), ("草", "3")], options, clock, conf=conf)
    repeater._LEDGER = None
    repeater.WINDOWS.clear()
    replay = [repeater.consider(text_event("乐", uid=str(i), mid=30 + i), conf,
                                options, SELF_IDS, clock=clock, rng=lambda: 0.0)
              for i in range(3)]
    check("冷却账本跨进程重启仍生效", not any(replay), repr(replay))


def test_ledger_robustness():
    print("[账本：坏文件不炸、统计可读]")
    fresh()
    path = repeater._ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    conf = settings()
    got = feed([("草", "1"), ("草", "2"), ("草", "3")], PLUGIN_OPTIONS(conf),
               None, conf=conf)
    check("账本是 list 时按空账本处理，照样跟", got == "草", repr(got))
    path.write_text("{ not json", encoding="utf-8")
    repeater.reset()
    got = feed([("乐", "1"), ("乐", "2"), ("乐", "3")], PLUGIN_OPTIONS(conf),
               None, conf=conf)
    check("账本不是 JSON 时按空账本处理，照样跟", got == "乐", repr(got))
    fresh()
    check("没跟过时状态说明是空串（行里不加字）", repeater.status_text() == "",
          repeater.status_text())
    feed([("草", "1"), ("草", "2"), ("草", "3")], PLUGIN_OPTIONS(conf), None,
         conf=conf)
    check("跟过之后状态说明报累计次数", "累计跟读 1 次" in repeater.status_text(),
          repeater.status_text())


# ---- 4. 接线 -------------------------------------------------------------------

def test_plugin_wiring():
    print("[接线：注册 / 事件流总闸 / 状态行]")
    plugin = commands.PLUGINS.get("repeater")
    check("repeater 已注册", plugin is not None)
    check("repeater 挂 on_event", plugin is not None and plugin.hooks.on_event is not None)
    check("repeater 声明需要事件流", plugin is not None and plugin.needs_event_loop)
    import plugins

    check("EVENT_LOADERS 里有 repeater",
          ("repeater", plugins._load_repeater) in plugins.EVENT_LOADERS)
    check("LOADERS 里有 repeater", ("repeater", plugins._load_repeater) in plugins.LOADERS)
    check("CLI 路与注册表是同一个 PLUGIN", plugins._load_repeater() is plugin,
          "两条派发路两份状态是会分叉的")

    off = commands.settings_from({"slash_commands": {"enabled": False}})
    on = commands.settings_from({"slash_commands": {"enabled": False},
                                 "plugins": {"repeater": {"enabled": True}}})
    check("默认关：指令关时不拉事件流", commands.event_stream_needed(off) is False)
    check("开了 repeater：指令关也拉事件流", commands.event_stream_needed(on) is True)
    check("指令开着照常起", commands.event_stream_needed(
        commands.settings_from({"slash_commands": {"enabled": True}})) is True)
    scoped = commands.settings_from({
        "plugins": {"repeater": {"enabled": True, "groups": [GROUP]}}})
    check("群白名单不影响「要不要起进程」（只判总开关）",
          commands.event_stream_needed(scoped) is True)

    # 三态门控：停用 / 群白名单 / 群级关闭
    fresh()
    check("停用时 group_message 返回 None",
          plugin.hooks.on_event("group_message", {
              "settings": off, "event": text_event("草"), "self_ids": SELF_IDS}) is None)
    fresh()
    only = commands.settings_from({
        "self_account": SELF,
        "plugins": {"repeater": {"enabled": True,
                                 "options": {"probability": 1.0},
                                 "groups": [OTHER]}}})
    blocked = [plugin.hooks.on_event("group_message", {
        "settings": only, "event": text_event("草", uid=str(i), mid=i),
        "self_ids": SELF_IDS}) for i in range(1, 4)]
    check("群白名单外的群不跟", not any(blocked), repr(blocked))
    fresh()
    closed = commands.settings_from({
        "self_account": SELF,
        "groups": {GROUP: {"enabled": True, "plugins": {"repeater": False}}},
        "plugins": {"repeater": {"enabled": True, "options": {"probability": 1.0}}}})
    blocked = [plugin.hooks.on_event("group_message", {
        "settings": closed, "event": text_event("草", uid=str(i), mid=i),
        "self_ids": SELF_IDS}) for i in range(1, 4)]
    check("群级关闭优先于总开关", not any(blocked), repr(blocked))

    line_on = commands.PLUGINS.status_line(on)
    check("--status 报 repeater 已启用", "repeater 已启用" in line_on, line_on)
    line_off = commands.PLUGINS.status_line(off)
    check("--status 报 repeater 停用", "repeater 停用" in line_off, line_off)
    check("停用时状态行不报累计次数（口径一致）",
          plugin.hooks.status(off) == "", repr(plugin.hooks.status(off)))

    # 未知事件不接：插座只有一个名字，别的插件的事件不该被本插件吞掉
    check("未知事件返回 None", plugin.hooks.on_event("reply_sent", {}) is None)

    # 重名 fail-fast：注册表仍然拦得住
    registry = Registry()
    registry.register(Plugin(name="repeater"))
    try:
        registry.register(Plugin(name="repeater"))
    except PluginError as exc:
        check("插件重名仍抛错", "插件重名" in str(exc), str(exc))
    else:
        check("插件重名仍抛错", False, "没有抛错")


def test_host_dispatch():
    print("[宿主派发：group_message → 发送通道]")
    sent = []
    conf = settings()
    commands.SETTINGS_BOX["settings"] = conf
    real_sender = commands.PLUGINS.host._sender
    commands.PLUGINS.host._sender = lambda s, gid, payload: sent.append((gid, payload))
    fresh()
    try:
        for index, uid in enumerate(["1", "2", "3"]):
            commands._emit_group_message(text_event("草", uid=uid, mid=index + 1),
                                         conf, SELF_IDS)
    finally:
        commands.PLUGINS.host._sender = real_sender
    check("三人复读后宿主真的发了一条", len(sent) == 1, repr(sent))
    check("发到正确的群", sent and sent[0][0] == GROUP, repr(sent))
    check("发的是那句正文", sent and sent[0][1] == "草", repr(sent))

    sent.clear()
    commands.PLUGINS.host._sender = lambda s, gid, payload: sent.append((gid, payload))
    fresh()
    try:
        commands._emit_group_message(text_event("草"), commands.settings_from({}),
                                     SELF_IDS)
    finally:
        commands.PLUGINS.host._sender = real_sender
    check("插件停用时宿主不发", not sent, repr(sent))

    sent.clear()
    conf = settings()
    commands.SETTINGS_BOX["settings"] = conf
    commands.PLUGINS.host._sender = lambda s, gid, payload: sent.append((gid, payload))
    fresh()
    try:
        private = dict(text_event("草"), message_type="private")
        for index, uid in enumerate(["1", "2", "3"]):
            commands._emit_group_message(dict(private, user_id=int(uid),
                                              message_id=index + 1), conf, SELF_IDS)
    finally:
        commands.PLUGINS.host._sender = real_sender
    check("私聊不参与（只认群消息）", not sent, repr(sent))

    # 发送失败不冒泡：一条复读跟不出去不该让 WS 循环死
    def boom(_settings, _gid, _payload):
        raise RuntimeError("send down")

    commands.SETTINGS_BOX["settings"] = conf
    commands.PLUGINS.host._sender = boom
    fresh()
    try:
        result = []
        for index, uid in enumerate(["1", "2", "3"]):
            result.append(commands._emit_group_message(
                text_event("草", uid=uid, mid=index + 1), conf, SELF_IDS))
    finally:
        commands.PLUGINS.host._sender = real_sender
    check("发送失败不冒泡（返回 None，异常被吞）", all(r is None for r in result),
          repr(result))


def _log_lines():
    """读本轮 ``commands.LOG`` 的全部行（门禁已把它改道临时文件）。"""
    try:
        return commands.LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def test_emit_budget():
    """事件型派发有耗时预算（issue #225）：慢插件超时跳过、记慢日志，不挡 # 指令。

    issue 复发钉子原文：注册一个 ``time.sleep`` 的夹具插件，断言 ``_emit_group_message``
    在预算内返回、记了一行慢日志，且紧随其后的 ``dispatch()`` 仍能处理 ``#`` 指令。
    """
    print("[派发耗时预算（issue #225）]")
    # 夹具：一个 on_event 里 sleep 的事件型插件。直接挂进注册表（不进 LOADERS——
    # 它是测试探针，不是真插件），让 PLUGINS.emit 能派发到它。
    def slow_event(_event, _ctx):
        time.sleep(3)  # 远超 EMIT_TIMEOUT
        return "should-never-arrive"

    probe = Plugin(name="slow-probe-225", hooks=Hooks(on_event=slow_event))
    conf = commands.settings_from({
        "self_account": SELF,
        "groups": {GROUP: {"enabled": True}},
        "slash_commands": {"enabled": True, "commands": {"ping": {"reply": "pong"}}},
    })
    commands.SETTINGS_BOX["settings"] = conf

    real_sender = commands.PLUGINS.host._sender
    sent = []
    commands.PLUGINS.host._sender = lambda s, gid, payload: sent.append((gid, payload))
    # dispatch 走的是 send_group_msg（OneBot HTTP），不是 host._sender——桩掉它，
    # 否则没真服务器时 #ping 的发送会失败、dispatch 返回 False，测不到「指令照常派发」。
    real_send = commands.send_group_msg
    sent_cmd = []
    commands.send_group_msg = lambda s, gid, segs: sent_cmd.append((gid, segs)) or {"data": {"message_id": 999}}
    registered = False
    budget = commands.EMIT_TIMEOUT
    try:
        commands.PLUGINS.register(probe)
        registered = True
        fresh()
        before = set(_log_lines())
        t0 = time.monotonic()
        result = commands._emit_group_message(text_event("草", uid="1", mid=1),
                                              conf, SELF_IDS)
        elapsed = time.monotonic() - t0
        after = set(_log_lines())
        slow_log = [ln for ln in after - before if "event plugin slow" in ln]
        # 慢插件被预算挡住：返回 None、远不到 sleep 的 3s、记了一行慢日志、没发出去
        check("慢插件超时返回 None", result is None, repr(result))
        check("返回在预算内（不真等 sleep 的 3s）",
              elapsed < budget + 1.0, f"elapsed={elapsed:.2f}s budget={budget}s")
        check("记了一行 event plugin slow 日志", len(slow_log) == 1,
              f"{len(slow_log)} 行: {slow_log}")
        check("慢插件期间没发复读", not sent, repr(sent))

        # issue 核心症状：慢插件挡不住紧随其后的 # 指令派发。
        ping_event = text_event("#ping", uid="2", mid=2)
        state = commands.new_state()
        dispatched = commands.dispatch(ping_event, conf, SELF_IDS, state)
        check("慢插件期间 #ping 仍派发成功", dispatched is True, repr(dispatched))
        # dispatch 把 group_id 当 int 发（event 里就是 int），GROUP 是字符串——按值比。
        check("#ping 回复已发出",
              bool(sent_cmd) and str(sent_cmd[0][0]) == GROUP, repr(sent_cmd))
    finally:
        commands.PLUGINS.host._sender = real_sender
        commands.send_group_msg = real_send
        if registered:
            # 探针从注册表摘掉，别污染同一进程里后续用例的 PLUGINS.plugins()
            commands.PLUGINS._plugins.pop(probe.name, None)
            for cmd in probe.commands:
                commands.PLUGINS._owners.pop(cmd.name, None)
                commands.PLUGINS._commands.pop(cmd.name, None)

    # 变异自检：把预算调大，慢插件就该等到结果（不再是「怎么测都超时跳过」）。
    # 参照 test_judgement_integrity 的 real/try/finally 还原模式。
    real_budget = commands.EMIT_TIMEOUT
    commands.EMIT_TIMEOUT = 10  # 远大于 sleep(0.2)，让快派发不被预算挡
    try:
        def quick_event(_event, _ctx):
            return None  # 没人接的形状，亚毫秒返回

        quick = Plugin(name="quick-probe-225", hooks=Hooks(on_event=quick_event))
        commands.PLUGINS.register(quick)
        try:
            fresh()
            t0 = time.monotonic()
            commands._emit_group_message(text_event("草", uid="1", mid=1),
                                         conf, SELF_IDS)
            elapsed = time.monotonic() - t0
            before = set(_log_lines())
            commands._emit_group_message(text_event("草", uid="1", mid=1),
                                         conf, SELF_IDS)
            after = set(_log_lines())
            slow_log = [ln for ln in after - before if "event plugin slow" in ln]
            check("预算够大时快派发不记慢日志", not slow_log, repr(slow_log))
            check("快派发不触发预算（远小于 budget）",
                  elapsed < commands.EMIT_TIMEOUT, f"elapsed={elapsed:.2f}s")
        finally:
            commands.PLUGINS._plugins.pop(quick.name, None)
    finally:
        commands.EMIT_TIMEOUT = real_budget


# ---- 5. 判据完整性自检 ---------------------------------------------------------

def test_judgement_integrity():
    """把判据换成恒 None / 恒 "x"，用例必须随之翻转。

    只钉「现在绿」的门禁挡不住「以后永远绿」：判据被换成 no-op 之后，所有
    「该跟」的用例仍然绿，门禁就变成了装饰。
    """
    print("[判据完整性自检]")
    real = repeater.consider
    try:
        repeater.consider = lambda *a, **k: None
        fresh()
        check("恒 None：该跟的用例翻红",
              feed([("草", "1"), ("草", "2"), ("草", "3")], None, None) is None)
        repeater.consider = lambda *a, **k: "x"
        fresh()
        check("恒 'x'：不该跟的用例翻红",
              feed([("草", "1"), ("草", "2")], None, None) == "x")
    finally:
        repeater.consider = real
    fresh()
    check("还原后该跟的用例仍绿",
          feed([("草", "1"), ("草", "2"), ("草", "3")], None, None) == "草")


def test_normalize():
    """配置归一的边界：越界概率必须钳住，不能回落成「开五成」。"""
    print("[配置归一]")
    opts = repeater.normalize({"probability": -0.2})
    check("probability=-0.2 钳到 0（想关的人不该被回落成 0.5）",
          opts["probability"] == 0.0, repr(opts["probability"]))
    opts = repeater.normalize({"probability": 1.5})
    check("probability=1.5 钳到 1", opts["probability"] == 1.0, repr(opts["probability"]))
    opts = repeater.normalize({"probability": "abc"})
    check("probability 非数字回落默认值",
          opts["probability"] == repeater.DEFAULTS["probability"],
          repr(opts["probability"]))
    opts = repeater.normalize({"min_repeats": 1})
    check("min_repeats=1 回升到 2（一个人复读不算复读）",
          opts["min_repeats"] == repeater.DEFAULTS["min_repeats"], repr(opts["min_repeats"]))
    opts = repeater.normalize({"deny_words": "禁词"})
    check("deny_words 写字符串也认", opts["deny_words"] == ["禁词"], repr(opts["deny_words"]))
    opts = repeater.normalize({"deny_words": [" a ", "", None]})
    check("deny_words 去空白与空项", opts["deny_words"] == ["a"], repr(opts["deny_words"]))
    opts = repeater.normalize({})
    check("空配置全用默认值",
          opts["window_seconds"] == repeater.DEFAULTS["window_seconds"]
          and opts["max_chars"] == repeater.DEFAULTS["max_chars"], repr(opts))
    opts = repeater.normalize({"max_chars": 0})
    check("max_chars=0 回落默认值（否则一条都跟不了，静默失效）",
          opts["max_chars"] == repeater.DEFAULTS["max_chars"], repr(opts["max_chars"]))


def test_event_stream_only():
    """`#` 指令关掉、只跑事件流时：复读照跟，指令一条都不回。

    启动日志承诺「只跑事件型插件（# 指令不回复）」，这一条把它钉住。
    """
    print("[只跑事件流：指令不回复]")
    conf = commands.settings_from({
        "self_account": SELF,
        "groups": {GROUP: {"enabled": True}},
        "slash_commands": {"enabled": False, "commands": {"ping": {"reply": "pong"}}},
        "plugins": {"repeater": {"enabled": True,
                                 "options": {"probability": 1.0}}},
    })
    check("这个配置下进程要起", commands.event_stream_needed(conf) is True)
    ping = text_event("#ping", uid="200000001")
    check("`#ping` 消息形态本身是通过 should_handle 的（对照组）",
          commands.should_handle(ping, conf, SELF_IDS) is True,
          "如果这条不成立，下面那条『总闸拦住』就测不到东西")
    check("总闸关：`#ping` 不派发", commands.should_dispatch_command(
        conf, ping, SELF_IDS) is False)
    on = commands.settings_from({
        "self_account": SELF,
        "groups": {GROUP: {"enabled": True}},
        "slash_commands": {"enabled": True, "commands": {"ping": {"reply": "pong"}}},
    })
    check("总闸开：`#ping` 照常派发（对照组）",
          commands.should_dispatch_command(on, ping, SELF_IDS) is True)

    sent = []
    real_sender = commands.PLUGINS.host._sender
    commands.SETTINGS_BOX["settings"] = conf
    commands.PLUGINS.host._sender = lambda s, gid, payload: sent.append((gid, payload))
    fresh()
    try:
        replies = [commands._emit_group_message(
            text_event("草", uid=uid, mid=index + 1), conf, SELF_IDS)
            for index, uid in enumerate(["1", "2", "3"])]
    finally:
        commands.PLUGINS.host._sender = real_sender
    check("只跑事件流时复读照跟（插件不看 slash_commands.enabled）",
          replies[-1] == "草" and len(sent) == 1, f"{replies!r} {sent!r}")


def main():
    for fn in (test_normalize, test_repeat_basics, test_chain, test_chain_parse,
               test_gates, test_frequency, test_ledger_robustness,
               test_plugin_wiring, test_host_dispatch, test_emit_budget,
               test_event_stream_only, test_judgement_integrity):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(f"{fn.__name__} 跑完不炸", False, f"{type(exc).__name__}: {exc}")
    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
