"""List account directory names only; no keys, snapshots or message databases opened."""
import contextlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

# The only platform this build reads (#350). The `wechat` branch used to import
# `wechatauto` out of `tools/wechat-reader`, which is gone with the stripped reader.
PLATFORMS = ('qq',)


def discover(platform):
    if platform != 'qq':
        raise ValueError('Invalid platform')
    sys.path.insert(0, str(ROOT/'tools/qq-reader'))
    from chatlog_keeper.qq_db import find_qq_data_root, find_qq_account_databases
    root = find_qq_data_root()
    return list(find_qq_account_databases(root)) if root else []


if __name__ == '__main__':
    if len(sys.argv) != 2 or sys.argv[1] not in PLATFORMS:
        print(f'usage: {Path(__file__).name} <{"|".join(PLATFORMS)}>', file=sys.stderr)
        raise SystemExit(2)
    with contextlib.redirect_stdout(sys.stderr):
        accounts = discover(sys.argv[1])
    print(json.dumps(accounts, ensure_ascii=False))
