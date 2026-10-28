"""Guards the single module identity of the water-group subsystem.

`water-group` contains a hyphen, so it cannot be imported as a package name
directly and the bridge package `water_group` exists to give every file exactly
one canonical import path. Before this suite, five call sites imported the
memory engine as the top-level module `memory_engine` while three others used
`water_group.memory_engine`. The engine holds a module-level singleton, so the
process silently ended up with two engines: two seed runs, two locks, two sets
of state.

The suite checks three layers:
  1. No source file imports a water-group module under a bare top-level name.
  2. Importing the package does not eagerly pull in optional dependencies.
  3. In a clean subprocess, both import paths resolve to the same module object
     and the singleton is shared.
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from check_fixture_support import scratch_dir  # noqa: E402

# 子进程探针会 import water_group.memory_engine，而 WG_HOME / DEFAULT_DB_PATH 都是
# import 期求值的（memory_store.py 的 WG_HOME / DEFAULT_DB_PATH，issue #11 PA 搬家），
# 不钉就会落到实机 ~/.water-group。
# get_memory_engine() 首次加载还会自动 seed_from_persona()（facade 末尾的单例），
# 于是每跑一次本门禁就往实机库写两行种子档案。钉到临时目录后，探针仍然验证同一件事
# （模块同一性 / 单例唯一），只是不再碰真实数据。见 issue #79。
_SCRATCH = scratch_dir(prefix="tulpa-wg-import-")
(_SCRATCH / "home").mkdir(parents=True, exist_ok=True)
PROBE_ENV = {
    **os.environ,
    "WATER_GROUP_HOME": str(_SCRATCH / "home"),
    "IDLEJOT_WG_MEMORY_DB": str(_SCRATCH / "mem.sqlite3"),
}

# Files whose only job is to import through the bridge; a top-level import here
# would be the bug this suite exists to catch.
SCANNED = [
    "chatlocal",
    "water-group",
    "water_group",
    "scripts",
    "app.py",
    "app_mcp.py",
]
WATER_GROUP_MODULES = {
    "memory_engine", "memory_commands", "memory_format",
    "memory_store", "memory_profile", "memory_observe", "memory_dossier",
    "bootstrap", "sync_groups",
}

RUNTIME_PROBE = """
import sys
import water_group
from water_group.memory_engine import get_memory_engine

# One module object per file: no top-level alias was created.
assert 'memory_engine' not in sys.modules, sorted(k for k in sys.modules if 'memory_engine' in k)
assert sys.modules['water_group.memory_engine'] is water_group.memory_engine

# One engine: the singleton is shared, not duplicated per import path.
assert get_memory_engine() is get_memory_engine()
print('PROBE_OK')
"""

STAR_PROBE = """
ns = {}
exec('from water_group import *', ns)
for name in ['memory_engine','memory_commands','memory_format','memory_store',
             'memory_profile','memory_observe','memory_dossier','bootstrap',
             'sync_groups']:
    assert name in ns, name
assert 'sys' not in ns and 'Path' not in ns and 'importlib' not in ns
print('STAR_OK')
"""

LAZY_PROBE = """
import sys
import water_group
assert 'water_group.bootstrap' not in sys.modules, 'bridge must not eager-import bootstrap'
assert sorted(water_group.__all__) == sorted([
    'memory_engine','memory_commands','memory_format','memory_store',
    'memory_profile','memory_observe','memory_dossier',
    'bootstrap','sync_groups'])
print('LAZY_OK')
"""


def test_no_bare_imports():
    """No file may import a water-group module under a top-level name."""
    violations = []
    for target in SCANNED:
        base = ROOT / target
        paths = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                raise AssertionError(f"{path} 无法解析: {exc}") from exc
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                for name in names:
                    if name in WATER_GROUP_MODULES or name.split(".")[0] in WATER_GROUP_MODULES:
                        if name.split(".")[0] != "water_group":
                            violations.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {name!r}")
    assert not violations, "water-group 模块必须通过 water_group 包名导入:\n  " + "\n  ".join(violations)


def test_no_string_import():
    """importlib.import_module would bypass the AST check above."""
    import re
    pattern = re.compile(r"""import_module\(\s*['"](?:memory_engine|memory_commands|memory_format|memory_store|memory_profile|memory_observe|memory_dossier|bootstrap|sync_groups)['"]""")
    hits = []
    for target in SCANNED:
        base = ROOT / target
        paths = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.relative_to(ROOT)}:{line}")
    assert not hits, "禁止用字符串导入 water-group 模块:\n  " + "\n  ".join(hits)


