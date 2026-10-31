"""钉住画像沉淀的第三条判据：连续 N 轮没人指名就触发（issue #162）。

issue #162 的现象：`dossier.py --check` 只有两条阈值（攒够 `burst_messages`，
或闲置 `idle_minutes` 且攒够 `min_messages`），**没有「连续几轮没被 @」这一维**。
于是安静的群（没人 @、但一直有人说话）永远判 `NO`：`idle` 不够 20 分钟、
`pending` 也涨不到 80，画像长期不更新。issue 里的实测是
`NO idle=13min pending=0`，本机复现是 `NO idle=7min pending=46` —— 同一形状。

这条门禁钉住新判据与它的接线，任何一处退回旧行为都红：

1. `--tick` 跨进程累加（`listen.py --cycles 1` 一次只跑一轮，「连续 3 轮」是三次
   进程调用），且 `quiet_since` 只在首轮写入、之后不再移动；
2. 轮次够了、素材够了、下限也过了 → `YES quiet` + exit 0；轮次不够 → NOT_YET；
3. `quiet_minutes` 是**下限**：3 轮在几秒内跑完时不触发（`--timeout 30` 时三轮
   只有 90 秒，只钉轮次会让「约 5 分钟」变成假话）；
4. `pending=0` 时即便 3 轮也不触发（空跑一次模型调用）；
5. 真实 `listen.py` 静默两轮 → 两次 tick，`quiet_rounds==2`（接线真的接上了）；
6. 真实 `listen.py` 收到 @ → `WAKE` 之后计数归零（有人指名是「连续无人指名」的反例）；
7. `--no-digest`（`reply.py` 续挂用的形态）与 `--probe`（门禁/测试的临时监听）
   都不计数——否则每次回完消息立刻满足条件，跑一次门禁就推高生产计数；
8. `--mark` 清零计数并推进游标，**再 tick 一次游标仍在**（`save_mark` 覆盖写的
   复发检测：覆盖写会把 `last_event_id` 冲掉，症状是「沉淀完立刻又 DUE」或
   「计数永远不涨」，两种都像偶发）；
9. 判据对时钟的依赖用**扫时钟**验证：`quiet_since` 固定，`now` 扫全天 + 边界，
   必须在 `>= quiet_minutes` 时 DUE、否则 NOT_YET。**不用固定时刻夹具**——
   `quiet_since` 是相对量，固定时刻在跨午夜/跨日时会假绿（#153 的教训）；
10. 对照组：同一组 `now`、同一份 `quiet_since`，只把 `quiet_rounds` 降到 2，
    全部必须 NOT_YET——否则「绿」只证明时长这一维被读到，轮次那一维是死的；
11. `NO` 行的 `quiet=2/3(4.1min)` 用**正则捕获组**逐字段比，不拿子串（#151/#152
    的教训：`"2/3" in text` 挡不住「rounds 印对、since 印错」的坏实现）；
12. 写坏的 mark 值（`quiet_since: false` / 字符串 / 列表 / 负数）降级成「没有
    计数」，不崩、也不变成「下限恒过」（`bool` 是 `int` 子类的坑，同 #124）；
13. **默认配置下第 3 轮必须 DUE**。这条补的是一个真实漏测：前 12 条全绿时，
    默认 `quiet_minutes=5` 让 3 轮（4.5 分钟）永远过不了下限，实际要到第 5 轮
    才触发——轮次判据成了死码，而 `NO` 行照样显示 `3/3`。用例 2 把下限覆盖成 0、
    用例 3 只钉「下限在拦」，没有一条断言默认组合能真的触发。修法是两头：
    `--tick --round-seconds` 把 `quiet_since` 回推到首轮**开始**（否则 N 轮只跨
    N-1 个间隔），默认下限取 4（< 4.5 留余量）。用例同时钉住两头。
14. 真实 `listen.py` 静默一轮 → `quiet_since` 回推到本轮开始（`--round-seconds`）。
15. **积压不是爆发**（issue #183）：200 条全部落在 `burst_window_minutes` 之外
    + 3 轮静默 → `YES quiet`，不再被 `YES burst` 短路。这条是 issue 现象的直接
    回归：旧代码只要 pending ≥ 80 就恒报 `YES burst`，quiet 分支根本走不到。
16. 窗口内的真爆发仍报 `YES burst`（原语义没有被 quiet 抢走）。
17. **同一批素材只叫醒一次**：`--tick due` 之后 `--check` 输出 `NO due-hold` 且
    NOT_YET；新消息进来立刻重新 DUE。没有这条，游标不动时每轮都在叫同一批。
18. **停滞兜底**：`--digest --reap` 在宽限内不动游标；超过 `reap_after_minutes`
    才推进到本批末尾，之后 pending 归零、quiet 计数清零、`--check` 转 `NO`。
19. `--mark` 清 due 状态：沉淀完成后下一批不该被上一批的 DUE 挡住。
20. **接线真的接上了**：真实 `listen.py` 判 DUE 后写 `due_event_id`；`--probe`
    不写（临时监听不许污染生产状态）。
21. **reap 只吞已消化的前缀**：200 条积压、单批 150 条 → 推进到 150、剩 50 条
    pending。直接推到 `due_event_id` 会跳过中间 50 条，用例 18 只有 6 条时
    抓不住（复核发现）。
22. `due_at` 在未来（时钟回拨 / 手改 mark）不许把 DUE 永久压住，也不许让
    `--reap` 卡在 `stalled=0`（复核发现）。
23. **下限的读法与机器速度无关**（issue #196）：同一份 mark、同一组**模拟**时刻，
    只把下限从「已过」换到「未过」，跨度读数必须一正一反。用例 13 走真实子进程，
    它的跨度必然掺进进程启动耗时；这条把那一维搬进进程内 patch 时钟，快慢都红
    不了，也挡住「把下限调松换绿」的修法：两档与用例 13 共用 `FIXTURE_*` 常量、由
    `fixture_floor_is_tight()` 钉住「恰好等于回推跨度」（issue #215）。

已知边界（这条门禁**不**声称能抓住的）：

- **硬编码日期/时刻**：判据只比相对量，把 `quiet_since` 换成写死的字面量也过；
  要覆盖得多组不同日期的夹具，本门禁没做（#151 已把这条写成既有边界）。
- **多群分槽**：`wg_dossier.json` 本来就是单个全局文件，本 issue 不扩面，
  docstring 里已声明（`doc/MULTI_GROUP_PLAN.md` 负责那次拆分）。
- **时间戳缺失的消息算不算爆发**：`recent_count` 对没有 `timestamp` 的行按「不在
  窗口内」处理（不虚报），这里的用例都带时间戳；线上 `collector` 一定写，缺了是
  上游问题，不在本门禁的射程内。

全程临时 `WATER_GROUP_HOME` + `IDLEJOT_WG_CONFIG`（阈值走临时配置，不读开发机
的 `config.json`），不碰 `~/.water-group`：

    .venv/Scripts/python.exe -m scripts.check_dossier_cadence

#247：本文件曾是单文件 `scripts/check_dossier_cadence.py`，PR #243 之后恰好顶到
`MAX_FILE_LINES = 1000` 的红线（余量为零，加一行即红）。按 #169 PF-2（
`scripts/check_listener_rehang/`）的先例拆成包：`_common`（常量/夹具/helper）、
`quiet_rounds`（用例 1-8）、`clock_fields`（用例 9-12）、`floor_reading`（用例
13/14/23）、`backlog_cursor`（用例 15-22），`__init__.py` 保留 docstring 与聚合
`main()`，`__main__.py` 供 `python -m`。入口名不变（`check_dossier_cadence`），
对 `gates.json` / `run_gates.py` / CI 透明。用例编号、断言、正文逐字搬运，零重排。
"""
# 导入序是 load-bearing（issue #247）：`_common` 必须最先——它把 `scripts/` 插进
# `sys.path`、钉住数据根（`check_fixture_support` import 期会把它钉成夹具目录）、
# 并定义 `fs` / 常量 / 夹具函数 / `dossier_module`。其他子模块 `from ._common
# import ...` 依赖这些名字先就位。顺序即旧文件的用例调用序——23 条契约的运行
# 顺序靠它逐字保持。禁止改成一行 `from . import _common, ...`：显式分行防后人误重排。
import sys

