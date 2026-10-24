"""Watchdog health probe must read the OneBot token fresh every call (#17).

`TOKEN = onebot_token()` used to be a module-level constant: whatever
config.json held at import -- "" on a cold start, or a token the operator has
since rotated -- was sent on every probe forever. NapCat answers 401,
healthy() stays False, three retries fail, and the watchdog concludes the
instance is wedged: kill_injected_instances() + relaunch() every 30-80s,
forever, while the log only says `unresponsive pid=...`. up.py re-reads the
config on every call; both must agree.

The gate replays a token rotation against a fake NapCat -- it fails if any
probe still carries the frozen token. Fully offline: httpx.post is replaced
by a recorder and no socket is opened.

    .venv/Scripts/python.exe scripts/check_watchdog_token.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_fixture_support import isolated_env

WATCHDOG = ROOT / "water-group" / "daemon" / "watchdog.py"


def load_watchdog(config_path):
    """Import watchdog.py the way the supervisor launches it, config pinned."""
    spec = importlib.util.spec_from_file_location("watchdog_under_test", WATCHDOG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CONFIG = config_path
    return module


def write_config(path, token):
    path.write_text(json.dumps({"onebot_token": token}), encoding="utf-8")


class FakeNapCat:
    """Answers get_login_info like NapCat: 200 only for the current token."""

    def __init__(self):
        self.seen = []
        self.current = ""

    def post(self, url, headers=None, timeout=None, trust_env=False):
        auth = (headers or {}).get("Authorization", "")
        self.seen.append(auth)
        if self.current and auth == f"Bearer {self.current}":
            return SimpleNamespace(status_code=200,
                                   json=lambda: {"status": "ok", "data": {}})
        return SimpleNamespace(status_code=401, json=lambda: {"status": "failed"})


def main():
    with tempfile.TemporaryDirectory(prefix="watchdog-token-") as tmp:
        config = Path(tmp) / "config.json"
        with patch.dict("os.environ", isolated_env(Path(tmp) / "home"), clear=True):
            write_config(config, "token-before-rotation")
            watchdog = load_watchdog(config)
            napcat = FakeNapCat()
            watchdog.port_open = lambda: True
            watchdog.httpx = napcat

            # 1. Fresh config: the probe carries the configured token.
            napcat.current = "token-before-rotation"
            assert watchdog.healthy() is True, "probe failed with the configured token"
            assert napcat.seen[-1] == "Bearer token-before-rotation", napcat.seen

            # 2. The regression: the operator rotates onebot_token in
            #    config.json. The next probe must follow, not repeat the
            #    import-time snapshot.
            write_config(config, "token-after-rotation")
            napcat.current = "token-after-rotation"
            assert watchdog.healthy() is True, "probe kept the pre-rotation token (frozen TOKEN regression)"
            assert napcat.seen[-1] == "Bearer token-after-rotation", napcat.seen

            # 3. Cold start: config.json absent -> token "" -> 401, no crash.
            config.unlink()
            napcat.current = ""
            assert watchdog.healthy() is False, "empty token must not authenticate"
            assert napcat.seen[-1] == "Bearer ", napcat.seen

            # 4. Env override still wins, per probe, without a restart.
            write_config(config, "token-from-config")
            napcat.current = "token-from-env"
            with patch.dict(os.environ, {"IDLEJOT_ONEBOT_TOKEN": "token-from-env"}):
                assert watchdog.healthy() is True, "env token override ignored"
                assert napcat.seen[-1] == "Bearer token-from-env", napcat.seen

            # 5. Port down: no probe at all, just False.
            watchdog.port_open = lambda: False
            before = len(napcat.seen)
            assert watchdog.healthy() is False
            assert len(napcat.seen) == before, "probe fired with the port closed"

    assert not hasattr(watchdog, "TOKEN"), "module-level TOKEN snapshot is back (#17)"
    print("PASS: watchdog probe reads onebot_token per call; rotation, cold start "
          "and env override all followed, port-down probe suppressed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
