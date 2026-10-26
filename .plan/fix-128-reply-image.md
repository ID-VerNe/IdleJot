# fix #128 — `reply.py` 加 `--image`：水群回复能发本地图片

worktree: `.claude/worktrees/fix+128-reply-image`（从 slim `ff38ffe` 起）
分支: `feat/128-reply-image`（单分节 issue，不带 `p<字母>` 后缀）

## 复现（实施前基线）

```
$ .venv/Scripts/python.exe water-group/daemon/reply.py 1 "看这张图" --image a.png
usage: reply.py [-h] [--intent INTENT] [--no-quote] [--at 花名] [--also ID]
                [--wait 秒] [--no-wait] [--no-followup] [--force] [--who 花名]
                [--group GROUP] [--list] [--context N] [--around ID]
                [--show ID]
                [target] [text]
reply.py: error: unrecognized arguments: --image a.png
exit=2
```

```
$ .venv/Scripts/python.exe water-group/daemon/reply.py --help | grep -iE "image|sticker|图片|表情"
$ echo $?
1
```

用 issue 的复现命令复现：**成功**（`exit=2`，错误签名与 issue 一致；`--help` 无任何图片入口）。

## 探索结论（逐条核对代码，非照抄 issue）

1. **发送出口只有一处**：`reply.py:470` `onebot_send()` POST `/send_group_msg`，
   payload 是段列表；`reply.py:497` `onebot_send_bubbles()` 一条气泡一次 POST，
   首条挂 `reply` 段，间隔 `BUBBLE_GAP=1.2s`。加图片只要多一个 `{"type":"image"}`
   段，**不需要新出口**。
2. **base64 图片段在本仓已验证**：`qr_login.py:163-174` `qr_image_segment()` 产
   `{"type":"image","data":{"file":"base64://"+b64}}`；`doc/SLASH_COMMANDS.md:323`
   实机复测 `retcode=0`，不必退 `file://` 落盘方案。
3. **计划通道发不了图**：`reply.py:248` 气泡写死 `kind:"text"`，`reply.py:152`
   `expressions:["text"]`。走 MCP 媒体路径（`import_chat_image` → `plan_chat_reply`
   → `send_chat_reply` sticker 气泡）是 issue 给的备选方案 2，但它要求改
   `expressions`、改气泡类型、且**丢掉 OneBot 原生引用**（`send_chat_reply` 的
   quote 只认当前批次，`SKILL.md` 已写明这是它被弃用的原因）。**采用方案 1**。
4. **图片校验的家已经在**：`wg_images.py:24` `MAX_BYTES=6 MiB`、`wg_images.py:31`
   `ext_for()` 按魔数判 PNG/GIF/JPEG/WEBP、认不出返回 `bin`。issue 建议的「尺寸校验」
   和「非图片拒绝」都能复用，不重写第二份。
5. **`wg_images.py` 是图片模块但不是发送模块**：docstring 自称「接收侧」。本 PR 把
   「本地文件 → 可发送段」这半边放进它，charter 从「接收侧」改成「收发两侧」——
   它本来就有 `MAX_BYTES` / `ext_for`，另起模块会复制这两样。
6. **`reply.py` 已经贴着体量红线**：989 行 / 上限 1000（`check_module_size.py`
   实测 ADVISORY）。所以发送侧逻辑必须落在 `wg_images.py`，`reply.py` 只留参数与
   一次调用。
7. **`reply.py:114 replied_ids()` 是死代码**：全仓无 `reply.replied_ids` 引用
   （消费方一律走 `up.replied_ids`，见 `up.py:98` / `watch.py:41` /
   `check_pending_reply.py:45`）。删掉它正好腾出红线下的余量，避免为了 3 行就
   破 1000。

## 方案

`--image PATH`（可重复）→ 校验 → 转 base64 段 → 作为**额外气泡**追加在文字气泡之后，
仍由 `onebot_send_bubbles()` 逐条落地，首条照旧挂引用。

### `water-group/daemon/wg_images.py`（新增发送侧，约 +55 行）

- `image_segment(raw)`：字节 → `{"type":"image","data":{"file":"base64://..."}}`，
  与 `qr_image_segment()` 同形状。
- `read_for_send(path)`：`stat()` 先判「存在 / 普通文件 / ≤ `MAX_BYTES`」，再
  `read(MAX_BYTES+1)` 复查（防 stat 与 read 之间变大），`ext_for(None, raw) == "bin"`
  即拒。**判据是文件头，不是扩展名**——扩展名经常是错的（`.jfif`、大写 `.PNG`），
  而魔数骗不了人。
- `attach_images(bubble_segments, paths)`：先把**所有**路径校验完再动手（一条坏路径
  = 一个请求都不发），然后把每张图作为独立气泡追加；顺手丢掉空文字气泡（纯图时
  `split_bubbles("")` 会给出一个空段列表，发出去就是一条空消息）。多个坏路径合并成
  一条 `ValueError`，不让 Agent 修一个跑一次。

### `water-group/daemon/reply.py`（约 +14 / −11 行）

- `--image`（`action="append"`，与 `--at` 同款）+ docstring Usage 一行。
- `text` 改为「`--image` 在时可省」（纯图单独一条）。
- 发图**强制走 OneBot**：`if has_at or args.image or (not args.no_quote and found)`；
  OneBot 不可用时打 `SEND_FAILED`，**不回退计划通道**（那条通道只会发文字，回退等于
  悄悄吞掉图片）。
- 校验失败打 `IMAGE_FAILED: <原因>` 并 `return 1`，一个请求都不发。
- 删 `replied_ids()`（死代码，见探索结论 7）。

