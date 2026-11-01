"""`python scripts/check_no_console_child/_entry.py` 入口（issue #329）。

门禁本体在包的 `__init__.py` 里。仓内没有 `scripts/check_no_console_child.py`：
包目录与单文件同名时单文件会挡住包名（`import check_no_console_child` 落到它身上），
于是包本体 import 不到自己的兄弟模块。所以入口搬进了包里，`check_gate_manifest.py`
按 `scripts/check_*/` 把整个包认成**一个**门禁 `check_no_console_child`（与
#169 PF-2 / #283 / #285 的拆包先例同形）；本文件名以 `_` 开头，不会被当成第二个门禁。

    uv run python scripts/check_no_console_child/_entry.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_no_console_child import run_main  # noqa: E402

if __name__ == '__main__':
    # 本文件在 `<根>/scripts/check_no_console_child/` 下：仓库根往上数三级
    sys.exit(run_main(Path(__file__).resolve().parents[2]))
