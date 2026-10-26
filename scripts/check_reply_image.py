"""issue #128 gate: `reply.py` 能把本地图片发进群（`--image`）。

症状：水群主 Agent 唯一的回复出口是 `reply.py`，而它只发文字与 @。任何「来张图」
「发个截图」「把这张图发群里」都撞在 `unrecognized arguments: --image` 上，只能嘴上
解释「我发不了图」——多模态群聊退化成纯文本。更坏的是绕路：Agent 若自己
`requests.post('/send_group_msg')` 手搓，会同时绕过 `record()` 记账与追问窗口。

本门禁钉十六件事：

  1. `--image <png>` + 文字 → 两次 `send_group_msg`：`[reply, text]` 与 `[image]`，
     且 image 段解出的字节与原文件逐字节相等（base64 段是 NapCat 实机验证过的形状，
     见 `doc/SLASH_COMMANDS.md:323`）；
  2. 纯图（无 text）→ 一次 `send_group_msg`：`[reply, image]`，**没有空文字段**；
  3. 两张图按参数顺序排在文字之后；
  4. 超 6 MiB / 内容不是图片（哪怕叫 `.png`）/ 只有 4 字节魔数 / 魔数齐全但结构坏 /
     路径不存在 → `exit=1`、**零请求**、输出点名原因；
  5. 半截的 JPEG（下载中断）→ 拒。**这条是 `verify()` 换成整张解码的理由**：
     Pillow 的 `verify()` 在 JPEG/GIF/WebP 上是空操作，半张图能一路过关发进群；
  6. 压缩率堆出来的超大像素图（9000×9000 的单色 PNG 只有 10 KiB）→ 拒，不解码；
  7. 帧数超过 `MAX_FRAMES`（301 帧的 16×16 GIF 只有 18 KiB）→ 拒；
  8. 好图 + 坏图混着给 → **一张都不发**（不是「先发好的再报错」）；
  9. 一次超过 `MAX_IMAGES` 张 → 拒，不发（气泡间隔会吃满宿主工具超时）；
  10. **图也算气泡**：正文 3 行 + 图 2 张、`--max-bubbles 4` → 拒（`IMAGE_FAILED`、
      零请求）。图不是附赠品，它和正文一起占气泡额度（issue #128 × #131）；
  11. OneBot 不可用时 **不回退计划通道**：`SEND_FAILED`、零请求、不记账、不出现
     回退行——回退过去图片会被静默吞掉（`send()` 的气泡写死 `kind:"text"`）；
  12. 读到的字节数与 `stat` 不符（下载器还在写）时不发半张图；
  13. 发图成功后 `wg_replied.jsonl` 有该 event 的记账行——防「图发了但还显示 [待回]」；
  14. `reply.py --help` 里有 `--image`（issue 第二条复现命令不再 `exit=1`）；
  15. **四种格式都真能发**：PNG / JPEG / GIF / WebP 各一张 → 全部 `SENT`、字节相等。
      只钉「截断的 JPEG 被拒」会漏掉反方向的回归——把 JPEG 整个拒掉时上面 14 条全绿，
      而群友发来的照片一张都发不出去；
  16. **额度内带图照发**：正文 3 行 + 图 2 张、`--max-bubbles 5` → 5 条气泡全部发出。
      第 10 条只有反向断言的话，「同时出现 `--image` 与 `--max-bubbles` 就一律拒」
      这种错误实现也能全绿。

真跑 `reply.py` 子进程 + loopback OneBot 假服务器 + 隔离数据根，不发任何真实消息：

    .venv/Scripts/python.exe scripts/check_reply_image.py
"""
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_fixture_support  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)

from fixture_server import FixtureHandler, fixture_server  # noqa: E402

REPLY = ROOT / "water-group" / "daemon" / "reply.py"
MAX_BYTES = 6 * 1024 * 1024
MAX_FRAMES = 300

# 1x1 透明 PNG 两张：等长、内容不同、Pillow 能整张解出来。
# 用真能解码的图而不是随手编的 PNG 头——校验是逐帧解码，编出来的假图会红在门禁
# 自己身上，看起来像门禁坏了。
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ"
    "/pLvAAAAAElFTkSuQmCC")
PNG2 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYPgPAAEDAQAI"
    "icLsAAAAAElFTkSuQmCC")

HOME = Path(tempfile.mkdtemp(prefix="wg_reply_image_"))
STATE = HOME / "state"
STATE.mkdir(parents=True)
(HOME / "data").mkdir()
CONFIG = HOME / "config.json"
GROUP = "123"
SELF = "10001"

