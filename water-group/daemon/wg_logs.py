"""逐行解码混编日志：napcat.log 里 UTF-8 与 GBK 并存。

`bootstrap.start_process_detached` 只能把子进程的裸字节追加进同一个文件
（`encoding=` 对子进程无效，见 issue #4），于是同一个 napcat.log 里有两股字节：
NapCat/Node 写 UTF-8，`launcher-user.bat` 的 `echo`/`pause` 写 936。整文件按
UTF-8 读会在第一行 GBK 处抛 UnicodeDecodeError，按 GBK 读又把 NapCat 的中文读成
鎺ユ敹；唯一正确的读法是逐行嗅探。

    .venv/Scripts/python.exe water-group/daemon/wg_logs.py napcat.log --tail 40
    .venv/Scripts/python.exe water-group/daemon/wg_logs.py napcat.log --grep 拒绝

CLI 存在的理由：`grep 中文` 命不中 GBK 行，`cat` 出来的混编文件在终端里也是坏的。
"""
import argparse
import sys
from pathlib import Path

import winconsole


def decode_log_line(raw: bytes) -> str:
    """Decode one log line without knowing which writer produced it.

    UTF-8 strict first: NapCat's output is valid UTF-8 and decoding it as GBK is
    the 鎺ユ敹 mojibake. GB18030 second: it is a superset of GBK, so cmd.exe's
    936 output comes back as the characters it meant. Lossy UTF-8 last, only for
    a line that is neither -- a truncated write, typically.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gb18030")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


def read_log_lines(path):
    """Yield every line of `path` decoded, streaming, mixed encodings and all."""
    with open(path, "rb") as fh:
        for raw in fh:
            yield decode_log_line(raw.rstrip(b"\r\n"))


def tail(path, count):
    """Last `count` lines, decoded. Reads the whole file: these logs are small
    enough that a seek-based tail is not worth the edge cases."""
    lines = list(read_log_lines(path))
    return lines[-count:] if count > 0 else []


def grep(path, needle):
    """Lines containing `needle`, decoded first so Chinese matches both halves."""
    return [line for line in read_log_lines(path) if needle in line]


def main(argv=None):
    # 一个专为读乱码日志而生的工具，不能在引发乱码的 936 环境里自己炸：管道 /
    # 重定向下 stdout 是 locale 编码，`print` 一条含 emoji 或四字节 GB18030 的行
    # 就是 UnicodeEncodeError。只在 main() 里收口，模块级会把 import 方的流也改掉。
    winconsole.ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="读取混编编码日志（UTF-8 + GBK 逐行嗅探）")
    parser.add_argument("path", type=Path, help="日志文件路径")
    parser.add_argument("--tail", type=int, default=0, metavar="N", help="只看最后 N 行")
    parser.add_argument("--grep", metavar="文本", help="只打印包含该文本的行")
    args = parser.parse_args(argv)

    if not args.path.exists():
        print(f"[ERROR] 日志不存在: {args.path}")
        return 1
    if args.grep is not None:
        lines = grep(args.path, args.grep)
    else:
        lines = tail(args.path, args.tail) if args.tail else list(read_log_lines(args.path))
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
