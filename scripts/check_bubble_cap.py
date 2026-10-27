"""钉住「一次回复能发几条气泡」：上限只有一个数，超限不再静默（issue #131）。

拆句上限过去是 `reply.py:243` 的裸 `3`，超出的句子只往 stdout 打一行 NOTE 就
扔掉——群里看到的是戛然而止的回复，而发送层本来接得住。同一份契约当时散在九处
（`reply.py` docstring/help、`mcp_chat_turns.py` 的 schema 与校验、`mcp_chat.py`
的系统提示、人格卡、`doc/MCP_CHAT_TURNS.md`、两份 `.agents/` 规范），改一处必漏。

这个门禁分两半：

1. **一致性**：两个上限（OneBot 直发 20 / 计划通道 12）各只有一处定义，所有文档
   与文案里的数字必须等于常量。断的是「下次只改一处」。
2. **行为**：0/1/5/20/21 行逐档断言「拆出几条、提示了什么」，外加配置上限、非法
   配置回落、成本估算、计划通道发送前拒绝。

    .venv/Scripts/python.exe scripts/check_bubble_cap.py
"""
import contextlib
import io
import math
import re
import sys
from pathlib import Path

import check_fixture_support as fs  # noqa: E402  (issue #169 PE: path 注入收口)
ROOT = fs.bootstrap_paths(scripts=False)

import wg_bubbles  # noqa: E402
import wg_budget  # noqa: E402
import reply  # noqa: E402
from chatlocal import mcp_chat_turns as turns  # noqa: E402