CALLS = []
# `四种格式都真能发` 要按字节核对，所以留一份原文。
GOOD = {}
EXT = {"JPEG": "jpg", "GIF": "gif", "WEBP": "webp"}


class OneBot(FixtureHandler):
    """只认 `/send_group_msg`：别的路径被调用就是这次回复走错了出口。"""

    def do_POST(self):
        body = self.read_json()
        assert self.path == "/send_group_msg", self.path
        assert self.headers.get("Authorization") == f"Bearer {TOKEN}", "token 没带上"
        CALLS.append(body)
        self.send_json({"status": "ok", "retcode": 0,
                        "data": {"message_id": 4000 + len(CALLS)}})


TOKEN = "reply-image-fixture"
# 假服务器起来后填进来；`OneBot不可用时不回退计划通道` 会临时把配置指到别处再改回来。
LIVE_URL = ""


def write_config(url):
    CONFIG.write_text(json.dumps({
        "self_account": SELF,
        "onebot_token": TOKEN,
        "onebot_http_url": url,
        "self_names": ["小鲸鱼"],
    }, ensure_ascii=False), encoding="utf-8")


def child_env():
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["WATER_GROUP_HOME"] = str(HOME)
    env["IDLEJOT_WG_STATE"] = str(STATE)
    env["TULPA_WG_STATE"] = str(STATE)
    env["IDLEJOT_WG_DB"] = str(HOME / "data" / "mcp-access.sqlite3")
    env["IDLEJOT_WG_CONFIG"] = str(CONFIG)
    # loopback 夹具不该走代理；开发机上设了 HTTPS_PROXY 时 requests 会绕出去。
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        env.pop(name, None)
    return env


