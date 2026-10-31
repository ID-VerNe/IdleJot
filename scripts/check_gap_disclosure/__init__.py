"""钉住停机窗口缺口披露：漏了什么事后说得出来，不再报告一切正常（issue #114）。

2026-10-26 那条 08:04 的 @ 就是这样丢的：后端 + NapCat 停机跨过一段时间再拉起，
窗口内的群消息不进任何本地存储（`chat_inbox` 由服务端在收到 OneBot 事件时写、
`wg_events.jsonl` 由 collector 在 `mention_only` 投递时写，停机时两者都不动）。
`up.py --status` 的「最近唤醒」只列拉起之后的、`reply.py --list` 为空、`listen.py`
报 `NO_WORK`——「没人叫我」与「有人叫过但机器不在」无法区分，直到人肉翻
`get_group_msg_history` 才发现窗口内 33 条、其中 1 条 @ 本机。

这条门禁钉住下面这些契约（编号是分组），任何一处退回旧行为都红：1. `wg_gap.compute()`
   的 diff 正确：夹具 `chat_inbox` 有 a/b/c，假 `get_group_msg_history` 返回 a/b/c/d/e
   （d/e 在停机窗口内、e 是 @ 本机），返回 `missing=2, at_me=1`，`at_me_lines` 含 e 的
   sender + 时间 + 正文片段。2. `wg_gap.describe()` 缺口非空时报 missing/at_me 计数；
   无记录或缺口为空时返回空串（排除型对照：恒加注脚的红在后半条，与 #112 同口径）。
   3. `up.py --status` 的「缺口」行缺口非空时出现、为空时不加字（只读 `wg_gap.json`，
   不联网、不写——保住 `--status`「只看不动」契约）。4. `listen.py` 的 NO_WORK 路径
   缺口非空时插一行提示、`RESTART:` 仍是末行（#69/#112 契约 1/9 不变）；WAKE 路径不插
   （Agent 已被叫醒）。5. `reply.py --list` 缺口非空时列漏掉的 @ 本机消息（行首 `id=-`），
   为空时不加段。6. `get_group_msg_history` 单次 200 上限（候选 4）：最老一条新于本地
   最后一条→`truncated=True` 且 `describe()` 含「只能核对最近 200 条」；老于则
   `truncated=False` 且无该句。7. NapCat 未响应（`fetch` 抛错）→ `unreachable=True`，
   `describe()` 说「无法核对」，不崩。8. @ 本机判定用 `self_account`，非本机 @ 不计入
   `at_me`。9. 本地一条都没有（首次部署/换群）不报缺口——与「停机漏收」区分。10. 窗口
   右边界取 `napcat_launch.json:started_epoch`，读不到回落 `computed_at`。
11. 缺口非空时 `describe()` 带**窗口起止** + **账本计算时刻** + 明写「仅供参考」（#148）：
    账本只在 collector 启动时算一次，不带时刻时旧账与新缺口同形。无缺口时仍空串（排除型
    对照：恒加注脚的红在这条）。12. 跨天窗口两端补 `MM-DD`，同一天不补（#148）。
    13. `at_me_lines()` 时间戳带日期（`YYYY-MM-DD HH:MM:SS`），与唤醒行的 `HH:MM:SS`
    不同形（#148）。14. `--list` 缺口段标题下紧跟窗口上下文、排在 `id=-` 行之前（#148）。
15. `wg_gap._local_state` 会话号解析与 `#219` 同口径（payload 优先、会话行兜底），但缺
    `chat_sessions` 表时（夹具/旧库）必须退回只读 payload，不得静默归零（#219）：LEFT
    JOIN 撞缺表抛 `sqlite3.Error` 被 `except` 吞掉会让缺口报得**比实际小**，比改前的「报得
    大」更危险。认群按 `wg_event_ref.group_of(cid) == gid`（字段结构判定），不按
    `endswith(':' + gid)`（#211）：同库里 `:direct:<user_id>` 会话的 user_id 等于某群号时，
    后缀匹配把私聊串进群、`local_ids` 与 `window_start` 都错，缺口少报。

全程临时 `WATER_GROUP_HOME` + `IDLEJOT_WG_DB`，不碰实机状态：

    .venv/Scripts/python.exe -m scripts.check_gap_disclosure

#283：本文件曾是单文件 `scripts/check_gap_disclosure.py`（958 行，advisory 带），
按 #169 PF-2（`scripts/check_listener_rehang/`）与 #264（`scripts/check_dossier_cadence/`）
先例拆成包：`_fixture`（数据根 / 夹具层 / `status_lines` 模板渲染）、`cases`（~25 条用例 +
`case(fn)` 收集器 + 用例内 helper + `main()`），`__init__.py` 保留 docstring 与聚合
`main()`，`__main__.py` 供 `python -m`。入口名不变（`check_gap_disclosure`），对
`gates.json` / `run_gates.py` / CI 透明。用例编号、断言、正文逐字搬运，零重排。
"""
# 导入序是 load-bearing（issue #283）：`_fixture` 必须最先——它在 import
# `check_fixture_support` 之前先把 `scripts/` 插进 `sys.path`、钉住数据根（`scratch_dir` /
# `IDLEJOT_WG_STATE` 指向夹具 `state/`）、进口 daemon 依赖（`up`/`wg_gap`/`reply` 等）、
# 定义 `fs` / 常量 / 夹具函数 / `CASES` / `case` 收集器 / `status_lines` 渲染 helper。
# `cases` 子模块 `from ._fixture import ...` 依赖这些名字先就位。顺序即旧文件的
# `@case` 注册序——25 条契约的运行顺序靠它逐字保持。禁止改成一行 `from . import
# _fixture, ...`：显式分行防后人误重排。
from . import _fixture  # noqa: F401
from . import cases  # noqa: F401

from ._fixture import CASES, fs


def main():
    print("=" * 72)
    print("        [测试] issue #114 停机窗口缺口披露：漏了什么事后说得出来")
    print("=" * 72)
    return fs.run_cases(CASES)
