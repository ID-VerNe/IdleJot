# issue #169 PE-2 — 13 处自带 ThreadingHTTPServer → fixture_server() + tempdir 推广

基线 `slim` @ `9cc0096`（PE-1 合入后）。

> 收口范围订正：`check_fixture_support.management_http()` 已在 PE-1（commit
> `9cc0096`）一并删除——PE-1 的 commit message 里写"先不动"，但实际 diff
> 已把那段 `@contextmanager def management_http(app)` 删掉了。本 PR 不再涉及
> 它，纯做 13 处服务器迁移。

## scope

13 个 `scripts/check_*.py` 自带 `ThreadingHTTPServer`（未走 `scripts/fixture_server.py` 的 `fixture_server()`）。`fixture_server()` 已经存在且被 12 个文件采用——这 13 个是漏网的。

| 文件 | bucket | 现状 |
|---|---|---|
| check_mcp_chat_turns | CI | 本机绿 |
| check_mcp_language_integration | CI | 本机绿 |
| check_mcp_multi_group_learning | CI | 本机绿 |
| check_mcp_reactions | CI | 本机绿 |
| check_mcp_auto_learning | MANUAL | 本机绿（tail 是 ASCII art） |
| check_mcp_chat_enrichment | MANUAL | **本机红**：`assert len(page['nodes'])==50` AssertionError——pre-existing（origin/slim 同样红），非服务器样板问题 |
| check_mcp_chat_materials_clients | MANUAL | 需外部 Codex CLI 参数，opt-in |
| check_mcp_chat_media | MANUAL | 本机绿 |
| check_mcp_chat_targets | MANUAL | 本机绿 |
| check_mcp_image_import | MANUAL | 本机绿 |
| check_mcp_image_reuse | MANUAL | **本机红**：`KeyError: 'representation'`——pre-existing（origin/slim 同样红） |
| check_mcp_language_usage | MANUAL | 本机绿 |
| check_mcp_sticker_library | MANUAL | 本机绿 |

## 迁移形态（统一）

每个文件的 `upstream = ThreadingHTTPServer(...); threading.Thread(...serve_forever...).start(); ...; finally: upstream.shutdown(); upstream.server_close()` 改成：

```python
with fixture_server(Handler, ports=allowed_ports) as upstream:
    # upstream.server_port 仍可用
    ...
```

`fixture_server()` 接受 `ports=`（network_guard 的 ports set，自动把 bound port 加进去），yield server，finally 里 `shutdown + server_close + thread.join`。与现有 13 处手写的生命周期等价。

## 关键约束

1. **13 处的 server 都是同一形态**：`ThreadingHTTPServer(('127.0.0.1',0), Handler)` + daemon thread `serve_forever` + `finally shutdown + server_close`。无变体（没有用 `HTTPServer` 非线程版、没有自定义 `server_bind`）。
2. **`allowed_ports` set 的时机**：6 个文件在 `network_guard(allowed_ports)` **之前**就 `add(upstream.server_port)`（reactions/auto_learning/chat_turns/language_integration/language_usage/multi_group_learning），7 个在之后。`fixture_server(ports=allowed_ports)` 内部 `ports.add(server.server_port)` 在 yield 前，所以无论 `network_guard` 在 `with` 内还是外都安全——但要注意 `allowed_ports` 这个 set 对象必须在调用 `fixture_server` 时已存在。
3. **`events.close()` 不归 `fixture_server`**：13 处都另有一个 `Events()`（WS fixture），它的生命周期独立，留在各文件。
4. **pre-existing 红（enrichment / image_reuse）**：迁移后必须仍是同样的红签名（AssertionError / KeyError），不能因为换了 server 包装就变了。这两条本机红是 issue #169 范围外的问题（enrichment 的 `nodes==50` 是分页上限漂移，image_reuse 的 `representation` key 是 schema 漂移），PE-2 只迁服务器、不修这两条。

## 顺序

1. 先迁 4 个 CI 文件（chat_turns / language_integration / multi_group_learning / reactions）——CI 会跑，验证迁移不破。
2. 再迁 9 个 MANUAL 文件——本机验证（enrichment / image_reuse 保持 pre-existing 红签名）。
3. ~~删 `check_fixture_support.management_http()`~~ —— PE-1 已删，无需再做。
4. tempdir 推广：74 处手写 `tempfile.mkdtemp` / `TemporaryDirectory` → `check_fixture_support.temp_dir()`。**注意 `temp_dir()` 是 `@contextmanager`，70 处是裸 `mkdtemp()` 路径**——迁移要改成 `with temp_dir() as tmp:`，不是换函数名。这一条单独 commit 或单独 PR（量太大，74 文件）。

## PR 切分

- **PE-2（本 PR）**：13 处服务器迁移。
- **PE-3（后续）**：74 处 tempdir → `temp_dir()` 推广（单独 PR，量大）。

## 回归

- mcp shard CI（4 个 CI 文件）：chat_turns / language_integration / multi_group_learning / reactions 必须仍绿。
- 9 个 MANUAL 文件本机：7 个绿保持绿，enrichment / image_reuse 保持 pre-existing 红签名不变。
- `check_gate_change_review`：13 个 `check_*.py` 改动需在 `## 门禁改动` 段逐个交代。
