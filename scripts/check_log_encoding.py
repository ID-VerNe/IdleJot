"""钉住 issue #4：控制台与日志的编码两层都得钉死。

2026-10-22 的现场：NapCat 控制台整屏 `锟斤拷`，`napcat.log` 里 UTF-8 行与 GBK 行
并存。两层原因——Python 侧只 reconfigure 了 stdout、没改控制台代码页（UTF-8 字节被
936 读回），而 `Popen` 的日志句柄写着 `encoding="utf-8"` 却只交出 fileno（子进程写
裸字节，两股字节进同一个文件）。

这里固定六件事：

  1. `decode_log_line` 逐行嗅探：UTF-8 优先、GB18030 兜底，混编文件每行都读得对
  2. `start_process_detached` 二进制追加：子进程写什么字节，文件里就是什么字节
  3. `start_process_detached` 给子进程一个**无窗口控制台**（CREATE_NO_WINDOW）：
     既不继承调用方的 936 控制台，也让它自己的控制台子进程继承这份无窗口控制台，
     所以既没有乱码窗口，也没有一闪而过的黑框（DETACHED 的语义是"没有控制台"，
     孙子反而会自己开一个可见的，见 issue #39 的残留）
  4. 七个入口都走 `ensure_utf8_stdio`，不许再散落 reconfigure（"只修了一半"）
  5. 无控制台 / pythonw（stdout 为 None）/ 没有 reconfigure 的流都不报错
  6. **读侧**：门禁起子进程读它的输出时走 `fs.run_python`，钉 UTF-8 而不是跟着
     宿主 locale 走——再加一组「换成 locale 编码就必红」的对照，证明这组用例
     抓得住问题本身，不是一个恒真的绿（issue #240）

    .venv/Scripts/python.exe scripts/check_log_encoding.py
"""
import io
import locale
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import check_fixture_support as fs  # noqa: E402  (issue #169 PE: path 注入收口)
from check_fixture_support import scratch_dir  # noqa: E402
fs.bootstrap_paths(scripts=False, root=False)

TMP = scratch_dir(prefix="wg_encoding_")

# bootstrap 在 import 时就算出 LOGS_DIR，隔离目录必须先设好，否则探针会写进真日志。
os.environ["WATER_GROUP_HOME"] = str(TMP)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

# bootstrap 走桥接包（连字符目录不能进 sys.path）；它自己会把 daemon 目录插进来，
# 所以 wg_logs / winconsole 的 flat import 也在同一套规则里。
from water_group import bootstrap  # noqa: E402
import wg_logs  # noqa: E402
import winconsole  # noqa: E402

