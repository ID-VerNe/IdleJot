"""issue #348 gate: 一份 parity 的临时现场只能被它自己收掉。

现场：同机两处 parity 同时活着时，其中一边的门禁整片报
`can't open file '.../idlejot-parity-XXXX/pristine/scripts/<gate>.py'`——树在跑的过程中
被另一边从底下抽走了。原先的收尾是「`remove` 自己的 → `prune` 全仓 → `rmtree`」，
且建树前不核对登记，所以任意一边都能抽掉另一边的树。

本门禁把修法的各条分别钉住，每条都配「不修就会红」的对照：

1. **跨进程互斥**：`check_pr_parity_lock` 的字节锁真能挡住第二个**进程**；同一个进程里
   两个句柄也互斥（钩子同进程连跑两次 parity 靠的就是这一条）；
2. **只认自己那份**：登记表按规范化路径比对；收尾只 `remove` 自己那条，别人正在用的树
   一格都不许动；
3. **不静默**：收不掉的残留要说进 `Report.problems`，不许一声不响地当成功；
4. **孤儿登记不复用**：登记在、目录不在时必须按失败处理并提示 `prune`，不许把一棵不存在
   的树当成可复用现场（取消全局 `prune` 之后的新坑，issue #348 复核第 ② 条）；
5. **`main()` 持锁接线**：跑真的 `check_pr_parity.main()`，锁被占期间它不许进入 `_parity`，
   放锁之后才进——只验锁模块本身证明不了 `main()` 用上了它（issue #348 复核第 ① 条）；
6. **收尾不误伤别人的登记**：本门禁收尾时若表里还有**现场之外活着**的 `idlejot-parity-*`
   树，`unregister_scene()` 必须拒绝全仓 prune、只把本现场的残留说出来——这是硬断言；
   旁人只剩**孤儿登记**（目录早没了）时才可能放行 `prune`，而这一步有前置条件：环境里
   本来就有旁树活着时打印 `SKIP` 不判红（issue #378 C1：环境脏不该染红），干净前提下
   仍留覆盖。
7. **收尾本身不再制造残留（issue #378 非破坏性部分）**：收尾某一步抛 `OSError` 时后续
   步骤仍执行且失败被说出来；建树中途失败不留半截现场；`fail` 分支先摘登记再删目录
   不造孤儿登记；`--keep` 给出可复制清理命令。清理只认 `idlejot-parity-` 前缀，绝不碰
   `.tmp/wt-*` / `tulpa-*`（前缀约束落成断言，不只写在注释里）。
8. **夹具自建自摘、登记表零差异（issue #378 卫生部分）**：用例造出的每条登记都在
   `finally` 里用 `git worktree remove --force` **只摘自己那一条**，目录已经不在的孤儿
   登记也一样（不把收尾寄托给全仓 `prune`，旁树活着时它本来就会被拦）；整个门禁跑完
   后 `git worktree list --porcelain` 与 `.git/worktrees` 条目相对跑前必须逐条一致，
   夹具漏登记就翻红。`prunable` 条数只作 WARN 级盘点（#378 自动回收的观测入口）。
9. **`--reclaim` 只报告档（issue #378 回收设计第一档）**：候选必须同时满足四条——前缀只认
   `idlejot-parity-*`（`.tmp/wt-*` / `tulpa-*` / `parity-348-*` 一律不进候选）、`pristine`
   不是活树、没有进程引用（按现场名扫 `Win32_Process` 的 CommandLine）、不在 `--keep` 现场
   （带 `.keep` 标记即排除，标记机制由第 11 条落地）。这一档**只打印命令、不删除**，
   断言里加一条反向对照（把前缀放宽到 `tulpa-*` 后该目录必进候选）证明判据不是空转。
10. **锁被占时 `--reclaim` 一条都不删**（复审窄修复）：旧写法在 rc=2 分支调
    `recorded_holder()`，该函数对 pid 已死的持有者记录顺手 `unlink()`（复审实测
    `stale_holder_before=True → after=False`），与「一条都不删」的字面口径冲突。夹具让
    一个真进程拿着锁、再把持有者记录改成死 pid，此时 `--reclaim` 必须 rc=2 **且记录原样
    还在**——修复前这一条必红。
11. **`--keep` 标记早落地（issue #378 删除档前置）**：`--keep` 的现场从 `make_scratch()`
    起就带 `.keep` 标记——跑门禁**之前**已在盘上，进程随后被硬杀（`finally` 不跑）标记也
    在，回收据此排除，不会把有意保留的排查现场当残留删掉。没有这条，标记写进 `finally`
    就保护不了硬杀路径。
12. **删除档（issue #378 回收第二档）**：`--reclaim --yes` 逐条复核前缀与 keep 标记
    （上游判据被改坏也不许在删除循环里误删）、`prunable` 先摘登记（`remove_registration()`
    只摘这一条，绝不 prune 全仓）再删目录、目录或登记收不掉要说话并给可粘贴手工命令、
    退出码非 0；动手前对候选再核一次进程引用。
13. **端到端**：真进程 CLI 带 `--root` 隔离根（绝不扫真实 `%TEMP%`）真删假现场 rc=0；
    0 候选照常退 0；锁被占立即 rc=2（与报告档同形）；`--yes` 不带 `--reclaim` 被拒。

本门禁**自己不跑全仓 prune 来造现场**：`cleanup()` 明文不调 `git worktree prune`，因为那是
全仓写操作。用例要的「真树」换一个 scratch 直接建就行；收尾也不再依赖 `prune`——本门禁
自己造出来的孤儿登记逐条 `git worktree remove --force` 摘掉（`forget_registration()`，
#378 删除档起委派生产侧 `parity.remove_registration()`——判据与执行只有一份，门禁的每次
夹具收尾同时也是生产路径的活测），`unregister_scene()` 的 `prune` 分支只在环境干净
（现场之外没有活树）时才走得到。

跑法：`python -m scripts.check_pr_parity_isolation`（本机手跑；它建真 worktree，量级与
`check_pr_parity.py` 同档，所以不在 CI 分片里跑）。#378 删除档：单文件 968 行按 #283/#285
/#329 先例拆成 `scripts/check_pr_parity_isolation/` 包（`__init__` / `__main__` / `_common` /
`cases_scene` / `cases_reclaim`），既有断言逐字搬运，入口名不变、对 gates.json 与 CI 透明。
"""
import os
from pathlib import Path
import shutil
import sys
import tempfile