def quiet(fn, *args, **kwargs):
    """跑一次并把 (返回值, stdout) 一起交出来——提示就是被测行为的一部分。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        value = fn(*args, **kwargs)
    return value, buf.getvalue()


def lines(count, prefix="第"):
    return "\n".join(f"{prefix}{i}句" for i in range(1, count + 1))


def fake_tool(name, description, properties, required):
    return dict(name=name, description=description, properties=properties, required=required)


def bubble_schema():
    for row in turns.schema(fake_tool):
        if row["name"] == "send_chat_reply":
            return row
    raise AssertionError("send_chat_reply 不在 schema 里")


def doc_number(path, pattern):
    """从文案里取出那个数字；取不到就是门禁自己坏了，直接抛。"""
    text = (ROOT / path).read_text(encoding="utf-8")
    found = re.search(pattern, text)
    if not found:
        raise AssertionError(f"{path} 里没有匹配 {pattern!r} 的文案")
    return int(found.group(1))


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# --------------------------------------------------------------------------
# 1. 一致性：一个数只写一次，其余全取常量
# --------------------------------------------------------------------------

@case
def 两侧上限来自同一个数():
    return turns.TURN_MAX_BUBBLES == wg_bubbles.TURN_MAX_BUBBLES


@case
def schema_与校验都取常量():
    bubbles = bubble_schema()["properties"]["bubbles"]
    return bubbles["maxItems"] == turns.TURN_MAX_BUBBLES and bubbles["minItems"] == 1


@case
def 校验拒绝上限加一():
    # 断言的是行为而不是写法（issue #136）：13 条必须被 `invalid_bubbles` 拒，12 条
    # 不能因为条数被拒。过去这里比对源码子串，同形坏实现（常量改了、校验另写一个
    # 数）照样能过。`submit()` 与门禁共用 `check_bubble_count()`，所以拿它问就是
    # 问发送路径本身；端到端的 submit() 用例在 check_mcp_chat_turns.py。
    def code(bubbles):
        try:
            turns.check_bubble_count(bubbles)
        except turns.TurnRejected as exc:
            return exc.code
        return None

    many = [dict(kind='text', text='第%d句' % i) for i in range(turns.TURN_MAX_BUBBLES + 1)]
    exact = many[:-1]
    return (code(many) == 'invalid_bubbles'
            and code(exact) is None
            and code([]) == 'invalid_bubbles'
            and code('not a list') == 'invalid_bubbles'
            and bubble_schema()["properties"]["bubbles"]["maxItems"] == turns.TURN_MAX_BUBBLES)


@case
def 工具描述写的是当前上限():
    return f"1–{turns.TURN_MAX_BUBBLES} 个语义气泡" in bubble_schema()["description"]


@case
def 文案里的数字都跟着常量走():
    sites = [
        ("chatlocal/mcp_chat_turns.py", r"一次提交 1–(\d+) 个语义气泡"),
        ("chatlocal/mcp_chat.py", r"提交1至(\d+)个语义气泡"),
        ("water-group/personas/behavior.md", r"上限 (\d+)，一次长总结"),
        ("doc/MCP_CHAT_TURNS.md", r"和 1–(\d+) 个完整语义气泡"),
    ]
    return all(doc_number(path, pattern) == turns.TURN_MAX_BUBBLES
               for path, pattern in sites)


@case
def 回复侧文档写的是自己的上限():
    sites = [
        ("water-group/daemon/reply.py", r"默认最多 (\d+) 条"),
        (".agents/rules/water_group.md", r"默认最多 (\d+) 条"),
        (".agents/skills/water-group/SKILL.md", r"默认最多 (\d+) 条"),
        ("water-group/config.example.json", r'"max_bubbles":\s*(\d+)'),
    ]
    return all(doc_number(path, pattern) == wg_bubbles.MAX_BUBBLES
               for path, pattern in sites)


@case
def 回复侧仍导出旧名字():
    return reply.MAX_BUBBLES == wg_bubbles.MAX_BUBBLES and reply.BUBBLE_GAP == wg_bubbles.BUBBLE_GAP


# --------------------------------------------------------------------------
# 2. 行为：拆句、上限、提示
# --------------------------------------------------------------------------

@case
def 五句全都发得出去():
    # issue #131 的原始症状：五句只发三句。这里钉死 5 → 5。
    return reply.split_bubbles(lines(5)) == [f"第{i}句" for i in range(1, 6)]


@case
def 单句不拆():
    return reply.split_bubbles("就一句") == ["就一句"]


@case
def 空文本不炸():
    return reply.split_bubbles("") == [""] and reply.split_bubbles(None) == [""]


@case
def 字面量换行仍拆():
    return reply.split_bubbles("第一句\\n第二句\\n第三句\\n第四句") == [
        "第一句", "第二句", "第三句", "第四句"]


@case
def 上限之内不提示():
    got, out = quiet(reply.split_bubbles, lines(wg_bubbles.MAX_BUBBLES))
    return len(got) == wg_bubbles.MAX_BUBBLES and "WARN" not in out


@case
def 超过上限截断并给出完整账():
    got, out = quiet(reply.split_bubbles, lines(wg_bubbles.MAX_BUBBLES + 1))
    kept = len(got) == wg_bubbles.MAX_BUBBLES
    told = (f"已发 {wg_bubbles.MAX_BUBBLES} 条、丢弃 1 条" in out
            and f"第{wg_bubbles.MAX_BUBBLES + 1}句" in out)  # 丢的是哪句也要说出来
    return kept and told


@case
def 上限可按次调小():
    got, out = quiet(reply.split_bubbles, lines(6), 5)
    return len(got) == 5 and "已发 5 条、丢弃 1 条" in out


@case
def 配置能调小上限():
    return wg_bubbles.config_limit({"reply": {"max_bubbles": 5}}) == 5


@case
def 配置缺省用硬顶():
    return (wg_bubbles.config_limit({}) == wg_bubbles.MAX_BUBBLES
            and wg_bubbles.config_limit(None) == wg_bubbles.MAX_BUBBLES)


@case
def 非法配置回落并说出来():
    got, out = quiet(wg_bubbles.config_limit, {"reply": {"max_bubbles": "abc"}})
    bool_case, bool_out = quiet(wg_bubbles.config_limit, {"reply": {"max_bubbles": True}})
    # 排除型断言的对照组：合法值必须一声不响（否则「总是打 WARN」也能过）
    ok_case, ok_out = quiet(wg_bubbles.config_limit, {"reply": {"max_bubbles": "8"}})
    return (got == wg_bubbles.MAX_BUBBLES and "不是正整数" in out
            and bool_case == wg_bubbles.MAX_BUBBLES and "不是正整数" in bool_out
            and ok_case == 8 and "WARN" not in ok_out)


@case
def 越界配置夹回硬顶():
    got, out = quiet(wg_bubbles.config_limit, {"reply": {"max_bubbles": 999}})
    zero, _ = quiet(wg_bubbles.config_limit, {"reply": {"max_bubbles": 0}})
    return got == wg_bubbles.MAX_BUBBLES and zero == 1 and "超出" in out


@case
def 成本按间隔算():
    return (round(wg_bubbles.estimate_seconds(5), 1) == 4.8
            and wg_bubbles.estimate_seconds(1) == 0
            and wg_bubbles.estimate_seconds(0) == 0)


@case
def 成本行按实际等待报剩余():
    # 三种 callsite 都要说实话：缺省按预算上限、--wait 30 就报 30、--no-wait 不报额度。
    # 期望值现算（issue #136）：`85s`/`25s` 曾经写死在这里，`wg_budget.WAIT_BUDGET`
    # 一改就假红——一个专门反对「同一个数写两处」的门禁自己藏了重复数字。
    spend = math.ceil(wg_bubbles.estimate_seconds(5))
    budget = wg_budget.WAIT_BUDGET
    default = wg_bubbles.cost_note(5)
    waited = wg_bubbles.cost_note(5, 30)
    no_wait = wg_bubbles.cost_note(5, 0)
    return (f"{wg_bubbles.estimate_seconds(5):.1f}s" in default
            and f"还能等 {budget - spend}s" in default
            and "最多等 30s" in waited and f"还能等 {max(0, 30 - spend)}s" in waited
            and "不占等待预算" in no_wait)


@case
def 计划通道成本按区间报下界():
    # 计划通道的真实间隔是 bubble_min…max（默认 1.2–4s），只报 BUBBLE_GAP 会把
    # 预算占用说小；区间形态下用「至少 / 最坏」，剩余额度按最坏扣（issue #136）。
    # 用 policy={} 走缺省回落，不读实机数据根（门禁不许依赖本机配置）。
    low, high = wg_bubbles.PLAN_GAP
    note = wg_bubbles.cost_note_for(5, plan_path=True, policy={})
    direct = wg_bubbles.cost_note(5, gap=low, gap_max=high)
    fixed = wg_bubbles.cost_note_for(5, policy={})
    worst = math.ceil(wg_bubbles.estimate_seconds(5, high))
    return (f"间隔 {low:g}–{high:g}s" in note and "至少" in note and "最坏" in note
            and f"还能等 {wg_budget.WAIT_BUDGET - worst}s" in note
            and "≈" not in note and note == direct
            and fixed == wg_bubbles.cost_note(5))


@case
def 计划通道间隔缺省回落常量():
    # policy 文件缺失/非法时回落 PLAN_GAP，而不是崩或报 0——报账不是校验器。
    low, high = wg_bubbles.PLAN_GAP
    missing = wg_bubbles.plan_gap_range({})
    broken = wg_bubbles.plan_gap_range({"bubble_min_seconds": "abc"})
    inverted = wg_bubbles.plan_gap_range({"bubble_min_seconds": 5, "bubble_max_seconds": 1})
    custom = wg_bubbles.plan_gap_range({"bubble_min_seconds": .5, "bubble_max_seconds": 2})
    return (missing == (low, high) and broken == (low, high) and inverted == (low, high)
            and custom == (.5, 2.) and turns.DEFAULTS['bubble_min_seconds'] == low
            and turns.DEFAULTS['bubble_max_seconds'] == high)


@case
def 单条不报成本():
    return wg_bubbles.cost_note(1) == "" and wg_bubbles.cost_note(0) == ""


@case
def 计划通道刚好装得下():
    return wg_bubbles.plan_overflow_message(wg_bubbles.TURN_MAX_BUBBLES) is None


@case
def 计划通道装不下要提前说清():
    note = wg_bubbles.plan_overflow_message(wg_bubbles.TURN_MAX_BUBBLES + 1)
    return (note is not None and "一条都没发" in note
            and str(wg_bubbles.TURN_MAX_BUBBLES) in note
            and str(wg_bubbles.MAX_BUBBLES) in note)


def main():
    return fs.run_cases(CASES)


if __name__ == "__main__":
    sys.exit(main())