def run(*args):
    proc = subprocess.run([sys.executable, str(REPLY), *args],
                          cwd=str(ROOT), env=child_env(), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    return proc


def seed_events(count):
    """每条用例一个 event id，带 onebot_message_id（引用段要用它）。

    时间戳取「此刻」：过期唤醒（`up.wake_expired`）会被 `reply.py` 默认拒绝，
    固定时间戳的夹具过几小时就会红在「拒绝：id=N 是 500 分钟前的唤醒」上——
    那是时钟问题，不是本门禁要测的东西。
    """
    now = int(time.time() * 1000)
    rows = [{"id": n, "sender": "群友甲", "content": f"@小鲸鱼 第 {n} 条",
             "timestamp": now, "is_self": False,
             "conversation_id": f"{SELF}:group:{GROUP}",
             "onebot_message_id": str(700000 + n)} for n in range(1, count + 1)]
    (STATE / "wg_events.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def image_bytes(body):
    """一条 `send_group_msg` 报文里的 image 段解出的字节，没有则 None。"""
    for segment in body.get("message") or []:
        if segment.get("type") != "image":
            continue
        ref = str((segment.get("data") or {}).get("file") or "")
        assert ref.startswith("base64://"), ref
        return base64.b64decode(ref[len("base64://"):])
    return None


def types_of(body):
    return [segment.get("type") for segment in body.get("message") or []]


def ledger_ids():
    path = STATE / "wg_replied.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["id"] for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def 文字之后跟一张图():
    CALLS.clear()
    proc = run("1", "看这张图", "--image", str(HOME / "a.png"), "--no-wait", "--no-followup")
    ok = (proc.returncode == 0 and len(CALLS) == 2
          and types_of(CALLS[0]) == ["reply", "text"]
          and types_of(CALLS[1]) == ["image"]
          and image_bytes(CALLS[1]) == PNG)
    return ok or proc.stdout + proc.stderr


@case
def 纯图不带空文字():
    CALLS.clear()
    proc = run("2", "--image", str(HOME / "a.png"), "--no-wait", "--no-followup")
    ok = (proc.returncode == 0 and len(CALLS) == 1
          and types_of(CALLS[0]) == ["reply", "image"]
          and image_bytes(CALLS[0]) == PNG)
    return ok or proc.stdout + proc.stderr


@case
def 两张图按参数顺序():
    CALLS.clear()
    proc = run("3", "两张", "--image", str(HOME / "a.png"),
               "--image", str(HOME / "b.png"), "--no-wait", "--no-followup")
    ok = (proc.returncode == 0 and len(CALLS) == 3
          and types_of(CALLS[0]) == ["reply", "text"]
          and image_bytes(CALLS[1]) == PNG and image_bytes(CALLS[2]) == PNG2)
    return ok or proc.stdout + proc.stderr


@case
def 超上限一张都不发():
    CALLS.clear()
    proc = run("4", "太大", "--image", str(HOME / "huge.png"), "--no-wait", "--no-followup")
    ok = (proc.returncode == 1 and CALLS == []
          and "IMAGE_FAILED" in proc.stdout and "超过单张上限" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def 内容不是图片按内容拒():
    """扩展名是 `.png`，内容是文本——按内容判，不按扩展名。"""
    CALLS.clear()
    proc = run("5", "假图", "--image", str(HOME / "fake.png"), "--no-wait", "--no-followup")
    ok = (proc.returncode == 1 and CALLS == []
          and "IMAGE_FAILED" in proc.stdout and "不是能解码的" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def 路径不存在一张都不发():
    CALLS.clear()
    proc = run("6", "没有这个文件", "--image", str(HOME / "nope.png"),
               "--no-wait", "--no-followup")
    ok = (proc.returncode == 1 and CALLS == []
          and "IMAGE_FAILED" in proc.stdout and "读不到" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def 好坏混着给也是零请求():
    """一条坏路径不该让前面的好图发出去——「发了一半」是最难收拾的失败模式。"""
    CALLS.clear()
    proc = run("7", "混着来", "--image", str(HOME / "a.png"),
               "--image", str(HOME / "nope.png"), "--no-wait", "--no-followup")
    ok = (proc.returncode == 1 and CALLS == [] and "IMAGE_FAILED" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def 只有魔数的假图也拒():
    """4 个字节的 `\\x89PNG` 能过魔数检查，但在群里就是一张裂图。

    这条钉的是「魔数不是判据」：`ext_for` 只看前 4 字节，改回它这条就红。
    """
    CALLS.clear()
    proc = run("8", "四个字节", "--image", str(HOME / "magic4.png"),
               "--no-wait", "--no-followup")
    ok = (proc.returncode == 1 and CALLS == []
          and "IMAGE_FAILED" in proc.stdout and "不是能解码的" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def 魔数对但结构坏也拒():
    """PNG 签名齐全、后面全是零：魔数认得出，Pillow 解不出——必须拒。

    这是 `Image.open()` 取代 `ext_for` 的直接理由：16 字节的假 PNG 头在
    `ext_for` 那里是「合法 PNG」，发出去就是裂图。
    """
    CALLS.clear()
    proc = run("10", "坏结构", "--image", str(HOME / "broken.png"),
               "--no-wait", "--no-followup")
    ok = (proc.returncode == 1 and CALLS == []
          and "IMAGE_FAILED" in proc.stdout and "不是能解码的" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def 下载中断的半截图不发():
    """半截 JPEG：`stat` 与 `read` 长度一致、`verify()` 也说没问题，但解不出来。

    这条钉的是「`verify()` 不是判据」。Pillow 13.0.0 的 `ImageFile.verify` 只做
    `self.fp = None`，JPEG/GIF/WebP 全都继承它——换成整张解码之前，这半张图会
    一路 `SENT` 出去，群里收到的是裂图。
    """
    CALLS.clear()
    proc = run("12", "半张图", "--image", str(HOME / "half.jpg"),
               "--no-wait", "--no-followup")
    ok = (proc.returncode == 1 and CALLS == []
          and "IMAGE_FAILED" in proc.stdout and "解不开" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def 压缩出来的超大像素也拒():
    """9000×9000 的单色 PNG 只有 10 KiB——6 MiB 文件上限对它毫无作用。

    不拦就是拿发图当解压炸弹入口：整张解要 400 MiB。
    """
    CALLS.clear()
    proc = run("13", "炸弹", "--image", str(HOME / "bomb.png"),
               "--no-wait", "--no-followup")
    ok = (proc.returncode == 1 and CALLS == []
          and "IMAGE_FAILED" in proc.stdout and "兆像素上限" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def 帧数超上限也拒():
    """301 帧的 16×16 GIF 只有 18 KiB、15 兆像素：像素上限拦不住，靠帧数上限。"""
    CALLS.clear()
    proc = run("14", "太多帧", "--image", str(HOME / "frames.gif"),
               "--no-wait", "--no-followup")
    ok = (proc.returncode == 1 and CALLS == []
          and "IMAGE_FAILED" in proc.stdout and "帧上限" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def 一次超过九张就拒():
    """气泡之间有 1.2s 间隔，张数不封顶会吃满宿主 120s 超时——被砍时一个字都不打印。"""
    CALLS.clear()
    paths = []
    for index in range(10):
        paths += ["--image", str(HOME / "a.png")]
    proc = run("11", "太多", *paths, "--no-wait", "--no-followup")
    ok = (proc.returncode == 1 and CALLS == []
          and "IMAGE_FAILED" in proc.stdout and "一次最多发" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def 图也算气泡额度():
    """图不是附赠品：正文 3 行 + 2 张图 = 5 条气泡，`--max-bubbles 4` 时该拒。

    `--max-bubbles` 说的是「这次发几条气泡」（issue #131），图也是气泡。超了必须
    在发之前拒——不是把图悄悄截掉，也不是只打一行 WARN：气泡之间的 1.2s 间隔会
    吃掉发送预算，而预算被宿主超时砍掉时这条路径一个字都不打印。
    """
    CALLS.clear()
    proc = run("15", "第一行\n第二行\n第三行",
               "--image", str(HOME / "a.png"), "--image", str(HOME / "b.png"),
               "--max-bubbles", "4", "--no-wait", "--no-followup")
    ok = (proc.returncode == 1 and CALLS == []
          and "IMAGE_FAILED" in proc.stdout and "气泡上限" in proc.stdout)
    return ok or proc.stdout + proc.stderr


@case
def OneBot不可用时不回退计划通道():
    """计划通道的气泡写死 kind=text，回退过去图片就没了——那是「悄悄吞图」。

    把 OneBot 指到一个没人听的端口，要求「零请求 + SEND_FAILED + 不记账 + 不出现
    回退行」。最后那条不能省：只断言文案与退出码的话，把 `return 1` 改成 `pass`、
    让它继续往下走计划通道的变异仍能全绿（夹具没有可用批次，落下去也只是打印
    「没有可用批次」然后返回 1）。
    """
    CALLS.clear()
    write_config("http://127.0.0.1:1")
    try:
        proc = run("9", "发不出去", "--image", str(HOME / "a.png"),
                   "--no-wait", "--no-followup")
    finally:
        write_config(LIVE_URL)
    ok = (proc.returncode == 1 and CALLS == []
          and "SEND_FAILED" in proc.stdout and "计划通道" in proc.stdout
          and "falling back" not in proc.stdout
          and 9 not in ledger_ids())
    return ok or proc.stdout + proc.stderr


@case
def 读到一半的文件不发():
    """stat 与 read 之间文件被改写（下载器还在写）：长度对不上就拒，不发半张图。

    这条只能进程内测——子进程里没法在 stat 与 read 之间插一脚。所以把 `Path.stat`
    打桩成「比真实文件大 10 字节」，模拟 stat 之后文件被截短。
    """
    sys.path.insert(0, str(ROOT / "water-group" / "daemon"))
    import wg_images  # noqa: PLC0415

    target = HOME / "a.png"
    real_stat = Path.stat

    class Shrunk:
        st_mode = real_stat(target).st_mode
        st_size = len(PNG) + 10

    Path.stat = lambda self, **kw: Shrunk() if self == target else real_stat(self, **kw)
    try:
        wg_images.read_for_send(str(target))
    except wg_images.ImageError as exc:
        return "在读取时被改动" in str(exc) or str(exc)
    finally:
        Path.stat = real_stat
    return "长度对不上却没拒"


@case
def 发图也记账():
    """防「图发了但还显示 [待回]」：图片同样算回了这条。"""
    ids = ledger_ids()
    return 1 in ids and 2 in ids


@case
def 帮助里能查到图片入口():
    """issue 第二条复现命令：`--help | grep -i image` 过去退出码 1。"""
    proc = run("--help")
    return proc.returncode == 0 and "--image" in proc.stdout


@case
def 四种格式都真能发():
    """PNG / JPEG / GIF / WebP 各发一张，字节逐份对得上。

    这是上面那批「拒」用例的反方向：只钉「截断的 JPEG 被拒」的话，一个把 JPEG
    整个拒掉的回归会让前 14 条全绿，而群友发来的照片一张都发不出去。判据是
    「整张解得开」，那么解得开的好图就必须真的发得出去。
    """
    CALLS.clear()
    proc = run("16", "四种格式",
               "--image", str(HOME / "good.jpg"), "--image", str(HOME / "good.gif"),
               "--image", str(HOME / "good.webp"), "--no-wait", "--no-followup")
    ok = (proc.returncode == 0 and len(CALLS) == 4 and "SENT" in proc.stdout
          and types_of(CALLS[0]) == ["reply", "text"]
          and [image_bytes(body) for body in CALLS[1:]]
          == [GOOD["JPEG"], GOOD["GIF"], GOOD["WEBP"]])
    return ok or proc.stdout + proc.stderr


@case
def 额度内带图照发():
    """正文 3 行 + 图 2 张 = 5 条气泡，`--max-bubbles 5` 正好卡满——必须全部发出。

    第 10 条 `图也算气泡额度` 只有反向断言（超了就拒）。没有这条正向对照，
    「只要同时出现 `--image` 与 `--max-bubbles` 就一律拒」这种错误实现也能全绿。
    """
    CALLS.clear()
    proc = run("17", "第一行\n第二行\n第三行",
               "--image", str(HOME / "a.png"), "--image", str(HOME / "b.png"),
               "--max-bubbles", "5", "--no-wait", "--no-followup")
    ok = (proc.returncode == 0 and len(CALLS) == 5
          and [types_of(body) for body in CALLS]
          == [["reply", "text"], ["text"], ["text"], ["image"], ["image"]])
    return ok or proc.stdout + proc.stderr


def encode(image, fmt, **kw):
    buffer = io.BytesIO()
    image.save(buffer, fmt, **kw)
    return buffer.getvalue()


def half_jpeg():
    """一张下载中断的 JPEG：文件早就写坏了，不是读的过程中被改。

    `stat` 与 `read` 长度一致，所以长度校验帮不上忙；`verify()` 也帮不上——
    JPEG 用的是 `ImageFile.verify`，它只关文件句柄。只有整张解码能拦住。
    """
    src = Image.linear_gradient("L").resize((64, 64)).convert("RGB")
    raw = encode(src, "JPEG", quality=90)
    return raw[: len(raw) * 3 // 5]


def build_fixtures():
    (HOME / "a.png").write_bytes(PNG)
    (HOME / "b.png").write_bytes(PNG2)
    (HOME / "fake.png").write_text("这不是图片，只是改了个名字", encoding="utf-8")
    (HOME / "magic4.png").write_bytes(b"\x89PNG")
    (HOME / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    (HOME / "huge.png").write_bytes(PNG + b"\x00" * (MAX_BYTES + 1 - len(PNG)))
    (HOME / "half.jpg").write_bytes(half_jpeg())
    # 三种非 PNG 的好图：`四种格式都真能发` 要按字节核对，所以留一份原文。
    src = Image.linear_gradient("L").resize((32, 32)).convert("RGB")
    GOOD["JPEG"] = encode(src, "JPEG", quality=90)
    GOOD["GIF"] = encode(src, "GIF")
    GOOD["WEBP"] = encode(src, "WEBP")
    for fmt, raw in GOOD.items():
        (HOME / f"good.{EXT[fmt]}").write_bytes(raw)    # 9000×9000 的单色 1-bit PNG 只有 10 KiB：6 MiB 的文件上限拦不住它，真解一遍
    # 要 400 MiB。像素上限就是为这种图设的。
    (HOME / "bomb.png").write_bytes(encode(Image.new("1", (9000, 9000)), "PNG"))
    # 301 帧的 16×16 GIF 只有 18 KiB、15 兆像素——像素上限拦不住，靠帧数上限。
    frames = [Image.new("RGB", (16, 16), (n % 250, (n * 7) % 250, 90))
              for n in range(MAX_FRAMES + 1)]
    (HOME / "frames.gif").write_bytes(
        encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=20))
    (STATE / "wg_session.txt").write_text("session-fixture", encoding="utf-8")
    seed_events(17)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    build_fixtures()
    bad = 0
    global LIVE_URL
    try:
        with fixture_server(OneBot) as server:
            LIVE_URL = f"http://127.0.0.1:{server.server_port}"
            write_config(LIVE_URL)
            for fn in CASES:
                try:
                    ok = fn()
                except AssertionError as exc:
                    ok = f"断言失败：{exc}"
                except Exception as exc:  # noqa: BLE001
                    ok = f"{type(exc).__name__}: {exc}"
                if ok is True:
                    print(f"✓ {fn.__name__}")
                else:
                    print(f"✗ {fn.__name__}: {ok}")
                    bad += 1
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    print("全部通过" if not bad else f"{bad} 项失败")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
