"""Path hygiene gate: runtime data must not live under the repo root.

issue #14's root cause: `5a12998` ("move local data to ~/.water-group, keep only
code in the repo") migrated state/logs but missed 6 DB paths, so one logical
store split into two physical files -- `reply.py` read the repo copy while the
backend wrote the home copy, and `reply.py --context` showed a two-hour-old
snapshot. `memory_engine.py` did not even read an environment variable, so test
fixtures wrote straight into the live memory database.

This gate statically scans source and asserts four things:

1. No SQLite/diagnostic path is derived from the repo root. It must go through
   `wg_paths.data_dir()` or `chatlocal.config.DATA`.
2. `memory_engine.py` resolves its data root from `WATER_GROUP_HOME`.
3. The four daemons that read `mcp-access.sqlite3` import `data_dir`.
4. `chatlocal/reply_timing.py` writes under `chatlocal.config.DATA`.

Paths that intentionally live in the repo are listed in `INTENTIONAL` below.

    .venv/Scripts/python.exe scripts/check_path_hygiene.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Deliberately anchored in the repo -- these are not runtime data:
#   .cache    RapidOCR / voice model cache (check_publication allows it)
#   imports   where users drop export files (config.py:76 points at it)
#   .tmp      transient scratch for legacy test scripts
#   .env      local configuration
INTENTIONAL = {'.cache', 'imports', '.tmp', '.env'}

# Packages that may touch runtime data. scripts/ are tests and use temp dirs.
SCAN_DIRS = ('chatlocal', 'water-group', 'water_group')

# Repo-root path derivations: ROOT / "data", parents[1] / 'data', ...
ROOT_EXPR = (r'(?:ROOT|PROJECT_ROOT|ROOT_DIR|HERE\.parents\[\d+\]'
             r'|__file__[^,\n]*parents\[\d+\])')
DATA_DIRS = r'(?:data|diagnostics|logs|state)'

DERIVE_RE = re.compile(
    rf'{ROOT_EXPR}\s*/\s*[\'"]({DATA_DIRS})(?:[/\\\\][^\'"]*)?[\'"]')

DAEMONS = ('reply.py', 'followup.py', 'dossier.py', 'mint_grant.py')


def _iter_sources():
    for folder in SCAN_DIRS:
        base = ROOT / folder
        if not base.exists():
            continue
        for path in sorted(base.rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            yield path


def scan_derived_paths():
    """Return [(path, lineno, line)] for repo-rooted runtime data paths."""
    hits = []
    for path in _iter_sources():
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(lines, 1):
            if line.strip().startswith('#'):
                continue
            match = DERIVE_RE.search(line)
            if not match or match.group(1) in INTENTIONAL:
                continue
            hits.append((path, lineno, line.strip()))
    return hits


def scan_memory_engine_root():
    """`memory_engine.py` must resolve its data root from the environment."""
    path = ROOT / 'water-group' / 'memory_engine.py'
    if not path.exists():
        return ['water-group/memory_engine.py 缺失']
    text = path.read_text(encoding='utf-8')
    problems = []
    if 'WATER_GROUP_HOME' not in text:
        problems.append(
            'water-group/memory_engine.py 的数据根没读 WATER_GROUP_HOME '
            '——测试隔离会失效，fixture 会写进生产记忆库')
    if re.search(r'DEFAULT_DB_PATH\s*=\s*PROJECT_ROOT\s*/', text):
        problems.append(
            'water-group/memory_engine.py 的 DEFAULT_DB_PATH 仍从仓库根推导')
    return problems


def scan_daemon_db_paths():
    """The daemons that read mcp-access.sqlite3 must go through data_dir()."""
    problems = []
    for name in DAEMONS:
        path = ROOT / 'water-group' / 'daemon' / name
        if not path.exists():
            problems.append(f'water-group/daemon/{name} 缺失')
            continue
        text = path.read_text(encoding='utf-8')
        if 'data_dir' not in text:
            problems.append(
                f'water-group/daemon/{name} 没 import data_dir —— DB 仍可能指向仓库')
        for match in re.finditer(r'^DB\s*=.*$', text, re.MULTILINE):
            line = match.group(0)
            if 'data_dir()' not in line and 'environ' not in line:
                problems.append(
                    f'water-group/daemon/{name}: DB 既不走 data_dir() 也不读环境变量')
    return problems


def scan_reply_timing():
    """reply_timing writes a diagnostics file; it must not use a repo path."""
    path = ROOT / 'chatlocal' / 'reply_timing.py'
    if not path.exists():
        return ['chatlocal/reply_timing.py 缺失']
    text = path.read_text(encoding='utf-8')
    if re.search(r'PATH\s*=\s*Path\(__file__\)', text):
        return ['chatlocal/reply_timing.py 的 PATH 仍从 __file__ 推导']
    if 'DATA' not in text:
        return ['chatlocal/reply_timing.py 没走 chatlocal.config.DATA']
    return []


def scan_isolated_env():
    """`isolated_env()` must always pin the data root.

    The pin must also live at **module level**, before any `patch.dict` context:
    the data root is resolved at import time and event handling runs on a
    background thread, so a context-manager-only pin is escaped by the very
    suites it protects (issue #89, second instance of the issue #79 root cause).
    """
    path = ROOT / 'scripts' / 'check_fixture_support.py'
    if not path.exists():
        return ['scripts/check_fixture_support.py 缺失']
    text = path.read_text(encoding='utf-8')
    if 'WATER_GROUP_HOME' not in text:
        return ['scripts/check_fixture_support.py 的 isolated_env() 不钉 WATER_GROUP_HOME'
                ' —— fixture 会落进真实 ~/.water-group']
    if not re.search(r"^os\.environ\[['\"]WATER_GROUP_HOME['\"]\]\s*=", text, re.MULTILINE):
        return ['scripts/check_fixture_support.py 没有在模块顶层钉 WATER_GROUP_HOME'
                ' —— patch.dict 退出后环境会恢复成实机根，后台线程会连上实机库（issue #89）']
    return []


# --- manual separator handling (issue #13) -----------------------------------
#
# `pathlib` is the repo's established style (205/282 files). What is left is not
# "string joins" but ad-hoc separator surgery: `replace('\\', '/')` to build a
# stable POSIX key, `os.sep` to name a cache file, `rstrip('\\/') + '\\'` to test
# a path prefix. Each of those encodes an assumption about which separator the
# input uses, and silently produces the wrong answer when it does not hold.

# `str(x).replace('\\', '/')` and friends.
SEP_REPLACE_RE = re.compile(
    r"""replace\(\s*(['"])\\(?:\1)\s*,\s*(['"])/\2\s*\)""")
OS_SEP_RE = re.compile(r'\bos\.sep\b')
OS_PATH_JOIN_RE = re.compile(r'\bos\.path\.join\b')

# Files exempt because the separator is genuinely part of the data:
#   up.py            `[\\/]` regexes match WMI command lines, where both forms occur
#   commands.py      `rstrip('/') + '/send_group_msg'` builds a URL, not a path
#   wg_paths.py      the single source of truth for the data root
#   check_path_hygiene.py  the detector's own patterns and self-test samples
MANUAL_SEP_EXEMPT = {
    'water-group/daemon/up.py',
    'water-group/daemon/commands.py',
    'water-group/daemon/wg_paths.py',
    'scripts/check_path_hygiene.py',
}


def _iter_scan_sources():
    """Every Python file in the packages and in scripts/."""
    for folder in SCAN_DIRS:
        base = ROOT / folder
        if not base.exists():
            continue
        for path in sorted(base.rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            yield path
    for path in sorted((ROOT / 'scripts').glob('*.py')):
        yield path


def scan_manual_separators():
    """Return [(path, lineno, kind, line)] for ad-hoc separator handling."""
    hits = []
    for path in _iter_scan_sources():
        rel = path.relative_to(ROOT).as_posix()
        if rel in MANUAL_SEP_EXEMPT:
            continue
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(lines, 1):
            if line.strip().startswith('#'):
                continue
            if SEP_REPLACE_RE.search(line):
                hits.append((path, lineno, "replace('\\\\','/')", line.strip()))
            elif OS_SEP_RE.search(line):
                hits.append((path, lineno, 'os.sep', line.strip()))
            elif OS_PATH_JOIN_RE.search(line):
                hits.append((path, lineno, 'os.path.join', line.strip()))
    return hits


def scan_stale_exemptions():
    """An exemption for a file that no longer exists is dead weight.

    Exemptions exist to document a deliberate exception, not to hide files that
    were deleted. Without this check a stale entry silently narrows the scan.
    """
    stale = [rel for rel in sorted(MANUAL_SEP_EXEMPT) if not (ROOT / rel).exists()]
    return stale


def self_test():
    """Prove the detectors fire on the exact shape of the original bug."""
    samples = [
        'DB = ROOT / "data" / "mcp-access.sqlite3"',
        "PATH = Path(__file__).resolve().parents[1] / 'data/diagnostics/reply-timing.jsonl'",
        'DB = HERE.parents[2] / "data" / "mcp-access.sqlite3"',
    ]
    for sample in samples:
        if not DERIVE_RE.search(sample):
            raise AssertionError(f'检测器漏掉了这个写法: {sample}')
    clean = [
        'DB = data_dir() / "mcp-access.sqlite3"',
        'PATH = DATA / "diagnostics" / "reply-timing.jsonl"',
        'ROOT / ".cache"',  # intentional
        'ROOT / "water-group" / "config.json"',  # config, not runtime data
    ]
    for sample in clean:
        match = DERIVE_RE.search(sample)
        if match and match.group(1) not in INTENTIONAL:
            raise AssertionError(f'检测器误报了这个写法: {sample}')
    _self_test_separators()
    return True


def _self_test_separators():
    """Separator detector: must fire on issue #13 shapes, never on pathlib."""
    BS = chr(92)
    flagged = [
        f"key = str(raw['source_db']).replace('{BS}', '/')",
        f'name = value.replace("{BS}", "/")',
        "name = value.replace( '%s', \"/\" )" % BS,
        "cache = ROOT / ('x' + os.sep + 'y')",
        "db = os.path.join(ROOT, 'data')",
    ]
    for sample in flagged:
        if not (SEP_REPLACE_RE.search(sample) or OS_SEP_RE.search(sample)
                or OS_PATH_JOIN_RE.search(sample)):
            raise AssertionError(f'分隔符检测器漏掉了这个写法: {sample}')
    clean = [
        "key = path.relative_to(root).as_posix()",
        "key = PureWindowsPath(value).as_posix()",
        "prefix = PureWindowsPath(exe).is_relative_to(PureWindowsPath(str(root)))",
        "db = data_dir() / 'mcp-access.sqlite3'",
        "cache = ROOT / '.cache'",
    ]
    for sample in clean:
        if SEP_REPLACE_RE.search(sample) or OS_SEP_RE.search(sample) or OS_PATH_JOIN_RE.search(sample):
            raise AssertionError(f'分隔符检测器误报了这个写法: {sample}')
    return True


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    self_test()

    failures = []

    hits = scan_derived_paths()
    if hits:
        failures.append('运行时数据路径仍从仓库根推导：')
        for path, lineno, line in hits:
            failures.append(f'  {path.relative_to(ROOT)}:{lineno}  {line}')

    failures.extend(scan_memory_engine_root())
    failures.extend(scan_daemon_db_paths())
    failures.extend(scan_reply_timing())
    failures.extend(scan_isolated_env())

    sep_hits = scan_manual_separators()
    if sep_hits:
        failures.append('仍有手工分隔符处理（应改用 pathlib）：')
        for path, lineno, kind, line in sep_hits:
            failures.append(f'  {path.relative_to(ROOT).as_posix()}:{lineno}  [{kind}]  {line}')

    stale = scan_stale_exemptions()
    if stale:
        failures.append('MANUAL_SEP_EXEMPT 里有已不存在的文件，请一并删掉这些条目：')
        failures.extend(f'  {rel}' for rel in stale)

    if failures:
        print('FAIL: 路径卫生检查未通过\n')
        for line in failures:
            print(line)
        print('\n运行时数据必须落在 WATER_GROUP_HOME（默认 ~/.water-group）：')
        print('  daemon 脚本    from wg_paths import data_dir')
        print('  chatlocal 包   from .config import DATA')
        print('  memory_engine  复述 WATER_GROUP_HOME 规则（不能 flat import wg_paths）')
        print('  路径分隔符     Path(...).as_posix() / PureWindowsPath(...).is_relative_to()')
        return 1

    print('✓ 检测器自测通过（能抓住 issue #14 的原始写法）')
    print('✓ 没有运行时数据路径从仓库根推导')
    print('✓ memory_engine 数据根读 WATER_GROUP_HOME')
    print('✓ 4 个 daemon 的 DB 走 data_dir()')
    print('✓ reply_timing 走 chatlocal.config.DATA')
    print('✓ isolated_env() 钉住 WATER_GROUP_HOME')
    print('✓ 没有残留的手工分隔符处理（replace/os.sep/os.path.join）')
    print('\nPASS: 路径卫生门禁通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