FAILED = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        FAILED.append(name)
        print(f"✗ {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"✓ {name}")


def expect(got, want, note=""):
    assert got == want, f"{note} got={got!r} want={want!r}"


# ---------------------------------------------------------------- 1. 逐行嗅探

DECODE_CASES = [
    ("UTF-8 中文", "10-22 11:27:56 [info] 收到消息".encode("utf-8")),
    ("GBK 中文（cmd.exe）", "拒绝访问。".encode("gbk")),
    ("纯 ASCII", b"QQ installation was not found in the registry."),
]


def decode_cases():
    for label, raw in DECODE_CASES:
        decoded = wg_logs.decode_log_line(raw)
        expect(decoded, raw.decode("utf-8") if label != "GBK 中文（cmd.exe）" else "拒绝访问。", label)
        assert "\ufffd" not in decoded, f"{label} 不该出现替换符"


def decode_never_raises():
    # 截断的写入：两种编码都不合法，只要求不抛、给出可读的替换符。
    decoded = wg_logs.decode_log_line(b"\xff\xfe\x00abc")
    assert isinstance(decoded, str) and decoded


def mixed_file_reads_clean():
    path = TMP / "mixed.log"
    gbk_line = "拒绝访问。"
    utf8_line = "10-20 10:55:50 [info] [FFmpeg] 检查 Native Addon 可用性..."
    # 🐻 是 cp936 编不出来的字符：936 管道下打印它会 UnicodeEncodeError，
    # 正好用来钉住 wg_logs CLI 的 stdout 收口。
    emoji_line = "10-20 10:56:00 [info] 熊男 🐻 上线"
    path.write_bytes(gbk_line.encode("gbk") + b"\r\n"
                     + utf8_line.encode("utf-8") + b"\n"
                     + emoji_line.encode("utf-8") + b"\n")
    expect(list(wg_logs.read_log_lines(path)), [gbk_line, utf8_line, emoji_line])
    # 整文件按 UTF-8 读会在这里炸——这正是不能整读的原因。
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("混编文件竟然能整读成 UTF-8，用例本身失效了")


def grep_hits_gbk_lines():
    path = TMP / "mixed.log"
    utf8_line = "10-20 10:55:50 [info] [FFmpeg] 检查 Native Addon 可用性..."
    expect(wg_logs.grep(path, "拒绝"), ["拒绝访问。"])
    expect(wg_logs.grep(path, "Native"), [utf8_line])
    expect(wg_logs.tail(path, 1), ["10-20 10:56:00 [info] 熊男 🐻 上线"])


def cli_survives_cp936_pipe():
    """CLI 自己也得能跑在 936 管道里——否则读乱码的工具自己先炸。"""
    path = TMP / "mixed.log"
    buf = io.BytesIO()
    wrapper = io.TextIOWrapper(buf, encoding="cp936", newline="")
    saved = sys.stdout
    sys.stdout = wrapper
    try:
        expect(wg_logs.main([str(path), "--tail", "3"]), 0, "CLI 退出码")
        wrapper.flush()
    finally:
        sys.stdout = saved
    out = buf.getvalue()
    assert "🐻".encode("utf-8") in out, "emoji 没有原样写出来"
    assert "拒绝访问。".encode("utf-8") in out, "GBK 行没有解对"


# ------------------------------------------------- 2. 日志句柄是二进制追加

def detached_log_keeps_raw_bytes():
    script = (
        "import sys;"
        "sys.stdout.buffer.write('拒绝访问。'.encode('gbk') + b'\\n');"
        "sys.stdout.buffer.write('收到消息'.encode('utf-8') + b'\\n');"
        "sys.stdout.flush()"
    )
    proc = bootstrap.start_process_detached([sys.executable, "-c", script], log_name="probe.log")
    proc.wait(timeout=60)
    raw = (TMP / "logs" / "probe.log").read_bytes()
    want = "拒绝访问。\n".encode("gbk") + "收到消息\n".encode("utf-8")
    # 文本模式会按一种编码重写这两行；二进制追加则原样落盘。
    expect(raw, want, "子进程裸字节被改写了")
    expect(list(wg_logs.read_log_lines(TMP / "logs" / "probe.log")), ["拒绝访问。", "收到消息"])


def child_gets_a_windowless_console():
    """#4 的乱码来自「继承调用方的控制台」；#39 的残留来自「DETACHED 没有控制台」。

    正确形态是第三种：子进程有自己的控制台，但那个控制台没有窗口。

      DETACHED_PROCESS   Windows 不给控制台，控制台程序（cmd/python）启动时自己
                         AllocConsole，**带窗口** -> 桌面闪一下，孙子进程亦然
      NO_WINDOW          Windows 给一个无窗口控制台 -> GetConsoleWindow()==0，
                         孙子进程继承同一份，不带 flag 也静默

    判据用子进程自报的两个数，不用 AttachConsole：Attach 在子进程刚启动时可能
    抢在控制台分配之前返回 ERROR_INVALID_HANDLE，旧断言正是靠这个竞态"通过"的。
    实测（2026-10-23）：NO_WINDOW -> `936 0`；DETACHED -> `936 <hwnd>`。
    """
    if sys.platform != "win32":
        return
    probe = ("import ctypes; k = ctypes.windll.kernel32; "
             "print(k.GetConsoleCP(), int(k.GetConsoleWindow() or 0))")
    proc = bootstrap.start_process_detached([sys.executable, "-c", probe],
                                            log_name="console-probe.log")
    proc.wait(timeout=60)
    lines = (TMP / "logs" / "console-probe.log").read_text(
        encoding="utf-8", errors="replace").strip().splitlines()
    cp, window = (int(part) for part in lines[-1].split())
    expect(cp != 0, True, "子进程没有控制台——DETACHED 又回来了，孙子只能自己开一个可见的")
    expect(window, 0, f"子进程的控制台带窗口（hwnd={window}），桌面上会闪")


# ------------------------------------------- 4. 七个入口都收口到 winconsole

ENTRIES = ["watch.py", "up.py", "commands.py", "dossier.py", "followup.py", "reply.py", "listen.py"]


def entries_use_ensure_utf8_stdio():
    for name in ENTRIES:
        text = (ROOT / "water-group" / "daemon" / name).read_text(encoding="utf-8")
        assert "winconsole.ensure_utf8_stdio(" in text, f"{name} 没接 ensure_utf8_stdio"
        assert "sys.stdout.reconfigure" not in text, f"{name} 还留着裸 reconfigure（只修了一半）"


# ------------------------------------------------- 5. 没有控制台也不能炸

class NoReconfigure(io.StringIO):
    """pythonw / 被重定向的流：没有 reconfigure 可调。"""


def stdio_helpers_are_optional():
    saved_out, saved_err = sys.stdout, sys.stderr
    calls = []
    saved_console = winconsole.use_utf8_console
    winconsole.use_utf8_console = lambda: calls.append(1) or True
    try:
        for out, err in ((None, None), (NoReconfigure(), NoReconfigure())):
            sys.stdout, sys.stderr = out, err
            winconsole.ensure_utf8_stdio()  # 不抛就算过
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
        winconsole.use_utf8_console = saved_console
    expect(len(calls), 2, "每次调用都要顺手改控制台代码页")


def ensure_utf8_stdio_switches_encoding():
    path = TMP / "reconfigure.txt"
    saved = sys.stdout
    fh = open(path, "w", encoding="cp936")
    sys.stdout = fh
    try:
        winconsole.ensure_utf8_stdio()
        fh.write("中文")
    finally:
        fh.close()
        sys.stdout = saved
    expect(path.read_bytes(), "中文".encode("utf-8"), "reconfigure 之后应当写 UTF-8 字节")


def use_utf8_console_without_tty_is_quiet():
    saved = sys.stdout
    sys.stdout = NoReconfigure()
    try:
        expect(winconsole.use_utf8_console(), False)
    finally:
        sys.stdout = saved


# ------------------------------------- 6. 门禁侧读子进程输出也钉 UTF-8 (#240)

# 与 issue #240 现场同形：`reply.py --list` 第一行「现在 …」后面就是中文，
# cp936 解码在第一个汉字处就炸（报错里是 pos 58）。
PROBE_SCRIPT = (
    "import sys;"
    "sys.stdout.buffer.write('现在 2026-10-23 20:52:07 (UTC+8) 待回 0 条\\n'.encode('utf-8'));"
    "sys.stdout.flush()"
)
PROBE_LINE = "现在 2026-10-23 20:52:07 (UTC+8) 待回 0 条"


def _probe(encoding):
    """按给定编码跑同一条探针；encoding=None 表示交给 locale（#240 的旧形态）。"""
    kwargs = {} if encoding is None else {"encoding": encoding}
    return subprocess.run(
        [sys.executable, "-c", PROBE_SCRIPT],
        capture_output=True, text=True, timeout=120, **kwargs,
    )


def run_python_pins_utf8():
    done = fs.run_python(["-c", PROBE_SCRIPT], timeout=120)
    expect(done.returncode, 0, "探针退出码")
    expect(done.stdout, PROBE_LINE + "\n", "子进程 UTF-8 输出应当原样读回")
    assert "\ufffd" not in done.stdout, "不该出现替换符"


def run_python_readable_from_cp936_host():
    """宿主是 cp936 时，换回 locale 解码就必须复现 #240 的短路（对照组）。"""
    if locale.getpreferredencoding(False).lower() in ("utf-8", "utf8"):
        return  # UTF-8 locale 的机器上这条对照组无从触发，如实说"不适用"
    done = _probe(None)
    assert done.returncode == 0, done.stderr
    assert done.stdout is None, (
        f"对照组没复现 #240（locale={locale.getpreferredencoding(False)} 竟然读得回），"
        "说明这组用例抓不住问题本身")


def run_python_refuses_caller_decoding_kwargs():
    for taken in ("text", "encoding", "errors", "universal_newlines"):
        try:
            fs.run_python(["-c", "print(1)"], **{taken: "utf-8" if taken in
                                                ("encoding", "errors") else True})
        except TypeError:
            continue
        raise AssertionError(f"run_python 竟然接受了调用方的 {taken!r}（会悄悄把 locale 放回来）")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    check("逐行嗅探：UTF-8 / GBK / ASCII", decode_cases)
    check("逐行嗅探：两编码都不合法也不抛", decode_never_raises)
    check("混编文件逐行读全对", mixed_file_reads_clean)
    check("grep / tail 命中 GBK 行", grep_hits_gbk_lines)
    check("CLI 在 936 管道下不炸（emoji 也不炸）", cli_survives_cp936_pipe)
    check("日志句柄二进制追加，裸字节不改写", detached_log_keeps_raw_bytes)
    check("注入实例拿到无窗口控制台（孙子进程也不会开窗）", child_gets_a_windowless_console)
    check("七个入口统一走 ensure_utf8_stdio", entries_use_ensure_utf8_stdio)
    check("无控制台 / 无 reconfigure 不报错", stdio_helpers_are_optional)
    check("ensure_utf8_stdio 真的切到 UTF-8 字节", ensure_utf8_stdio_switches_encoding)
    check("无 tty 时 use_utf8_console 安静返回", use_utf8_console_without_tty_is_quiet)
    check("门禁读子进程输出钉 UTF-8（#240）", run_python_pins_utf8)
    check("对照组：locale 解码就读崩，用例抓得住问题（#240）", run_python_readable_from_cp936_host)
    check("run_python 拒绝调用方自带的解码参数（#240）", run_python_refuses_caller_decoding_kwargs)

    shutil.rmtree(TMP, ignore_errors=True)
    if FAILED:
        print(f"{len(FAILED)} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
