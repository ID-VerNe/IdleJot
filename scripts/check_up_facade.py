"""门面完整性：`up.<name>` 必须在席、且与归属模块是同一个对象（issue #37）。

issue #37 把 `up.py` 从一个 1063 行的杂物间拆成「三个模块 + 门面」。拆分之后有三种
退化形态，都比「搬错一行」安静：

1. **搬回去**：某个函数被挪回 `up.py`，门面重新变杂物间，下一次改动又会撞 1000 行墙。
2. **漏导出**：新模块加了公开函数，但 `up.py` 忘了 re-export——外部（`watch.py` /
   `reply.py` / 门禁）在运行期才 `AttributeError`，而门禁恰好绿在别的路径上。
3. **补丁打空**（本 issue 最坑的）：门禁拿 `up.config = lambda: ...` 当桩。拆分后
   `up.config` 只是 re-export，改它不会影响 `wg_up_wake.is_command` 内部读的 `config`，
   门禁照绿、测的却是真配置。所以这里把「同一对象」也钉死，谁再想打 `up.*` 至少会先
   看到这条契约。

顺带钉住分层方向：`wg_up_probe` / `wg_up_wake` 是底层，不许 import `up` 或
`wg_up_stack`（否则成环）；`wg_up_stack` 可以 import 前两个。

    .venv/Scripts/python.exe scripts/check_up_facade.py
"""
import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "water-group" / "daemon"
sys.path.insert(0, str(DAEMON))

from check_fixture_support import scratch_dir  # noqa: E402

# 数据根在 import 前隔离：up/wg_up_wake 都在模块级求 state_dir()。
HOME = scratch_dir(prefix="tulpa-facade-gate-")
os.environ["WATER_GROUP_HOME"] = str(HOME)

import up  # noqa: E402
import wg_up_probe  # noqa: E402
import wg_up_stack  # noqa: E402
import wg_up_wake  # noqa: E402

MODULES = (wg_up_probe, wg_up_wake, wg_up_stack)
MODULE_NAMES = ("wg_up_probe", "wg_up_wake", "wg_up_stack")

# up.py 允许自己定义的东西：进程自身标识与总览屏，外加两个只有入口用的助手。
# 新增条目要在这里写明理由，别让「先放回去再说」悄悄发生。
UP_OWN_DEFS = {"strip_proxy", "python_exe", "report", "main"}

# 分层方向：底层模块不得反向 import 门面或上层。
FORBIDDEN_IMPORTS = {
    "wg_up_probe": {"up", "wg_up_stack"},
    "wg_up_wake": {"up", "wg_up_stack"},
    "wg_up_stack": {"up"},
}


def top_level_defs(module):
    """公开的顶层函数/类（下划线开头的是模块内部件，不要求出现在门面上）。"""
    return {name for name, value in vars(module).items()
            if not name.startswith("_")
            and getattr(value, "__module__", None) == module.__name__
            and (callable(value) or isinstance(value, type))}


def up_literal_defs():
    """`up.py` 自己写下的顶层 def/class（不看 re-export 进来的）。"""
    tree = ast.parse((DAEMON / "up.py").read_text(encoding="utf-8"))
    return {node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def imported_names(source):
    """一个文件 import 了哪些顶层模块名（`import x` / `from x import ...`）。"""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def check_facade_covers_modules():
    """模块里的公开函数/类都能从 `up` 拿到，而且是同一个对象。"""
    missing, drifted = [], []
    for module in MODULES:
        for name in sorted(top_level_defs(module)):
            if not hasattr(up, name):
                missing.append(f"{module.__name__}.{name}")
            elif getattr(up, name) is not getattr(module, name):
                drifted.append(f"{module.__name__}.{name}")
    assert not missing, f"门面漏导出（外部会 AttributeError）：{missing}"
    assert not drifted, f"门面名字不是归属模块的对象（补丁会打空）：{drifted}"
    count = sum(len(top_level_defs(m)) for m in MODULES)
    print(f"✓ 1. 门面覆盖 {count} 个公开定义，且与归属模块同一对象")


def check_no_duplicate_defs():
    """同名公开定义不许同时出现在两个模块里——那就是第二份实现的入口。"""
    owners = {}
    dup = []
    for module in MODULES:
        for name in top_level_defs(module):
            if name in owners:
                dup.append(f"{name}: {owners[name]} + {module.__name__}")
            owners[name] = module.__name__
    assert not dup, f"同名定义出现在多个模块：{dup}"
    print(f"✓ 2. 三个模块之间无同名公开定义（{len(owners)} 个名字各归一处）")


def check_up_defines_only_the_facade():
    """`up.py` 自己只许定义门面清单里的东西。"""
    own = up_literal_defs()
    extra = sorted(own - UP_OWN_DEFS)
    assert not extra, (f"up.py 又长出了自己的定义：{extra}——放回对应模块，"
                       f"或者说明理由后加进 UP_OWN_DEFS")
    print(f"✓ 3. up.py 顶层只定义门面清单内的 {len(own)} 个名字")


def check_layer_direction():
    """底层不许反向 import 门面/上层；门面必须 import 全部三个模块。"""
    for name in MODULE_NAMES:
        source = (DAEMON / f"{name}.py").read_text(encoding="utf-8")
        bad = imported_names(source) & FORBIDDEN_IMPORTS[name]
        assert not bad, f"{name}.py 反向 import {sorted(bad)} —— 会成环"
    up_imports = imported_names((DAEMON / "up.py").read_text(encoding="utf-8"))
    missing = [name for name in MODULE_NAMES if name not in up_imports]
    assert not missing, f"up.py 没 import {missing}，门面是空的"
    print("✓ 4. 分层方向正确：probe/wake 不反向依赖，stack 只依赖下层，up 聚合三者")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 72)
    print("        [测试] issue #37 up.py 门面完整性：名字在席、同源、不分层反向")
    print("=" * 72)
    check_facade_covers_modules()
    check_no_duplicate_defs()
    check_up_defines_only_the_facade()
    check_layer_direction()
    print("=" * 72)
    print("✓ 全部 4 项门面契约测试通过！")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