def test_bridge_lazy():
    """Importing the bridge must not eagerly load optional dependencies."""
    done = subprocess.run([sys.executable, "-c", LAZY_PROBE], cwd=ROOT, capture_output=True, text=True, env=PROBE_ENV)
    assert done.returncode == 0, done.stderr
    assert "LAZY_OK" in done.stdout


def test_single_module_identity():
    """In a clean interpreter both paths must be the same module object."""
    done = subprocess.run([sys.executable, "-c", RUNTIME_PROBE], cwd=ROOT, capture_output=True, text=True, env=PROBE_ENV)
    assert done.returncode == 0, done.stderr
    assert "PROBE_OK" in done.stdout


def test_star_import_stays_clean():
    """Star import must expose the submodules and nothing from the bridge."""
    done = subprocess.run([sys.executable, "-c", STAR_PROBE], cwd=ROOT, capture_output=True, text=True, env=PROBE_ENV)
    assert done.returncode == 0, done.stderr
    assert "STAR_OK" in done.stdout


def _live_seed_rows():
    """`seed:*` user_ids currently present in the live memory database."""
    import sqlite3

    live_db = Path.home() / ".water-group" / "data" / "group_memories.sqlite3"
    if not live_db.exists():
        return set()  # 没有实机库可污染（干净机器），无从断言
    conn = sqlite3.connect(str(live_db))
    try:
        return {r[0] for r in conn.execute(
            "SELECT user_id FROM member_profiles WHERE user_id LIKE 'seed:%'"
        ).fetchall()}
    finally:
        conn.close()


def test_no_live_db_pollution():
    """Guards against a future probe forgetting to pin the data root.

    Every probe below runs in a subprocess that imports memory_engine, whose
    WG_HOME / DEFAULT_DB_PATH are evaluated at import time. A probe that does
    not pin WATER_GROUP_HOME / IDLEJOT_WG_MEMORY_DB silently resolves the real
    ~/.water-group, and get_memory_engine() seeds two placeholder profiles into
    the live database on first use (issue #79). Failing here turns that into a
    red gate instead of a mystery row someone notices weeks later.

    The probes run inside this function so the snapshot brackets exactly the
    code under test. Only rows *created by this run* count: a leftover seed row
    from an earlier polluted run would otherwise be misreported as a fresh
    failure, hiding that the current probes are clean.
    """
    before = _live_seed_rows()
    test_bridge_lazy()
    test_single_module_identity()
    test_star_import_stays_clean()
    leaked = sorted(_live_seed_rows() - before)
    assert not leaked, (
        "探针写入了实机记忆库的种子档案，说明有子进程没钉 WATER_GROUP_HOME / "
        f"IDLEJOT_WG_MEMORY_DB: {leaked}"
    )


if __name__ == "__main__":
    test_no_bare_imports()
    print("✓ 无裸顶层导入（AST 全量扫描）")
    test_no_string_import()
    print("✓ 无字符串式 import_module 导入")
    # 三个子进程探针由 test_no_live_db_pollution 统一驱动：污染检查要把它们
    # 包在快照之间，才知道哪一行是本次写的。
    test_no_live_db_pollution()
    print("✓ 桥接包懒加载，不 eager-import bootstrap")
    print("✓ 干净子进程中两路径同一模块、单例唯一")
    print("✓ 星号导入命名空间干净")
    print("✓ 实机记忆库未被探针写入种子档案")
    print("PASS: water_group module identity suite passed.")
