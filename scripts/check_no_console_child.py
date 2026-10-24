"""Console-window gate: no runtime spawn may give its child a console window.

issue #39: a console-less parent (pythonw, DETACHED_PROCESS, CREATE_NO_WINDOW)
that starts a *console* child without CREATE_NO_WINDOW hands that child a brand
new console -- the black box that flashes on the desktop for a few hundred
milliseconds. It is not reproducible by reading code, only by a window hook, so
the only durable defence is a static rule plus one place that spells the flag.

This gate asserts six things:

1. Every `subprocess.run/Popen/call/check_output/check_call` in the runtime tree
   (`water-group/`, its `water_group/` bridge, `chatlocal/`, `server_wg.py`)
   names a window-suppressing flag in `creationflags` -- `NO_WINDOW` or
   `CREATE_NO_WINDOW`, not merely "some integer". `creationflags=0` and
   `creationflags=CREATE_NEW_PROCESS_GROUP` fail: neither touches the console.
   Spawn through `winconsole.run_hidden` / `popen_hidden` (daemons) or
   `platform_process.run_hidden` / `owned_popen` (backend) and it is satisfied
   by construction.
2. A POSIX-only branch may opt out with `# console-gate: posix-only` on the call
   or on the line above it. Exemptions are checked for staleness: a marker that
   no longer sits on an un-flagged spawn fails, so dead exemptions cannot pile up.
3. `CREATE_NO_WINDOW` is spelled in exactly two modules -- those two helpers.
   Five copies of `getattr(subprocess, 'CREATE_NO_WINDOW', 0)` is how the last
   call site (voice.py) ended up without it. The scan reads the AST, so a
   docstring or a comment may still explain the flag (issue #91 needed exactly
   that); an attribute access, a `getattr(..., 'CREATE_NO_WINDOW')` or a
   from-import is what fails. The same constant written as the literal
   `0x08000000` would slip past an identifier rule, so it is scanned for
   separately as text.
4. `DETACHED_PROCESS` names are banned from runtime code, `winconsole.py`
   excepted (it strips the flag, so it has to mention it). It looks like a
   stronger window suppressor and is the opposite: the child gets no console, so
   a console program allocates a visible one. Only `winconsole.py` knows this is
   about flags rather than text -- docstrings elsewhere are free to explain it,
   which is why this rule reads the AST instead of grep.
5. Both helpers really work: a child runs and its exit code survives.
6. `run_passthrough` delivers the child's output to a caller that has no console
   of its own. Same flag as rule 1, opposite consequence: with CREATE_NO_WINDOW
   the child's stdout points at its new console, so inheriting it drops whatever
   the child printed (issue #91 -- `listen.py` reported a bare `WAKE` four times
   in a row).

    uv run python scripts/check_no_console_child.py
"""
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SPAWNERS = frozenset({'run', 'Popen', 'call', 'check_output', 'check_call'})

# Runtime spawn sites only. scripts/ is excluded on purpose: those are CI and
# manual diagnostics, and a console window during a manual run is not a bug.
SCAN_DIRS = ('water-group', 'water_group', 'chatlocal')
SCAN_FILES = ('server_wg.py',)
SUFFIXES = frozenset({'.py', '.pyw'})

# The only two modules allowed to name the flag; everything else goes through them.
FLAG_OWNERS = frozenset({'water-group/daemon/winconsole.py',
                         'chatlocal/platform_process.py'})

# DETACHED_PROCESS is stripped, not applied, so only the stripper may name it.
DETACHED_OWNERS = frozenset({'water-group/daemon/winconsole.py'})

# `0x08000000` is CREATE_NO_WINDOW spelled as a number; it would dodge rule 3.
NUM_NO_WINDOW = re.compile(r'0[xX]0*8000000\b')

MARKER = 'console-gate: posix-only'


def iter_sources():
    for folder in SCAN_DIRS:
        base = ROOT / folder
        if not base.exists():
            continue
        for path in sorted(base.rglob('*')):
            if path.suffix in SUFFIXES and '__pycache__' not in path.parts:
                yield path
    for name in SCAN_FILES:
        path = ROOT / name
        if path.exists():
            yield path


def spawn_calls(tree):
    """Every call that starts a process through the subprocess module.

    Aliases are resolved from the file's own imports, so `import subprocess as sp`
    and `from subprocess import run` are caught too -- an alias is a cheap way to
    slip past a regex gate.
    """
    modules = set()
    bare = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == 'subprocess':
                    modules.add(alias.asname or 'subprocess')
        elif isinstance(node, ast.ImportFrom) and node.module == 'subprocess':
            for alias in node.names:
                if alias.name in SPAWNERS:
                    bare.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr in SPAWNERS
                and isinstance(func.value, ast.Name) and func.value.id in modules):
            yield node
        elif isinstance(func, ast.Name) and func.id in bare:
            yield node