from . import _common  # noqa: F401
from . import quiet_rounds  # noqa: F401
from . import clock_fields  # noqa: F401
from . import floor_reading  # noqa: F401
from . import backlog_cursor  # noqa: F401

from .quiet_rounds import (
    test_tick_accumulates_across_processes,
    test_three_rounds_are_due,
    test_floor_blocks_short_rounds,
    test_no_material_is_not_due,
    test_listen_counts_quiet_rounds,
    test_wake_resets_counter,
    test_no_digest_and_probe_do_not_count,
    test_mark_and_tick_do_not_clobber,
)
from .clock_fields import (
    test_clock_sweep_matches_floor,
    test_rounds_dimension_is_live,
    test_no_line_reports_counter,
    test_bad_mark_values_do_not_crash,
)
from .floor_reading import (
    test_default_config_fires_on_round_three,
    test_listen_backdates_first_round,
    test_floor_reading_survives_a_slow_host,
)
from .backlog_cursor import (
    test_backlog_is_not_a_burst,
    test_recent_spike_is_still_a_burst,
    test_same_batch_is_not_announced_twice,
    test_digest_reap_releases_a_stalled_cursor,
    test_mark_clears_due_state,
    test_listen_records_due_batch,
    test_reap_only_covers_the_digested_prefix,
    test_future_due_does_not_hold_forever,
)
from ._common import temp_dir, write_config, seed_store


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with temp_dir(prefix="wg-cadence-") as home:
        seed_store(home)
        write_config(home)
        test_tick_accumulates_across_processes(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_three_rounds_are_due(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_floor_blocks_short_rounds(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_no_material_is_not_due(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_listen_counts_quiet_rounds(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_wake_resets_counter(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_no_digest_and_probe_do_not_count(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_mark_and_tick_do_not_clobber(home)
    test_clock_sweep_matches_floor()
    test_rounds_dimension_is_live()
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_no_line_reports_counter(home)
    with temp_dir(prefix="wg-cadence-") as home:
        seed_store(home)
        write_config(home)
        test_bad_mark_values_do_not_crash(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_default_config_fires_on_round_three(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_listen_backdates_first_round(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_backlog_is_not_a_burst(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_recent_spike_is_still_a_burst(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_same_batch_is_not_announced_twice(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_digest_reap_releases_a_stalled_cursor(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_mark_clears_due_state(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_listen_records_due_batch(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_reap_only_covers_the_digested_prefix(home)
    with temp_dir(prefix="wg-cadence-") as home:
        write_config(home)
        test_future_due_does_not_hold_forever(home)
    test_floor_reading_survives_a_slow_host()
    print("=" * 72)
    print("✓ dossier 沉淀时机（连续 N 轮无人指名 + 积压归因 + 游标兜底）23 项门禁全部通过")
    print("=" * 72)
