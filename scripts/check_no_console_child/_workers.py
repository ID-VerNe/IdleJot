"""从运行树派生「后端在运行时拉起的 `scripts/` worker」范围（issue #329）。

## 为什么单独成模块

这份派生逻辑原本写在 `check_no_console_child.py` 里（issue #329 的实现），把那个
文件顶过了 1000 行红线（`check_module_size.py`，issue #11）。按 #169 PF-2 /
#283 / #285 的先例拆成包：入口名不变、对 `gates.json` 与 CI 透明，断言与正文
逐字保留，零重排。

## 它钉住什么

`RUNTIME_WORKERS` 是「已确认的快照」，**不是**事实来源：每次运行都由
`derive_workers()` 重读运行树，两个方向的差异都判红——

* 代码里起了一个没登记的 worker（issue #319 的病根换个时间复发）；
* 登记了却已经没人再起（范围里留个死文件，挡住真正的位置）；
* 交给 spawn 的 `scripts/` 路径静态求不出来（既不能确认已扫，也不能确认没扫）。

「静态求值」的口径写死在这里，不猜：只有裸的 `'scripts/x.py'`、
`ROOT_NAMES` 里的名字，以及字面量 `Path(__file__).resolve().parent.parent`
这一串，才解析成仓库根；别的前缀一律算「求不出来」并响亮报错。`glob` 模式
不是路径（它指不出具体某个 worker），`-m chatlocal.x` 也不是 worker（那个文件
本来就在 `SCAN_DIRS` 里）。

判据本身（该不该带窗口抑制标志）不在这里，留在 `check_no_console_child.py`。
"""
import ast
import re
import sys
from pathlib import Path, PurePosixPath

# `sys.path[0]` is `scripts/` when the gate runs as `python scripts/check_no_console_child.py`,
# but the repository root when it runs as `python -m scripts.check_no_console_child`. Both
# entry points are supported, so the path the callers depend on is pinned here, once,
# before the shim above it is imported (issue #329).
_SHIM = Path(__file__).resolve().parents[1]
if str(_SHIM) not in sys.path:
    sys.path.insert(0, str(_SHIM))

# ...with one exception the directory rule gets wrong (issue #319): a handful of
# scripts/ files are started *by the backend at runtime*, so they are runtime
# code that merely lives outside the packages. Scanning the whole directory
# would fire on dozens of deliberate diagnostic spawns; scanning none of it
# leaves these three unguarded for the day one of them shells out to ffmpeg.
#
# This is a *confirmed snapshot*, not the source of truth (issue #329):
# `derive_workers()` re-reads the runtime tree on every run and both directions
# of the difference fail. Adding a spawn of a new scripts/ worker to chatlocal/
# without adding it here is red; so is leaving a line here after the spawn moved
# or was deleted. Keep the trailing comment -- it is what a reviewer reads.
RUNTIME_WORKERS = frozenset({
    'scripts/parse_artifact.py',       # chatlocal/artifacts.py:294
    'scripts/list_client_accounts.py',  # chatlocal/client_accounts.py:39
    'scripts/voice_source.py',          # chatlocal/voice.py:189
})

# A `scripts/<name>.py` path is the only shape worth deriving (issue #329).
# `-m chatlocal.x` and `scripts/` diagnostic helpers are not workers: the former
# is already inside SCAN_DIRS, and the latter are not started by the backend.
WORKER_DIR = 'scripts'
WORKER_SUFFIXES = ('.py', '.pyw')

# `glob` patterns are not paths (`glob.glob('scripts/*.py')`); a name carrying
# one of these can only be reported as undecided, never registered.
_WILDCARDS = re.compile(r'[*?\[\]]')

# Names the packages publish for the repository root (`chatlocal/config.py:5`).
# A path that starts with one of these and then `scripts/<file>` names a worker;
# any other leading name is reported as undecided instead of guessed at.
ROOT_NAMES = frozenset({'ROOT'})