def _flag_names(node):
    """Every identifier spelling inside a creationflags expression.

    Names only, so `subprocess.CREATE_NEW_PROCESS_GROUP | winconsole.NO_WINDOW`
    yields both halves and an alias (`from subprocess import CREATE_NO_WINDOW`)
    still shows up as a Name.
    """
    names = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.append(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.append(sub.attr)
    return names


def flag_verdict(call):
    """Classify a spawn by what its creationflags do to the console window.

    'ok'       -- names NO_WINDOW: the child gets a console with no window, and
                  its own children inherit that, so grandchildren stay silent.
    'detached' -- names DETACHED_PROCESS: the opposite of a suppressor, see the
                  module docstring.
    'missing'  -- anything else (`0`, only CREATE_NEW_PROCESS_GROUP) or absent.
                  An integer that is not one of our names proves nothing: that is
                  how `NEW_PROCESS_GROUP | DETACHED` satisfied the old check.
    """
    for kw in call.keywords:
        if kw.arg != 'creationflags':
            continue
        names = _flag_names(kw.value)
        if any('DETACHED' in name for name in names):
            return 'detached', names
        if any(name.endswith('NO_WINDOW') for name in names):
            return 'ok', names
        return 'missing', names
    return 'missing', []


def detached_uses(tree, filename):
    """Line numbers where runtime code *names* DETACHED_PROCESS as a value.

    Docstrings and comments are exempt by construction: the former are
    `ast.Constant`, the latter never reach the AST. What is left is code that
    would actually hand the flag to something -- including a call to one of our
    helpers, which `spawn_calls` cannot see.
    """
    if filename in DETACHED_OWNERS:
        return []
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and 'DETACHED' in node.attr:
            lines.append(node.lineno)
        elif isinstance(node, ast.Name) and 'DETACHED' in node.id:
            lines.append(node.lineno)
    return sorted(set(lines))


def marker_positions(lines, start, end):
    """1-based line numbers of an exemption marker for this call, if any.

    Accepted on any line of the statement or on the line directly above it: the
    call itself is frequently too long to carry a trailing comment.
    """
    found = []
    for lineno in range(start, end + 1):
        if MARKER in lines[lineno - 1]:
            found.append(lineno)
    if start >= 2 and MARKER in lines[start - 2]:
        found.append(start - 1)
    return found


def scan_source(text, filename='<memory>'):
    """Return (violations, stale_markers, flagged) for one source text.

    violations    -- [(lineno, line, why)] spawns that would open a window
    stale_markers -- [(lineno, line)] exemption comments with nothing to exempt
    flagged       -- count of spawns that do carry creationflags
    """
    tree = ast.parse(text, filename=filename)
    lines = text.splitlines()
    # Rule 1 does not apply inside the two helper modules: they are the ones that
    # apply the flag, through `**_with_no_window(kwargs)`, which no static scan can
    # see through. Rule 3 pins the constant to those files instead.
    trusted = filename in FLAG_OWNERS
    violations = []
    exempt = set()
    flagged = 0
    for call in spawn_calls(tree):
        start = call.lineno
        end = getattr(call, 'end_lineno', start)
        verdict, _ = flag_verdict(call)
        if trusted or verdict == 'ok':
            flagged += 1
            continue
        positions = marker_positions(lines, start, end)
        if positions:
            exempt.update(positions)
            continue
        why = ('DETACHED_PROCESS 不是窗口抑制器：子进程没有控制台，'
               '控制台程序会自己开一个可见的' if verdict == 'detached'
               else 'subprocess call without a window-suppressing creationflags')
        violations.append((start, lines[start - 1].strip(), why))
    reported = {item[0] for item in violations}
    for lineno in detached_uses(tree, filename):
        if lineno in reported:
            continue
        violations.append((lineno, lines[lineno - 1].strip(),
                           'DETACHED_PROCESS 不是窗口抑制器，运行时树里禁止出现'))
    violations.sort(key=lambda item: item[0])
    stale = [(n, lines[n - 1].strip()) for n in range(1, len(lines) + 1)
             if MARKER in lines[n - 1] and n not in exempt]
    return violations, stale, flagged


def flag_spellings(tree):
    """Line numbers where runtime code spells `CREATE_NO_WINDOW` as a value.

    Docstrings and comments are exempt by construction, exactly like
    `detached_uses`: a docstring is an `ast.Constant` that no call consumes, and
    a comment never reaches the AST. That distinction is what lets the call sites
    explain *why* inheriting stdout is not enough (issue #91) without tripping
    the gate that keeps the constant in two modules.
    """
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == 'CREATE_NO_WINDOW':
            lines.add(node.lineno)
        elif isinstance(node, ast.Name) and node.id == 'CREATE_NO_WINDOW':
            lines.add(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module == 'subprocess':
            for alias in node.names:
                if alias.name == 'CREATE_NO_WINDOW':
                    lines.add(node.lineno)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in {'getattr', 'setattr'} and len(node.args) > 1):
            name = node.args[1]
            if isinstance(name, ast.Constant) and name.value == 'CREATE_NO_WINDOW':
                lines.add(name.lineno)
    return sorted(lines)


def scan_flag_owners():
    """`CREATE_NO_WINDOW` must not be re-spelled in runtime code outside the helpers."""
    hits = []
    for path in iter_sources():
        rel = path.relative_to(ROOT).as_posix()
        if rel in FLAG_OWNERS:
            continue
        try:
            text = path.read_text(encoding='utf-8')
            tree = ast.parse(text)
        except (OSError, UnicodeDecodeError, SyntaxError):
            # A parse failure is already reported by scan_source; do not double it.
            continue
        lines = text.splitlines()
        for lineno in flag_spellings(tree):
            line = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ''
            hits.append((rel, lineno, line))
    return hits


def scan_numeric_flags():
    """`0x08000000` is CREATE_NO_WINDOW written as a number, and rule 3 is text."""
    hits = []
    for path in iter_sources():
        rel = path.relative_to(ROOT).as_posix()
        if rel in FLAG_OWNERS:
            continue
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if NUM_NO_WINDOW.search(stripped) and not stripped.startswith('#'):
                hits.append((rel, lineno, stripped))
    return hits


def check_helpers():
    """Run both helpers for real; a helper that cannot start a child is no fix."""
    failures = []
    sys.path.insert(0, str(ROOT / 'water-group' / 'daemon'))
    import winconsole

    result = winconsole.run_hidden([sys.executable, '-c', 'print("ok")'],
                                   capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or result.stdout.strip() != 'ok':
        failures.append('winconsole.run_hidden 未能正常跑完子进程')

    result = winconsole.run_hidden([sys.executable, '-c', 'print("ok")'],
                                   capture_output=True, text=True, timeout=60,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    if result.returncode != 0 or result.stdout.strip() != 'ok':
        failures.append('winconsole.run_hidden 在调用方自带 creationflags 时失效')

    no_window = getattr(winconsole, '_with_no_window', None)
    if no_window is None:
        failures.append('winconsole._with_no_window 不见了：附加 flag 的合并规则无法校验')
    else:
        merged = no_window({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP})['creationflags']
        if merged != subprocess.CREATE_NEW_PROCESS_GROUP | winconsole.NO_WINDOW:
            failures.append('winconsole._with_no_window 丢了调用方的 CREATE_NEW_PROCESS_GROUP')
        # DETACHED is not a window suppressor: Windows ignores NO_WINDOW when both
        # are set, so keeping it would silently restore the flashing black box
        # (issue #39's residue, measured 2026-10-23).
        detached = no_window({'creationflags': subprocess.DETACHED_PROCESS})['creationflags']
        if detached & subprocess.DETACHED_PROCESS:
            failures.append('winconsole._with_no_window 保留了 DETACHED_PROCESS：'
                            'NO_WINDOW 与它同用时会被 Windows 忽略')
        if not detached & winconsole.NO_WINDOW:
            failures.append('winconsole._with_no_window 没把 DETACHED_PROCESS 换成 NO_WINDOW')

    process = winconsole.popen_hidden([sys.executable, '-c', 'import sys; sys.exit(3)'])
    if process.wait(timeout=60) != 3:
        failures.append('winconsole.popen_hidden 的子进程退出码没有保留')

    sys.path.insert(0, str(ROOT))
    from chatlocal.platform_process import owned_popen, run_hidden
    result = run_hidden([sys.executable, '-c', 'print("ok")'],
                        capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or result.stdout.strip() != 'ok':
        failures.append('chatlocal.platform_process.run_hidden 未能正常跑完子进程')
    process = owned_popen([sys.executable, '-c', 'import sys; sys.exit(4)'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if process.wait(timeout=60) != 4:
        failures.append('chatlocal.platform_process.owned_popen 的子进程退出码没有保留')
    return failures


# `pythonw.exe` has no console at all -- the shape `listen.py` runs in when an
# agent starts it in the background. The probe prints a marker from a child
# spawned through `run_passthrough`; if the marker does not come back on our
# pipe, the relay is not relaying.
PASSTHROUGH_PROBE = """
import sys
sys.path.insert(0, {daemon!r})
import winconsole
rc = winconsole.run_passthrough([{python!r}, '-c', 'print("passthrough-visible")'])
print('child-rc', rc)
"""


def check_passthrough_visible():
    """Rule 6: a console-less caller must still see the relayed child's output."""
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if pythonw.exists():
        # A GUI-subsystem parent gets no console and inherits none: the cheapest
        # way to reproduce the exact condition from the issue.
        parent, extra = [str(pythonw)], {}
    else:
        # No pythonw (a bare interpreter install): DETACHED_PROCESS also yields a
        # parent without a console. Measured: the same inherited-stdout child
        # prints nothing under either shape.
        parent, extra = [sys.executable], {'creationflags': subprocess.DETACHED_PROCESS}
    probe = PASSTHROUGH_PROBE.format(daemon=str(ROOT / 'water-group' / 'daemon'),
                                     python=sys.executable)
    try:
        result = subprocess.run([*parent, '-c', probe],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace',
                                timeout=120, env=dict(os.environ, PYTHONUTF8='1'),
                                **extra)
    except (OSError, subprocess.SubprocessError) as exc:
        return [f'无控制台父进程的透传探针跑不起来（{exc}）']
    out = result.stdout or ''
    failures = []
    if result.returncode != 0:
        failures.append(f'透传探针退出码 {result.returncode}：{out.strip()[:300]}')
    if 'passthrough-visible' not in out:
        failures.append('winconsole.run_passthrough 的子进程输出没有到无控制台调用方'
                        '（issue #91：继承 stdout 在这个形状下是空的）')
    if 'child-rc 0' not in out:
        failures.append('winconsole.run_passthrough 没有把子进程退出码交回调用方')
    return failures


SELF_TEST_CASES = (
    ('import subprocess\nsubprocess.run(["x"])\n', 1, '未带 flag 的直调必须被抓'),
    ('import subprocess\nsubprocess.run(["x"], creationflags=1)\n', 1,
     '任意整数不算抑制窗口'),
    ('import subprocess\nsubprocess.run(["x"], creationflags=0)\n', 1,
     'creationflags=0 不算抑制窗口'),
    ('import subprocess\nsubprocess.run(["x"], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)\n',
     1, '只有 NEW_PROCESS_GROUP 不算抑制窗口'),
    ('import subprocess\nsubprocess.run(["x"], creationflags=subprocess.CREATE_NO_WINDOW)\n',
     0, 'CREATE_NO_WINDOW 放行'),
    ('import subprocess\nsubprocess.run(["x"], creationflags=winconsole.NO_WINDOW)\n',
     0, 'NO_WINDOW 常量放行'),
    ('import subprocess\nsubprocess.run(["x"], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP'
     ' | winconsole.NO_WINDOW)\n', 0, '组合 flag 里带 NO_WINDOW 放行'),
    ('import subprocess\nsubprocess.run(["x"], creationflags=subprocess.DETACHED_PROCESS)\n',
     1, 'DETACHED_PROCESS 必须被抓'),
    ('import subprocess\nimport winconsole\n'
     'winconsole.run_hidden(["x"], creationflags=subprocess.DETACHED_PROCESS)\n',
     1, '经 helper 传 DETACHED 也要被抓（那不是 subprocess 调用）'),
    ('"""DETACHED_PROCESS 只是说明文字，不是使用。"""\nimport subprocess\n'
     'subprocess.run(["x"], creationflags=subprocess.CREATE_NO_WINDOW)\n',
     0, 'docstring 里提到 DETACHED 不算违规'),
    ('import subprocess as sp\nsp.Popen(["x"])\n', 1, '模块别名必须被抓'),
    ('from subprocess import Popen\nPopen(["x"])\n', 1, 'from 直导入必须被抓'),
    ('import subprocess\n# console-gate: posix-only -- linux\nsubprocess.run(["x"])\n', 0,
     'POSIX 白名单放行'),
    ('import subprocess\nsubprocess.run(["x"])  # console-gate: posix-only\n', 0,
     '行尾白名单放行'),
    ('import subprocess\nNOTE = "subprocess.run([1])"\n', 0, '字符串里的示例不算调用'),
    ('import subprocess\nsubprocess.Popen(["x"], **{"shell": False})\n', 1,
     '无法静态确认的 **kwargs 必须被抓'),
)


def self_test():
    """Prove the scanner still catches the shape issue #39 was made of."""
    for source, expected, why in SELF_TEST_CASES:
        violations, stale, _ = scan_source(source)
        if len(violations) != expected:
            raise AssertionError(f'检测器自测失败（{why}）：期望 {expected} 处，实得 {len(violations)} 处')
        if stale:
            raise AssertionError(f'检测器自测失败（{why}）：误报白名单失效')
    orphan = 'import subprocess\n# console-gate: posix-only\nX = 1\n'
    if not scan_source(orphan)[1]:
        raise AssertionError('检测器自测失败：孤立的白名单注释必须被判定为失效')
    # 数字字面量旁路：只认 CREATE_NO_WINDOW 的那个值，别把 0x200 也抓进来
    for text, expected in (('creationflags=0x08000000', True),
                           ('creationflags=0x8000000', True),
                           ('creationflags=0x00000200', False)):
        if bool(NUM_NO_WINDOW.search(text)) is not expected:
            raise AssertionError(f'检测器自测失败：数字规则对 {text!r} 判断错了')
    # 规则 3 读 AST：解释这个 flag 的 docstring / 注释不算「拼写」，真拿它当值才算
    for text, expected, why in (
            ('"""子进程用 CREATE_NO_WINDOW，见 issue #91。"""\n', 0, 'docstring 里的说明'),
            ('import subprocess  # CREATE_NO_WINDOW\nx = 1\n', 0, '注释里的说明'),
            ('import subprocess\nsubprocess.CREATE_NO_WINDOW\n', 1, '属性访问'),
            ("import subprocess\nx = getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n", 1, 'getattr 字符串'),
            ('from subprocess import CREATE_NO_WINDOW\n', 1, 'from 导入'),
    ):
        if len(flag_spellings(ast.parse(text))) != expected:
            raise AssertionError(f'检测器自测失败：{why} 的判定错了')
    return True


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    self_test()

    failures = []
    violations = []
    stale = []
    flagged = 0
    for path in iter_sources():
        rel = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            failures.append(f'{rel}: 无法读取（{exc}）')
            continue
        try:
            bad, dead, count = scan_source(text, rel)
        except SyntaxError as exc:
            failures.append(f'{rel}: AST 解析失败（{exc}）')
            continue
        violations.extend((rel, *item) for item in bad)
        stale.extend((rel, *item) for item in dead)
        flagged += count

    if violations:
        failures.append('这些 spawn 会给子进程开一个控制台窗口（issue #39）：')
        failures.extend(f'  {rel}:{lineno}  {line}' for rel, lineno, line, _ in violations)

    if stale:
        failures.append('白名单注释已经不对应任何未加固的 spawn，请删掉：')
        failures.extend(f'  {rel}:{lineno}  {line}' for rel, lineno, line in stale)

    for rel, lineno, line in scan_flag_owners():
        failures.append(f'CREATE_NO_WINDOW 只能出现在 winconsole.py / platform_process.py：'
                        f'  {rel}:{lineno}  {line}')

    for rel, lineno, line in scan_numeric_flags():
        failures.append(f'0x08000000 就是 CREATE_NO_WINDOW，请改用 helper 的常量：'
                        f'  {rel}:{lineno}  {line}')

    failures.extend(check_helpers())
    failures.extend(check_passthrough_visible())

    if failures:
        print('FAIL: 控制台开窗门禁未通过\n')
        for line in failures:
            print(line)
        print('\ndaemon 侧   from winconsole import ... → winconsole.run_hidden / popen_hidden')
        print('后端侧      from .platform_process import ... → run_hidden / owned_popen')
        print('POSIX 专用分支   在调用行或其上一行写 `# console-gate: posix-only` 并说明原因')
        print('要子进程的输出    用 winconsole.run_passthrough，别指望 run_hidden 继承 stdout（issue #91）')
        print('DETACHED_PROCESS 不是窗口抑制器，运行时树里不要出现（只有 winconsole 要提它）')
        return 1

    print('✓ 检测器自测通过（能抓住 issue #39 的原始写法，也能区分 flag 的值）')
    print(f'✓ 运行时树 {flagged} 处 spawn 已收口（flag 或显式 POSIX 白名单）')
    print('✓ 运行时树里没有 DETACHED_PROCESS（它不是窗口抑制器）')
    print('✓ CREATE_NO_WINDOW 只在 winconsole / platform_process 里拼写，也没有 0x08000000 旁路')
    print('✓ run_hidden / popen_hidden / owned_popen 实跑正常')
    print('✓ run_passthrough 在无控制台父进程下仍能把子进程输出交给调用方（issue #91）')
    print('\nPASS: 控制台开窗门禁通过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
