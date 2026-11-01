"""`python -m scripts.check_no_console_child` 入口（issue #329）。

与薄入口 `scripts/check_no_console_child/_entry.py` 走同一个 `run_main()`。`-m` 进来时
本文件位于 `<根>/scripts/<包名>/`，仓库根往上数三级。
"""
import sys
from pathlib import Path

from . import run_main

if __name__ == '__main__':
    sys.exit(run_main(Path(__file__).resolve().parent.parent.parent))