# Placeholder path part for the inline `Path(__file__).resolve().parent.parent`
# expression (see `_path_parts`); it can never be a real file name.
SELF_ROOT = '<self-root>'


def _path_parts(node):
    """The literal segments of a path expression, or None when not static.

    Recognises the shapes the runtime tree actually uses:
    `'scripts/x.py'`, `ROOT / 'scripts' / 'x.py'`, `ROOT/'scripts'/'x.py'`
    (parsed identically -- `/` is just `Div`), `os.path.join(...)`, and
    `str(...)` around any of them. Anything else (a Name, an f-string, a call
    to something unknown) returns None, which the caller reports as undecided.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _parts(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _path_parts(node.left)
        if left is None:
            # `ROOT / 'scripts/x.py'`: a name with a literal path under it. The
            # name is kept as an opaque part so `_worker_from_parts` can tell a
            # known root from an unknown one (issue #329).
            left = _bare_name(node.left)
            if left is None:
                return None
        right = _path_parts(node.right)
        if right is None:
            return None
        return left + right
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == 'str' and len(node.args) == 1):
        return _path_parts(node.args[0])
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'join'):
        parts = []
        for arg in node.args:
            piece = _path_parts(arg)
            if piece is None:
                return None
            parts.extend(piece)
        return parts
    if _is_path_of_file(node):
        # `Path(__file__).resolve().parent.parent` is the repository root; keep
        # it as a placeholder so `_worker_from_parts` can accept exactly this
        # shape and report every other leading expression as undecided.
        return [SELF_ROOT]
    return None


def _parts(text):
    """The path segments of a literal, `'scripts/x.py'` -> `['scripts', 'x.py']`.

    `PurePosixPath` rather than `re.split(r'[\\\\/]+')`: the repo's path rule is
    `Path(...).as_posix()` (AGENTS §5.1), so mixed separators fold here the same
    way they do for the runtime. `..` stays a segment rather than being
    resolved, and `_worker_from_parts` then refuses it as "not a plain
    `scripts/<name>`" instead of pointing the check at a different file.
    """
    return [part for part in PurePosixPath(text).parts if part]


def _bare_name(node):
    """The identifier of a `Name` node as a one-element path, else None."""
    if isinstance(node, ast.Name):
        return [node.id]
    return None


def _worker_from_parts(parts, roots):
    """`['scripts', 'x.py']` -> `'scripts/x.py'`; None if it is not a worker.

    Only two leading shapes resolve to the repository root (issue #329):
    nothing at all (`'scripts/x.py'`, the shape the daemon uses) and a name in
    `roots` -- or the literal `Path(__file__)...` expression itself, which is
    what `chatlocal/config.py:5` binds to `ROOT` and `chatlocal/artifacts.py`
    writes inline as `str(ROOT/'scripts/x.py')`, the same file. Any other prefix
    (a temp dir, a vendored tree, a variable) could name something else, so it
    returns None and the caller reports the site as undecided rather than
    guessing.
    """
    parts = list(parts)
    if 'scripts' not in parts:
        return None
    if any(_WILDCARDS.search(part) for part in parts):
        # `glob.glob('scripts/*.py')`: a pattern is not a path, so it names no
        # worker; the caller reports it as undecided (issue #329).
        return None
    at = parts.index('scripts')
    if at == 1 and parts[0] != SELF_ROOT and parts[0] not in roots:
        return None
    if at > 1:
        return None
    name = parts[at + 1] if len(parts) > at + 1 else ''
    if not name.endswith(WORKER_SUFFIXES) or len(parts) != at + 2:
        return None
    return WORKER_DIR + '/' + name


def derive_workers(sources):
    """(workers, undecided) harvested from the runtime tree's spawn sites.

    workers   -- {worker path: ['file:line', ...]} for every *statically
                 resolvable* `scripts/<name>.py` passed to a spawn
    undecided -- ['file:line', ...] for spawns that mention `scripts` in some
                 form that could not be evaluated statically

    Reading the tree instead of the whitelist is the whole point (issue #329):
    the whitelist is a snapshot a human confirmed, so the day a new worker
    appears the two disagree and the gate says so.

    Matching is deliberately wider than `spawn_calls`: the runtime tree passes
    its argv to `run_hidden` / `owned_popen` (and, in voice.py, to a local
    `_child` wrapper around them), so a direct-subprocess-only scan would find
    nothing at all and the diff would just say "every worker is unstarted".
    A call that hands a `scripts/<name>.py` path as an argument is a spawn site
    whichever helper it goes through -- that path is only ever an executable.
    """
    workers = {}
    undecided = set()
    for rel, text in sources:
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError:
            continue
        roots = _roots(tree)
        names = _string_constants(tree)
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            where = f'{rel}:{call.lineno}'
            resolved = False
            for arg in call.args:
                if isinstance(arg, ast.Starred):
                    continue
                # The argv list is the usual shape: `run_hidden([sys.executable,
                # str(ROOT/'scripts/x.py'), ...])`. Look inside it as well.
                pieces = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else (arg,)
                for piece in pieces:
                    if isinstance(piece, ast.Starred):
                        continue
                    parts = _path_parts(_substitute(piece, names))
                    if parts is None:
                        # Only an *unresolvable* mention of scripts/ is
                        # undecided; everything else is somebody's argument.
                        if _mentions_scripts(piece):
                            undecided.add(where)
                            resolved = True
                        continue
                    worker = _worker_from_parts(parts, roots)
                    if worker is not None:
                        if where not in workers.setdefault(worker, []):
                            workers[worker].append(where)
                        resolved = True
                    elif _mentions_scripts(piece):
                        # Static, but rooted somewhere that is not the
                        # repository -- say so instead of guessing.
                        undecided.add(where)
                        resolved = True
            if not resolved and _mentions_scripts(call):
                # The path reached the spawn some other way -- wrapped in
                # `str(...)`, joined, or passed to a helper that builds argv.
                undecided.add(where)
    return workers, sorted(undecided)


def _mentions_scripts(node):
    """Whether an expression shelters the word `scripts` anywhere inside it."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if 'scripts' in _parts(sub.value):
                return True
    return False


def _string_constants(tree):
    """{name: literal} for module-level `name = 'literal'` assignments.

    `name = 'scripts/x.py'` followed by `run_hidden(..., name)` is one hop away
    from a static worker and the tree does not write it that way today -- but a
    new worker may. Following the hop keeps the diff honest (issue #329): the
    name is then *resolvable*, so a missing registration fails loudly instead of
    landing in a pile of undecided sites nobody reads.
    """
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if not isinstance(target, ast.Name):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            found[target.id] = value.value
        # `name = os.path.join('scripts', 'x.py')` and friends.
        elif isinstance(value, ast.Call):
            parts = _path_parts(value)
            if parts:
                found[target.id] = '/'.join(parts)
    return found


def _substitute(node, names):
    """`Name` -> the literal it was bound to; every other node unchanged."""
    if isinstance(node, ast.Name) and node.id in names:
        return ast.Constant(value=names[node.id])
    return node


def _roots(tree):
    """Module-level names bound to the repository root -- issue #329's one hazard.

    Two shapes count, and both are really the repository root:

    * `ROOT = Path(__file__).resolve().parent.parent` -- `chatlocal/config.py:5`,
      the same value as this gate's `ROOT`;
    * `from .config import ROOT` -- how `chatlocal/artifacts.py:14`,
      `client_accounts.py:7` and `voice.py:11` get it.

    Any other leading name could be a temporary directory, a vendored tree or an
    unrelated variable; resolving it would invent a worker and skip a real one,
    so it stays undecided and the caller fails loudly.

    The imported form is taken at its word rather than chased through `.config`:
    a name that is not the root only ever lands on a `scripts/` path that does
    not exist, which `scan_scope()` already reports as a missing worker.
    """
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and _is_path_of_file(node.value):
                found.add(target.id)
        elif isinstance(node, ast.ImportFrom):
            # The root is the only path constant the packages publish by that
            # name; `index.py` and the like are never used as a path prefix.
            for alias in node.names:
                if alias.name in ROOT_NAMES:
                    found.add(alias.asname or alias.name)
    return frozenset(found)


def _is_path_of_file(node):
    """`Path(__file__)`, `.resolve()`, `.parent`, `.parent.parent` -- and nothing weird.

    `.resolve()` is a *call* on the path (`Path(__file__).resolve()`), while
    `.parent` is a plain attribute; both are accepted only with no arguments.
    """
    if isinstance(node, ast.Attribute):
        if node.attr == 'parent':
            return _is_path_of_file(node.value)
        return False
    if isinstance(node, ast.Call):
        if (isinstance(node.func, ast.Attribute) and node.func.attr == 'resolve'
                and not node.args and not node.keywords):
            return _is_path_of_file(node.func.value)
        return (_is_path_ctor(node.func) and not node.keywords
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == '__file__')
    return False


def _is_path_ctor(node):
    """`Path` -- imported under that name, or pathlib.Path spelled in full."""
    if isinstance(node, ast.Name):
        return node.id == 'Path'
    return (isinstance(node, ast.Attribute) and node.attr == 'Path'
            and isinstance(node.value, ast.Name) and node.value.id == 'pathlib')


def worker_scope(sources):
    """(unregistered, unstarted, undecided) -- issue #329's bidirectional diff.

    sources      -- [(relative path, text)] for the runtime tree, as handed over
                    by the gate's `read_runtime_sources()`; this module never
                    walks the tree itself, so "the runtime tree" keeps exactly
                    one definition (`SCAN_DIRS` + `SCAN_FILES`, issue #329)
    unregistered -- code starts this worker, RUNTIME_WORKERS does not list it:
                    it is runtime code sitting outside the scan, i.e. issue
                    #319's disease at a later date
    unstarted    -- RUNTIME_WORKERS lists it, no runtime spawn passes it any
                    more: the scan keeps a dead file alive and hides the real
                    owner of a moved spawn
    undecided    -- a spawn passes something script-shaped that cannot be
                    resolved; register the worker or make the path literal
    """
    derived, undecided = derive_workers(sources)
    unregistered = [(name, derived[name]) for name in sorted(derived)
                    if name not in RUNTIME_WORKERS]
    unstarted = sorted(name for name in RUNTIME_WORKERS if name not in derived)
    return unregistered, unstarted, undecided


# 派生范围（issue #329）的自测。每个片段是「运行树里的一个文件」，重点是
# `chatlocal/` 里的写法真的会被认出是 worker —— 直接 subprocess 扫描在这里
# 一无所获（实际代码都经 run_hidden / owned_popen / 本地 _child 转手）。
WORKER_SCOPE_CASES = (
    # (片段, 期望 workers, 期望 undecided 条数, 说明)
    ("import sys\nfrom .config import ROOT\nfrom .platform_process import run_hidden\n"
     "run_hidden([sys.executable, str(ROOT/'scripts/parse_artifact.py'), 'x'])\n",
     {'scripts/parse_artifact.py': 1}, 0, 'chatlocal 里 str(ROOT/...) 的 argv 列表'),
    ("import sys\nfrom pathlib import Path\n"
     "ROOT = Path(__file__).resolve().parent.parent\n"
     "run_hidden([sys.executable, ROOT / 'scripts' / 'x.py'])\n",
     {'scripts/x.py': 1}, 0, 'ROOT / "scripts" / "x.py" 逐段拼接'),
    ("import sys\nrun_hidden([sys.executable, 'scripts/x.pyw'])\n",
     {'scripts/x.pyw': 1}, 0, '相对仓库根的写法（daemon 侧的样子）'),
    ("import sys\nrun_hidden([sys.executable, '-m', 'chatlocal.investigation_sql'])\n",
     {}, 0, '-m chatlocal.x 不是 worker（它本来就在 SCAN_DIRS 里）'),
    ("import sys, tempfile\nfrom pathlib import Path\nTMP = Path(tempfile.mkdtemp())\n"
     "run_hidden([sys.executable, str(TMP/'scripts'/'x.py')])\n",
     {}, 1, '以未知根开头的路径必须报 undecided，不许猜成仓库内的 worker'),
    ("import sys, glob\nrun_hidden([sys.executable, glob.glob('scripts/*.py')[0]])\n",
     {}, 1, 'glob 出来的路径无法静态求值，必须报 undecided'),
    ("import sys\nname = 'scripts/x.py'\nrun_hidden([sys.executable, name])\n",
     {'scripts/x.py': 1}, 0, '模块级字面量变量：跟一跳即可静态求值，不许当成 undecided 埋掉'),
    ("import sys\nname = pick()\nrun_hidden([sys.executable, 'scripts/' + name])\n",
     {}, 1, '拼接不可求值的变量：必须报 undecided，不许猜成仓库内的 worker'),
)


def self_test_worker_scope(runtime_sources):
    """派生逻辑的四件事：认得出、不猜、双向对账、-m 不算 worker。

    `runtime_sources` 是真树的 `[(rel, text)]`，由入口传进来：本模块不自己走树，
    「哪些文件算运行树」只在 `check_no_console_child/__init__.py` 定义一次。
    """
    for source, expected, undecided_n, why in WORKER_SCOPE_CASES:
        workers, undecided = derive_workers([('chatlocal/site.py', source)])
        got = {name: len(sites) for name, sites in workers.items()}
        if got != expected or len(undecided) != undecided_n:
            raise AssertionError(
                f'检测器自测失败（{why}）：期望 {expected} / {undecided_n} 条 undecided，'
                f'实得 {got} / {undecided}')
    # 双向对账：代码里起了一个没登记的 worker 必须红（issue #329 的病根），
    # 登记了却没人再起的也必须红（否则扫描范围会留一个死文件挡住真正的位置）
    site = [('chatlocal/site.py',
             "import sys\nfrom .config import ROOT\n"
             "from .platform_process import run_hidden\n"
             "run_hidden([sys.executable, str(ROOT/'scripts/new_worker.py')])\n")]
    global RUNTIME_WORKERS
    saved = RUNTIME_WORKERS
    try:
        RUNTIME_WORKERS = frozenset()
        unregistered, unstarted, _ = worker_scope(site)
        if [name for name, _ in unregistered] != ['scripts/new_worker.py'] or unstarted:
            raise AssertionError('检测器自测失败：未登记的运行时 worker 必须被判红（issue #329）')
        RUNTIME_WORKERS = frozenset({'scripts/new_worker.py'})
        if worker_scope(site) != ([], [], []):
            raise AssertionError('检测器自测失败：登记之后同一处 spawn 必须放行')
        RUNTIME_WORKERS = frozenset({'scripts/new_worker.py', 'scripts/gone.py'})
        unregistered, unstarted, _ = worker_scope(site)
        if unregistered or unstarted != ['scripts/gone.py']:
            raise AssertionError('检测器自测失败：登记了却没人起 worker 必须被判红')
    finally:
        RUNTIME_WORKERS = saved
    # 真树上的 spawn 点必须真的被认出来，否则「两边都为空」只是空对空。
    # 这里只钉「认得出真代码」这一件事：拿 RUNTIME_WORKERS 当基准的话，登记
    # 已经失效时自测会先抛 AssertionError，真正该读的失败报告反而被吞掉。
    # 运行树由调用方读进来（`read_runtime_sources()` 在本模块之外），自测不绕过它。
    real, _ = derive_workers(runtime_sources)
    if not real:
        raise AssertionError('检测器自测失败：运行树里一处 scripts/ worker 都没认出来')
    return True
