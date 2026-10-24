# Third-party components

The MIT license at the repository root applies to Tulpa's own code. It does not
relicense the dependencies or external services. Exact package versions are
pinned in `pyproject.toml` / `uv.lock`.

This slim branch carries only the water-group backend (`server_wg.py` and the
`chatlocal/` engine it imports). The upstream desktop product — the .NET shell,
the portable ZIPs, the readers, FFmpeg, RapidOCR/ONNXRuntime, WebView2 — was
removed in Phase 3 and is not redistributed from this branch.

| Component | License / source |
| --- | --- |
| CPython 3.13 | [PSF license](https://docs.python.org/3/license.html); user-supplied runtime, not bundled |
| SQLite | [Public domain](https://www.sqlite.org/copyright.html); the `sqlite3` stdlib module in the Python runtime |
| mcp 1.26+ | [MIT](https://github.com/modelcontextprotocol/python-sdk/blob/v1.26.0/LICENSE); MCP server + tool routing. Pinned `<2` because the `@protocol.list_tools()` decorator API changed in 2.x |
| websockets 15+ | BSD-3-Clause. Pinned `<16`; the chat gate's `Events` fixture uses `websockets.sync.server` 15.x APIs |
| FastAPI | [MIT](https://github.com/fastapi/fastapi/blob/master/LICENSE); the `server_wg.py` backend |
| uvicorn | [BSD-3-Clause](https://github.com/encode/uvicorn/blob/master/LICENSE.md); ASGI server |
| httpx | [BSD-3-Clause](https://github.com/encode/httpx/blob/master/LICENSE); outbound HTTP (OneBot calls, voice ASR) |
| requests | [Apache-2.0](https://github.com/psf/requests/blob/main/LICENSE); `reply.py` OneBot HTTP send path |
| python-dotenv | [BSD-3-Clause](https://github.com/theskumar/python-dotenv/blob/main/LICENSE); `.env` loading |
| jsonschema | [MIT](https://github.com/python-jsonschema/jsonschema/blob/main/COPYING); MCP wire-contract validation |
| dnspython | [ISC](https://github.com/rthalley/dnspython/blob/v2.9.0/LICENSE); DNS-over-TLS fallback for reply-timing probes (see `doc/REPLY_TIMING.md`) |
| Pillow | [HPND/MIT](https://github.com/python-pillow/Pillow/blob/main/LICENSE); image handling for chat media |
| opencc-python-reimplemented | [Apache-2.0](https://github.com/longls/opencc/blob/master/LICENSE); traditional-to-simplified conversion in `voice.py` |
| pysilk-mod | [MIT](https://github.com/saiisdave/pysilk-mod/blob/master/LICENSE); SILK byte-signature sniff only — **not imported**, the module is not loaded at runtime; see `chatlocal/mcp_qq_media.py` SILK detection |
| pywin32 | [PSF-2.0](https://github.com/mhammond/pywin32/blob/main/Pythonwin/LICENSE.txt); Windows-only, installed but **not imported** — `voice_process.py` / `platform_process.py` use pure `ctypes` for the Job Object, pywin32 is a transitive convenience |
| qq-bridge 群聊行为与小鲸鱼角色卡 | [MIT](https://github.com/Derpyu520/qq-bridge/blob/9df6a7e7fc5abcb36793337f778483bd442a3d2d/LICENSE); 小鲸鱼角色卡经 Tulpa 维护者按需改编，保留来源及许可。来源提交、原版与当前文本 SHA256、修改说明记录在 `water-group/personas/little_whale.source.json`。行为与工具协议单独适配，仅用于 MCP 持续群聊，未引入其 DSH 会话桥接或后台唤醒系统 |
| 60s API（`vikiboss/60s`） | [MIT](https://github.com/vikiboss/60s/blob/master/LICENSE); `water-group/daemon/sixty.py` 只消费其 HTTP JSON 接口（不打包代码），实例地址由部署者在 `config.json` 的 `plugins.sixty.options.base_url` 自行配置（不随仓库分发）。接口返回的热榜 / 天气 / 简报内容版权归各原平台（微博、腾讯天气等），本项目只做**标题 + 链接的原文透传**，不存储、不改写、不摘要；端点白名单与禁用法见 `doc/SLASH_COMMANDS.md` §9 |

## Not bundled

NapCat (the OneBot 11 adapter), QQ and WeChat clients are not redistributed.
`scripts/setup-napcat.ps1` downloads a pinned NapCat Shell release into
`tools/napcat-shell/` (gitignored); QQ + QQNT must already be installed on the
host. Users install and run their own OneBot service under its own license. No
Microsoft / Office fonts are redistributed. User-provided credentials, chat
exports, model answers and local account state are not part of the repository.
