"""Smoke-test every water-group daemon entry the way the supervisor launches it.

The supervisor runs each daemon as `python water-group/daemon/<name>.py`, so
`sys.path[0]` is the script's own directory and the repo root is only present if
the script puts it there itself. `check_water_group_import.py` imports the bridge
package from the repo root in-process, which never exercises that launch shape --
that blind spot is how watchdog.py shipped with `sys.path.insert(0, ROOT /
"water-group")` and raised ModuleNotFoundError on every restart for two days.

Each entry runs in its own `python -P` subprocess. The module body executes under
a non-__main__ run name, so no daemon loop starts; then every `water_group` import
the script defers into a function is replayed the way that function does it --
skipping the ones that first put the repo root on sys.path themselves, since
replaying those out of context would fail for the wrong reason.
"""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import check_fixture_support as fs  # noqa: E402  (issue #169 PE: path 注入收口)

ENTRY_DIR = ROOT / "water-group" / "daemon"

# `python <script>` puts the script's directory at sys.path[0]; `-c` would not, and
# `-P` drops the cwd entry, so the child recreates that one entry explicitly.
CHILD = """
import importlib, json, os, runpy, sys
target = sys.argv[1]
sys.path.insert(0, os.path.dirname(os.path.abspath(target)))
runpy.run_path(target, run_name="__entry_smoke__")
for spec in json.loads(sys.argv[2]):
    if spec["kind"] == "from":
        pkg = importlib.import_module(spec["module"])
        for alias in spec["names"]:
            getattr(pkg, alias)
    else:
        importlib.import_module(spec["module"])
"""


def _is_path_insert(node):
    """`sys.path.insert(...)` or `sys.path.append(...)`."""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"insert", "append"}
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "path"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "sys")


def _water_group_spec(node):
    """The import form as a replayable spec, or None when it is not ours."""
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module != "water_group" and not module.startswith("water_group."):
            return None
        return {"kind": "from", "module": module,
                "names": sorted(alias.name for alias in node.names)}
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names
                 if alias.name == "water_group" or alias.name.startswith("water_group.")]
        return [{"kind": "import", "module": name} for name in names] or None
    return None


def deferred_imports(source):
    """water_group imports a function performs after its own sys.path setup.

    Module-level imports already ran during run_path. A function that inserts the
    repo root itself (reply.dossier_matches, dossier.digest) owns its path and is
    left alone: replaying its import from the module body would fail for reasons
    the real call never hits.
    """
    specs = []
    for scope in ast.walk(ast.parse(source)):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        inserts = [node.lineno for node in ast.walk(scope) if _is_path_insert(node)]
        for node in ast.walk(scope):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if any(line < node.lineno for line in inserts):
                continue
            found = _water_group_spec(node)
            if isinstance(found, list):
                specs.extend(found)
            elif found:
                specs.append(found)
    return specs


def check_entry(script):
    specs = deferred_imports(script.read_text(encoding="utf-8"))
    # `-P` 是解释器开关，但排在 `-c` 之前时由 CPython 照常识别，可以当子进程 argv
    # 传给 run_python。入口都会 ensure_utf8_stdio，解码钉在 run_python 里（issue #240）。
    proc = fs.run_python(
        ["-P", "-c", CHILD, str(script), json.dumps(specs)],
        cwd=ROOT, timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return f"{script.name}: {detail[-1] if detail else 'exit ' + str(proc.returncode)}"
    return None


def test_entry_smoke():
    entries = sorted(ENTRY_DIR.glob("*.py"))
    assert entries, f"no daemon entries found under {ENTRY_DIR}"

    failures = [msg for msg in (check_entry(script) for script in entries) if msg]
    assert not failures, "daemon entry import failed:\n  " + "\n  ".join(failures)

    print(f"✓ {len(entries)} daemon entries import cleanly under script launch")


if __name__ == "__main__":
    test_entry_smoke()
    print("PASS: daemon entry smoke suite passed.")