from ._common import (
    FAILS, forget_or_report, prunable_report, registry_diff, registry_state,
    scene_paths, scene_rel, guarded,
)
from .cases_reclaim import RECLAIM_CASES
from .cases_scene import SCENE_CASES


def main():
    if os.name != 'nt':
        print('SKIP: parity 是 Windows-only（msvcrt）')
        return 0
    scene = Path(tempfile.mkdtemp(prefix='parity-348-'))
    before = registry_state()
    try:
        prunable_report('跑前')
        for name, case in (*SCENE_CASES, *RECLAIM_CASES):
            guarded(name, case, scene)
    finally:
        shutil.rmtree(scene, ignore_errors=True)
        # 回归断言（issue #378 卫生）：跑完的登记表必须与跑前**逐条一致**。
        # 先拍快照再清理：夹具漏了登记时这条要能翻红，而不是被收尾顺手抹平。
        left = scene_paths(str(scene))
        if left:
            FAILS.append(f'夹具留下登记：{[scene_rel(scene, p) for p, _ in left]}')
        for problem in registry_diff(before, registry_state()):
            FAILS.append(problem)
        # 判红已记下，清理只为不把本机越跑越脏；摘不掉就打印可复制命令（不许静默）。
        for path, _ in left:
            forget_or_report(path)
        still = scene_paths(str(scene))
        if still:
            FAILS.append(f'夹具残留摘不掉：{[scene_rel(scene, p) for p, _ in still]}')
            for path, _ in still:
                print(f'FAIL 手工清理命令 — git worktree remove --force "{path}"')
        # WARN 级盘点：本机陈年 prunable 是环境前提，只观测、不判红（#378 C1）。
        prunable_report('跑后')
    if FAILS:
        for line in FAILS:
            print(f'FAIL {line}')
        return 1
    print('PASS parity 现场隔离二十一项')
    return 0


if __name__ == '__main__':
    sys.exit(main())