### 记账与窗口

不变：图片也算「回了这条」，`record()` / `open_followup()` 照旧触发，`--no-quote`
语义不变。

## 明确不做

- **不做目录白名单**（issue 修复候选里的「路径必须落在允许目录内」）。理由：真正的
  边界是「只能发真图片」，而这条由文件头判定兜住——凭据文件（JSON/DPEM）魔数过不
  了 `ext_for`，压根进不了发送层。目录白名单会给一种「已经隔离了」的错觉，同时把
  最常见的用法（用户给个 Downloads 里的路径）挡在外面。模型真要外泄数据，它有
  shell，`--image` 不是那道门。
- **不做 URL 拉图**：`chatlocal/mcp_chat.py:62` 明确禁止把 URL 交给发送层，这里同样。
- **不改 `commands.py` 的 `#` 指令图片路径**：`commands.py:412-429` 已可用。
- **不接 MCP 媒体路径 / 表情库**：见探索结论 3；要表情库语义（幂等、已看过的图）是
  另一件事，不并入本条。
- **不动 `expressions:["text"]`**（`reply.py:152`）：方案 1 不经过计划通道。
- **(b) 画图**：issue 已定单独立项，本 PR 不碰。

## 验证方式

新增门禁 `scripts/check_reply_image.py`，登记进 `scripts/gates.json` 的 `reply` 分片，
注释写明防 #128 复发。用 `scripts/fixture_server.py` 起 loopback OneBot 假服务器 +
隔离数据根（`WATER_GROUP_HOME` / `IDLEJOT_WG_STATE` / `IDLEJOT_WG_CONFIG`），真跑
`reply.py` 子进程，断言：

1. `--image <png>` + 文字 → 两次 `send_group_msg`：`[reply, text]` 与 `[image]`；
   image 段 `data.file` 以 `base64://` 开头，解出的字节与原文件逐字节相等；
2. 纯图（无 text）→ 一次 `send_group_msg`：`[reply, image]`，**没有空文字段**；
3. 两张图 → 图按参数顺序排在文字之后；
4. 超 6 MiB → `exit=1`、**零请求**、输出点名「超过单张上限」；
5. 内容是文本的文件（哪怕叫 `.png`）→ `exit=1`、零请求、输出点名「不是 PNG/JPEG/GIF/WebP」；
6. 路径不存在 → `exit=1`、零请求；
7. 发图成功后 `wg_replied.jsonl` 有该 event 的记账行（防「图发了但还显示 [待回]」）；
8. `reply.py --help` 含 `--image`（issue 第二条复现命令不再 `exit=1`）。

基线验证（`## 基线验证` 段要贴）：同一份门禁脚本拷进未打补丁的 pristine worktree
必须红，且红签名是 `unrecognized arguments: --image`——不是 import 裸奔。

同时跑：`check_reply_context` / `check_pending_reply` / `check_listener_rehang` /
`check_module_size` / `check_gate_manifest` / `check_pr_parity`。

## 文档

- `AGENTS.md` §0：「16 个参数」→ 17，命令块补 `--image`；§5.1 生成段由
  `check_gate_manifest.py --write` 重写。
- `README.md`：「已知限制」里「**只发文字。** 表情、图片、语音这些能力在 MCP 层，
  命令行这条路没接」改为图片已接、表情/语音仍未接；「按花名 @ 人」一节后面补发图说明。
- `.agents/skills/water-group/SKILL.md`：回复用法补 `--image`。

## 流程

1. plan 追评到 issue #128（过 `check_publication.py --require-private`）。
2. 改 `wg_images.py` / `reply.py`。
3. 写 `check_reply_image.py`，登记 `gates.json`，`check_gate_manifest.py --write`。
4. 基线红 → 补丁绿；跑 reply 分片 + parity。
5. PR（`Refs #128`，带 `## 门禁改动` + 独立上下文复核结论）→ CI 绿且无冲突则合并。

## 实施后修正（独立复核驱动）

计划里写的三处判据在实施/复核后被推翻，代码以本文档末尾为准：

1. **魔数校验 → Pillow 解码校验**。计划说「判据是文件头，魔数骗不了人」，复核指出
   `ext_for` 只看前 4 个字节：`b"\x89PNG"` 四个字节就能过，到群里是裂图；先加
   `MIN_IMAGE_BYTES=16` 也不够（16 字节的 `\xff\xd8` + 零、`GIF89a` + 零同样能过）。
   改成 `Image.open()` + `verify()` 判真实格式（`wg_images.image_format`），
   `SEND_FORMATS` 白名单。Pillow 早在 `pyproject.toml` 里、MCP 侧
   `mcp_media_pixels.pixels()` 用它做同一件事，不新增依赖；实测 6 MiB PNG 的
   verify 5ms。已知残留：截断的 GIF 两种判据都拦不住，由 stat/read 长度比对兜。
2. **新增张数上限 `MAX_IMAGES=9`**。计划里没有上限，复核指出每条气泡之间睡
   `BUBBLE_GAP=1.2s`，25 张就吃满 `wg_budget.SEND_BUDGET`，宿主 120s 超时会把整条
   调用连输出一起砍掉——而那条路径一个字都不打印。
3. **`read_for_send` 现在要求 `len(raw) == info.st_size`**。计划只防「读的过程中
   变大」，复核指出变小（下载器还在写）会把半张图发出去。

另外两条复核结论按计划执行、未改：不做目录白名单（理由见「明确不做」），
删除 `replied_ids()` 与 `_config()` 收口到 `wg_config.config()` 均确认无副作用。
