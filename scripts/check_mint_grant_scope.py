"""issue #201 gate: 冷启动的 grant 默认 scope 不许静默开媒体授权。

缺陷（`water-group/daemon/mint_grant.py:37-45`）：grants 表为空（冷启动 / 数据根重建）
时 `last_scope()` 回落 `DEFAULT_SCOPE`，而那里 `chat_video` / `chat_files` 是 `True`，
`qq_*` 家族是 `False`。两组口径不一致：collector 实际只调 `wait_chat_messages`，
`read_chat_attachment` / `import_qq_media` 是 Agent 侧工具、按 scope 放行
（`chatlocal/mcp_chat.py:441`、`chatlocal/mcp_qq_media.py:22`），默认开这两位对 collector
零收益，却让「操作者手动收窄过的授权」在下次重建后静默回弹。

这条门禁钉两层，任一处退回旧默认值都红：

1. **常量层**：`DEFAULT_SCOPE` 里 `chat_video` / `chat_files` 为 `False`，同时
   `SCOPE_FIELDS` 里这两个键仍然存在（只改默认值、不许把它们从清单里悄悄删掉）。
   排除型对照：collector 真正需要的几位（`send` / `chat` / `onebot` / `manage` /
   `chat_voice`）必须仍为 `True`——否则「全改 False」也能让本门禁变绿。
2. **契约层**：空 grants 表下桩住 `httpx.Client`，走一遍真正的 `mint()`，断言发往
   `POST /api/mcp/connections` 的 body 里 `chat_video` / `chat_files` 是 `False`。
   钉住对外可观察的请求体，而不只是模块内常量（`mint()` 里 `scope.get(key, False)`
   的回落分支同样被覆盖）。

全程离线：数据根钉到临时目录、grants 用假库、HTTP 全桩，不碰真栈：

    .venv/Scripts/python.exe scripts/check_mint_grant_scope.py
"""
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import check_fixture_support as fs  # noqa: E402  (issue #169 PE: path 注入收口)
from check_fixture_support import isolated_env, scratch_dir  # noqa: E402

# 数据根在 import 前隔离：mint_grant 在模块级求值 STATE_DIR / DB。
_SCRATCH = scratch_dir(prefix="tulpa-mint-grant-")
_HOME = _SCRATCH / "home"
os.environ.clear()
os.environ.update(isolated_env(_HOME))
os.environ["IDLEJOT_WG_STATE"] = str(_SCRATCH / "state")

fs.bootstrap_paths(scripts=False, root=False)

import mint_grant  # noqa: E402

GRANTS_DDL = """CREATE TABLE grants(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL, epoch TEXT NOT NULL, accounts TEXT NOT NULL,
    created_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)"""

# 冷启动必须保守的媒体授权（issue #201 的症状面）。
MEDIA_OFF = ("chat_video", "chat_files")
# collector 真正依赖的能力：排除型对照，防止「全改 False」蒙过关禁。
COLLECTOR_NEEDS = ("send", "chat", "onebot", "manage", "chat_voice")


def _empty_grants_db(name):
    """一个存在但 grants 表为空的库——正是冷启动 / 数据根重建后的形状。"""
    db = _SCRATCH / name
    conn = sqlite3.connect(db)
    try:
        conn.execute(GRANTS_DDL)
        conn.commit()
    finally:
        conn.close()
    return db


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"id": "grant-fixture", "token": "fixture-token"}


class _FakeClient:
    """httpx.Client 的替身：记下 POST body，不回真网络。"""

    posts = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        _FakeClient.posts.append({"url": url, "body": json, "headers": headers})
        return _FakeResponse()


def test_default_scope_media_off():
    """冷启动常量：媒体授权默认关，collector 依赖的几位仍开。"""
    off = {key: mint_grant.DEFAULT_SCOPE.get(key) for key in MEDIA_OFF}
    assert off == {key: False for key in MEDIA_OFF}, (
        f"#201：冷启动不许默认开媒体授权，实际 {off}")
    # 只改默认值，不许把键从 SCOPE_FIELDS 里删掉（那会让操作者的 UI 开关失效）。
    missing = [key for key in MEDIA_OFF if key not in mint_grant.SCOPE_FIELDS]
    assert not missing, f"{missing} 必须仍在 SCOPE_FIELDS 里（只改默认值）"
    needs = {key: mint_grant.DEFAULT_SCOPE.get(key) for key in COLLECTOR_NEEDS}
    assert all(needs.values()), f"collector 依赖的能力不许被一起关掉：{needs}"
    # qq_* 家族原本就是保守的，口径必须一致。
    qq = {key: mint_grant.DEFAULT_SCOPE.get(key)
          for key in mint_grant.DEFAULT_SCOPE if key.startswith("qq_")}
    assert qq and not any(qq.values()), f"qq_* 应全关，实际 {qq}"
    print("✓ 1. DEFAULT_SCOPE：chat_video/chat_files 默认关，collector 所需与 qq_* 口径一致")


def test_cold_start_scope_rolls_back_to_default():
    """空 grants 表 → last_scope() 回落 DEFAULT_SCOPE，媒体授权为关。"""
    db = _empty_grants_db("grants-empty.sqlite3")
    with patch.object(mint_grant, "DB", db):
        scope = mint_grant.last_scope()
    assert scope == mint_grant.DEFAULT_SCOPE, f"空表应回落 DEFAULT_SCOPE，实际 {scope}"
    leaked = {key: scope.get(key) for key in MEDIA_OFF if scope.get(key)}
    assert not leaked, f"#201：冷启动 scope 仍开着媒体授权 {leaked}"
    print("✓ 2. 空 grants 表冷启动：回落的 scope 里媒体授权是关的")


def test_cold_start_mint_body_media_off():
    """契约层：冷启动 mint() 真正发出的 POST body 里媒体授权是关的。"""
    db = _empty_grants_db("grants-mint.sqlite3")
    token_file = _SCRATCH / "state" / "mcp_token.txt"
    _FakeClient.posts = []
    with patch.object(mint_grant, "DB", db), \
            patch.object(mint_grant, "TOKEN_FILE", token_file), \
            patch.object(mint_grant.httpx, "Client", _FakeClient):
        token = mint_grant.mint()
    assert token == "fixture-token", f"mint 应返回桩 token，实际 {token}"
    assert len(_FakeClient.posts) == 1, f"应恰好发一次建授权请求，实际 {_FakeClient.posts}"
    body = _FakeClient.posts[0]["body"]
    leaked = {key: body.get(key) for key in MEDIA_OFF if body.get(key)}
    assert not leaked, f"#201：冷启动 mint 的请求体仍开着媒体授权 {leaked}"
    # 请求体必须覆盖全部 SCOPE_FIELDS（mint 不写全会让后端按默认补，口径再次分叉）。
    dropped = [key for key in mint_grant.SCOPE_FIELDS if key not in body]
    assert not dropped, f"mint 请求体漏了 scope 字段：{dropped}"
    assert body["chat_voice"] is True, "collector 的语音 ASR 不许被一起关掉"
    assert token_file.read_text(encoding="utf-8") == "fixture-token", "mint 应落盘 token"
    print("✓ 3. 冷启动 mint() 的 POST body：媒体授权关、字段齐全、voice 仍开")


def main():
    test_default_scope_media_off()
    test_cold_start_scope_rolls_back_to_default()
    test_cold_start_mint_body_media_off()
    print("PASS: issue #201 冷启动 grant 默认 scope 检查全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
