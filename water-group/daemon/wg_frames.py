"""视频抽帧：把一段 mp4 变成几张图，交给既有的图片多模态管线（issue #200 P3）。

issue #200 的方案 A。方案 B（原生视频理解）需要底层多模态支持视频输入，当前
MCP 只吃图片块，所以走抽帧：抓首帧 / 中帧 / 尾帧，落成 PNG，之后就能按普通
图片读——不需要改 MCP 契约，也不需要模型侧支持视频。

**ffmpeg 是可选的**。它不在 ``pyproject.toml``（仓库不替使用者装系统级可执行
文件），所以这里只探测，不自动下载：

* 找得到 → 正常抽帧；
* 找不到 → :func:`probe` 返回带 ``problem`` 的结果，调用方**明说**缺什么、
  怎么补（装 ffmpeg 并加进 PATH）。**不静默降级成「没有内容」**——那会让
  Agent 以为群友发的是个空视频。

判据与边界：

* 时长用 ``ffprobe`` 问，问不到（``ffprobe`` 缺失 / 文件不是视频）就说不知道，
  此时退化成「只抽首帧」，而不是猜一个时长去抽中间；
* 三个时间点按**有视频的**时长算，且去重：0.2 秒的短视频不会抽出三张一样的
  首帧；
* 单帧输出走 ``-frames:v 1``，不产出视频流；超时按墙钟掐断；
* 子进程一律 ``winconsole.run_hidden``（防黑框，issue #39）。
"""
import shutil
import subprocess

import winconsole

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

FRAME_TIMEOUT = 30        # 单帧抽取的墙钟预算（秒）
PROBE_TIMEOUT = 10        # 问时长
TALL_LIMIT = 1280         # 长边上限：抽帧只为看懂内容，不需要原分辨率
# 三个位置：首、中、尾。尾帧不用 1.0，很多容器最后一帧落在尾部边界外。
POSITIONS = (0.0, 0.5, 0.9)


def fftools():
    """``(ffmpeg 路径, ffprobe 路径)``，缺哪个哪项为 ``None``。"""
    return shutil.which(FFMPEG), shutil.which(FFPROBE)


def probe():
    """探测抽帧能力。

    返回::

        {"ok": bool, "ffmpeg": str, "ffprobe": str, "problem": str}

    ``problem`` 是**能直接印给人看的一句**：缺什么、怎么补。``ok`` 为真时为空串。
    """
    ffmpeg, ffprobe = fftools()
    if ffmpeg and ffprobe:
        return {"ok": True, "ffmpeg": ffmpeg, "ffprobe": ffprobe, "problem": ""}
    missing = [name for name, path in ((FFMPEG, ffmpeg), (FFPROBE, ffprobe)) if not path]
    return {
        "ok": False, "ffmpeg": ffmpeg or "", "ffprobe": ffprobe or "", "problem":
        f"抽帧需要 {'、'.join(missing)}，但 PATH 里找不到。"
        "装好 ffmpeg（含 ffprobe）并加进 PATH 后再试；"
        "不想装就先自己看视频文件。",
    }


def duration(path):
    """视频时长（秒）。问不到返回 ``0.0``——**不猜**。

    ``ffprobe`` 不存在、文件不是视频、容器没写时长，三种都落到这里。调用方据
    ``0.0`` 只抽首帧，而不是拿一个编出来的时长去定位中间。
    """
    _, ffprobe = fftools()
    if not ffprobe:
        return 0.0
    try:
        done = winconsole.run_hidden(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return 0.0
    if done.returncode != 0:
        return 0.0
    try:
        return max(0.0, float((done.stdout or "").strip()))
    except ValueError:
        return 0.0


def timestamps(seconds):
    """要抽的时刻列表。时长未知只给首帧；短时长去重后可能不足三张。

    去重的意义：0.2 秒的视频按 0/0.1/0.18 抽出来是三张几乎一样的图，白白多花
    首次读图的额度，还容易让 Agent 以为「视频就这么静止」。
    """
    if seconds <= 0:
        return [0.0]
    picked = []
    for pos in POSITIONS:
        at = round(seconds * pos, 2)
        if not picked or at - picked[-1] >= 0.5:
            picked.append(at)
    return picked


def extract(path, out_dir, *, stem="frame"):
    """抽帧并返回结果字典。

    ::

        {"ok": bool, "frames": [{"path","at"}], "duration": float, "problem": str}

    失败时 ``frames`` 为空、``problem`` 说明原因；**不抛异常**——抽帧失败不该
    让补读口崩掉，它只是「这条视频看不了内部」，文件本体仍在。
    """
    from pathlib import Path

    state = probe()
    if not state["ok"]:
        return {"ok": False, "frames": [], "duration": 0.0, "problem": state["problem"]}
    src = Path(path)
    if not src.is_file():
        return {"ok": False, "frames": [], "duration": 0.0,
                "problem": f"本地没有 {src}，先下载再抽帧。"}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    seconds = duration(src)
    frames, problems = [], []
    for index, at in enumerate(timestamps(seconds)):
        target = out / f"{stem}-{index}.png"
        command = [state["ffmpeg"], "-v", "error", "-ss", f"{at:.2f}", "-i", str(src),
                   "-frames:v", "1",
                   "-vf", f"scale='min({TALL_LIMIT},iw)':-1",
                   "-y", str(target)]
        try:
            done = winconsole.run_hidden(command, capture_output=True, text=True,
                                         timeout=FRAME_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append(f"{at:.2f}s 抽帧失败：{exc.__class__.__name__}")
            continue
        if done.returncode != 0 or not target.is_file():
            detail = (done.stderr or "").strip().splitlines()
            problems.append(f"{at:.2f}s 抽帧失败" + (f"：{detail[-1]}" if detail else ""))
            continue
        frames.append({"path": str(target), "at": at})
    if not frames:
        reason = "；".join(problems) if problems else "ffmpeg 没有报错，但也没有产物"
        return {"ok": False, "frames": [], "duration": seconds,
                "problem": f"一帧都没抽出来：{reason}"}
    return {"ok": True, "frames": frames, "duration": seconds,
            "problem": "；".join(problems)}


def render(result, *, event_id=None, index=0):
    """把结果渲染成一行可交给 Agent 的说明。

    与 ``wg_videos.render`` 同一纪律：成功给出**具体路径**，失败给出**具体原因**，
    绝不印一个「抽帧完成/失败」了事。
    """
    who = f" id={event_id} i={index}" if event_id is not None else ""
    if not result.get("ok"):
        return f"[抽帧{who} 失败：{result.get('problem') or '未知原因'}]"
    lines = [f"[抽帧{who} {len(result['frames'])} 帧"
             + (f" 时长 {result['duration']:.1f}s" if result.get("duration") else " 时长未知")
             + "]"]
    for frame in result["frames"]:
        lines.append(f"  {frame['at']:.2f}s {frame['path']}")
    if result.get("problem"):
        lines.append(f"  部分失败：{result['problem']}")
    return "\n".join(lines)
