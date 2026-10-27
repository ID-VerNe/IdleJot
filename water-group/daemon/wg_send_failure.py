"""发送失败说清楚：真实 retcode、禁言解禁时刻、这次调用等不等得起（issue #132）。

issue #132 的现场是机器人被禁言，`reply.py <id> "..." --at <花名>` 打出：

    send: retcode=200 message_id=None
    SEND_FAILED: 带 @ 的消息只能走 OneBot，计划通道会把它降级成纯文本

第二句回答的是「为什么不退回计划通道」，却被读成「是 @ 害的」——Agent 于是去掉
@ 重发一次，照样失败，两轮四分钟过去，群里已经刷过一屏。真实原因（OneBot 响应体
`message` 字段里的 `EventChecker Failed: ... EventRet: {"result": 120}`）和真正的
解禁时刻，一个字都没出现。

三件事收在这里，`reply.py` 只留分支判断：

1. **原文**：`onebot_send()` 失败时把 `retcode` / `message` / `wording`（或网络异常）
   记进调用方持有的 sink，`describe()` 原样拼一行——不猜、不映射错误码（那是
   `chatlocal/` 网关层的活）。
2. **禁言**：`diagnose()` 只在失败路径上查一次 `get_group_member_info(no_cache=True)`
   的 `shut_up_timestamp`，再读 `get_group_info().group_all_shut` 区分全员 / 单人，
   `shut_up_timestamp > now` 才说「禁言至 HH:MM:SS（还剩 N 秒）」。探测失败说
   「禁言状态未知」——**不许**把拿不到状态说成没被禁言（#112 的同一条纪律：假事实
   比没有输出更坏）。
3. **等待**：`wait_plan()` 用 `wg_budget.WAIT_BUDGET` 判「解禁 + 5s 在不在额度内」，
   在就睡过去补发一次，不在就如实报「需等 N 秒」。硬等会被宿主在 120s 处砍掉整棵
   进程树，而那条路径上一个字都不打印（#79）。

明确不做：禁言期间改走计划通道（最终也是 `send_group_msg`，同样被拦）、全局 OneBot
错误码映射表、自动解除禁言。`onebot_send_bubbles()` 的「中途失败即停」语义不变——
`handle()` 只在一条都没发出去时才补发，绝不把已落地的气泡再发一遍。
"""
import json
import os
import time
from pathlib import Path

import requests

import wg_budget  # noqa: E402  (等待预算的唯一出口)
import wg_clock  # noqa: E402  (解禁时刻按同一份渲染口径)
import wg_config  # noqa: E402  (config.json 的唯一读取口)
from wg_paths import state_dir  # noqa: E402

LEDGER = state_dir() / "wg_send_failure.json"
# 解禁后多等几秒：QQ 的禁言判定与本地时钟都有秒级抖动，卡在边界上会白等一轮。
UNMUTE_MARGIN = 5
# 只读入口的复查最短间隔（issue #145）：同一分钟内被反复叫起时不再重复打接口。
RECHECK_AFTER = 60
# 打桩点：`--wait-unmute` 的睡眠由这里走，门禁按调用记录判定「预算内不硬等」。
SLEEP = time.sleep


def _first_line(value):
    """OneBot 的 message 常常是多行（含 traceback），只留首行。"""
    text = str(value or "").strip()
    return text.splitlines()[0].strip() if text else ""


def note(sink, *, data=None, exc=None, message=""):
    """把一次失败的原始事实写进调用方给的 sink（`None` 时什么都不做）。

    sink 由调用方持有而不是模块全局：`onebot_send()` 的返回值仍是 bool，
    现有调用点与门禁（`check_reply_context.py`）的契约一条不动。
    """
    if sink is None:
        return
    sink.clear()
    if exc is not None:
        sink.update(kind="network", retcode=None,
                    message=f"{type(exc).__name__}: {exc}", wording="")
    elif data is not None:
        sink.update(kind="onebot", retcode=data.get("retcode"),
                    message=_first_line(data.get("message")),
                    wording=_first_line(data.get("wording")))
    else:
        sink.update(kind="config", retcode=None, message=str(message), wording="")


def describe(failure):
    """一行原始事实：`OneBot retcode=200 message=EventChecker Failed: ...`。

    三种失败因此必然打出三句不同的话：禁言带 `EventChecker Failed`、参数错带
    OneBot 的参数提示、超时带异常类名。把它们收敛成一句「带 @ 的消息只能走
    OneBot」正是本 issue 的症状。
    """
    failure = failure or {}
    kind = failure.get("kind") or ""
    if kind == "network":
        return f"OneBot 请求失败 {failure.get('message') or '（无异常信息）'}"
    if kind == "config":
        return f"OneBot 未调用：{failure.get('message') or '配置缺失'}"
    retcode = failure.get("retcode")
    message = failure.get("message") or ""
    wording = failure.get("wording") or ""
    if retcode is None and not message:
        return "OneBot 失败原因未知（响应体里没有 retcode 也没有 message）"
    parts = [f"OneBot retcode={retcode if retcode is not None else '缺失'}"]
    if message:
        parts.append(f"message={message}")
    if wording and wording not in message:
        parts.append(f"wording={wording}")
    return " ".join(parts)


def _post(action, payload, timeout=10):
    """对 OneBot HTTP 发一次只读查询，返回 data 段；失败抛异常（由调用方降级）。"""
    cfg = wg_config.config()
    base = str(cfg.get("onebot_http_url") or "").rstrip("/")
    token = str(cfg.get("onebot_token") or "")
    if not base:
        raise RuntimeError("onebot_http_url 未配置")
    resp = requests.post(f"{base}/{action}", json=payload,
                         headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    body = resp.json()
    if body.get("retcode") != 0:
        raise RuntimeError(f"{action} retcode={body.get('retcode')}")
    return body.get("data") or {}


def _all_shut(post, group_id):
    """`get_group_info().group_all_shut`：True / False / None（查不到就是 None）。

    全员禁言与个人禁言是两个信号，**不能只看自己那一个字段**：NapCat 在全员禁言时
    不一定给成员写 `shut_up_timestamp`，只看它会将「全群都发不出去」报成「与禁言
    无关」（独立复核抓到的假事实）。
    """
    try:
        value = post("get_group_info", {"group_id": int(group_id)}).get("group_all_shut")
    except Exception:  # noqa: BLE001  查不到就返回 None，不冒充 False
        return None
    # 字段缺失（老版本 / 裁剪过的响应）同样是「不知道」，不能当 False——否则
    # 「未检出禁言」又变成一句假话（复核遗留 1）。
    return None if value is None else bool(value)


def diagnose(group_id, failure, post=None, now=None):
    """失败之后的禁言诊断：(剩余秒数 or None, 待打印行)。

    只在失败路径上跑，成功路径零额外延迟。四档结论互不冒充：单人/全员禁言（求出
    解禁时刻，全员禁言时解禁时刻可能取不到）、未禁言（成员与群两个探测都成功）、
    状态未知（探测本身失败）——第三、四档不说「未禁言」。
    """
    post = post or _post
    moment = time.time() if now is None else float(now)
    if (failure or {}).get("kind") == "network":
        return None, ["  未探测禁言：请求在拿到响应之前就失败了，不是 OneBot 的业务拒绝"]
    account = str(wg_config.config().get("self_account") or "")
    if not account:
        return None, ["  未探测禁言：config.json 缺 self_account"]
    try:
        member = post("get_group_member_info",
                      {"group_id": int(group_id), "user_id": int(account), "no_cache": True})
    except Exception as exc:  # noqa: BLE001  探测失败不许拖垮诊断
        return None, [f"  禁言状态未知：{type(exc).__name__}: {exc}"]
    try:
        until = int(member.get("shut_up_timestamp") or 0)
    except (TypeError, ValueError):
        until = 0
    seconds = wg_clock.seconds_left(until, moment) if until > moment else None
    scope = _all_shut(post, group_id)
    if seconds is None:
        if scope is True:
            return None, ["  全员禁言：整个群都发不出去"
                          f"（本机 shut_up_timestamp={until}，解禁时刻以管理员解禁为准）"]
        if scope is False:
            return None, [f"  未检出禁言（shut_up_timestamp={until}，group_all_shut=False）："
                          "失败来自 OneBot 的其它原因，见上一行原文"]
        return None, [f"  禁言状态未知：读不到 group_all_shut（查询失败或响应缺字段），"
                      f"本机 shut_up_timestamp={until}"]
    lines = [f"  机器人被禁言至 {wg_clock.clock(until)}（还剩 {seconds} 秒），"
             f"期间任何通道都发不出去"]
    if scope is True:
        lines.append("  全员禁言：整个群都发不出去，等解禁即可")
    elif scope is False:
        lines.append("  单人禁言：其他成员仍能发言，只有本机被拦")
    lines.append(f"  建议：等 {seconds + UNMUTE_MARGIN} 秒后重发"
                 "（--wait-unmute 在等待预算内会自动等）")
    return seconds, lines


def wait_plan(mute, budget=None, elapsed=0.0):
    """解禁要等多久、这次调用等不等得起：(等待秒数 or None, 待打印行)。

    「等不起」不是失败，是边界事实：宿主在 `AGENT_TOOL_TIMEOUT` 处砍进程树，
    硬睡到解禁等于把「发不出去」变成「进程静默消失」（#79）。`elapsed` 是这次调用
    已经用掉的秒数（诊断与发送本身就吃掉一部分）——额度是**整次调用共享的总额**，
    所以走 `wg_budget.clamp_wait()` 而不是自己减预算（#88）。
    """
    budget = wg_budget.WAIT_BUDGET if budget is None else int(budget)
    need = int(mute) + UNMUTE_MARGIN
    # 两个上限都要满足：调用方给的预算（本模块里恒等于 WAIT_BUDGET，但形参不能只进文案）
    # 与整次调用剩余额度（clamp_wait 按 elapsed 算，#88）。
    allowance = min(int(budget), wg_budget.clamp_wait(need, elapsed))
    if allowance < need:
        return None, [f"  需等 {need} 秒才解禁，超出单次调用等待预算 {budget}s"
                      f"（已用 {max(0, int(elapsed))}s，剩 {allowance}s）："
                      f"这次不硬等，请稍后重挂再发（失败已记进 {LEDGER.name}）"]
    return need, [f"  等 {need} 秒解禁后补发（--wait-unmute，等待预算 {budget}s 内）…"]


def record(group_id, failure, mute=None, mute_until=None, recovered=None, now=None):
    """落盘上一次发送失败（原子写）。写不进不崩——账本不该让 reply.py 挂。

    `mute_until` 是**绝对时刻**，由调用方算一次传进来：账本如果每写一次就拿
    「现在 + 剩余秒数」重算，睡过一轮后那个时刻会往后漂（独立复核抓到的问题）。
    """
    moment = int(time.time() if now is None else now)
    if mute_until is None and mute:
        mute_until = moment + int(mute)
    entry = {"at": moment, "group_id": str(group_id or ""),
             "kind": (failure or {}).get("kind") or "",
             "retcode": (failure or {}).get("retcode"),
             "reason": describe(failure),
             "mute_until": int(mute_until) if mute_until else None,
             "recovered": recovered,
             # 复查四件套（issue #145）：`recheck_result` 是结论记号
             # （`muted` / `released` / `unknown`），`recovered_at` 是「实测解禁」
             # 的时刻，`rechecked_at` 是最后一次复查尝试（成功与否都写，用来限流），
             # `group_shut` 是那次复查读到的 group_all_shut。新失败从零开始记。
             "recheck_result": None,
             "recovered_at": None,
             "rechecked_at": None,
             "group_shut": None}
    _write(entry)


def _write(entry):
    """原子替换账本；写不进返回 False（调用方不许因此崩）。

    issue #169 PD：合流到 `wg_daemon.write_json`（唯一临时名 + os.replace）。
    """
    import wg_daemon
    return wg_daemon.write_json(LEDGER, entry)


def read():
    """当前记录（dict）；文件不存在或读坏时 None。"""
    try:
        data = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear():
    """删掉记录（测试用；生产路径只写不删）。"""
    try:
        Path(LEDGER).unlink()
    except OSError:
        pass


def last_line(now=None):
    """`reply.py --list` 表头的一行：上一次为什么没发出去（没有记录时返回空串）。

    空串=不造假事实：与 `wg_gap.describe()` 同口径，恒加注脚的实现红在「一次都没
    失败过也报一句」上。

    禁言尾巴按**现在**分档（issue #145）：`mute_until` 是失败那一刻冻结的预测，
    过期之后继续打「禁言至 HH:MM:SS」等于把一小时前的推断当成此刻的事实。四档
    互不冒充：仍在禁言（未来时刻）/ 实测解禁（`recovered_at`）/ 到点未复查 /
    复查未能确认。
    """
    moment = time.time() if now is None else float(now)
    rec = read()
    if not rec:
        return ""
    tail = _mute_tail(rec, moment)
    if rec.get("recovered") is True and not rec.get("recovered_at"):
        tail += "，本次调用内已补发成功"
    elif rec.get("recovered") is False:
        tail += "，补发未成功"
    return (f"# 上次发送失败：{wg_clock.stamp(rec.get('at'))} "
            f"group={rec.get('group_id') or '?'} {rec.get('reason') or '原因未记录'}{tail}")


def _mute_tail(rec, moment):
    """禁言那一段的话术：只在拿得到事实时下结论，拿不到就说「未确认」。

    `recheck_result` 是复查结论的唯一记号（`muted` / `released` / `unknown`）：
    没有它就分不清「禁言至 X」是失败那一刻的探测、还是刚刚复查确认的事实。
    """
    until = rec.get("mute_until")
    try:
        until = int(until) if until else 0
    except (TypeError, ValueError):
        until = 0
    recovered_at = rec.get("recovered_at")
    rechecked_at = rec.get("rechecked_at")
    result = rec.get("recheck_result")
    stamp = wg_clock.clock(rechecked_at) if rechecked_at else ""
    if recovered_at:
        return (f"；禁言已于 {wg_clock.clock(recovered_at)} 实测解禁"
                + (f"（原判 {wg_clock.clock(until)}）" if until else ""))
    if rec.get("recovered") is True:
        # 补发成功本身就是「禁言不再是障碍」的最强证据，禁言尾巴这时只剩噪音。
        return ""
    if until > moment:
        left = f"；禁言至 {wg_clock.clock(until)}（还剩 {int(until - moment)} 秒"
        if result == "muted":
            return f"{left}，{stamp} 复查仍禁言）"
        if result == "unknown":
            return f"{left}，{stamp} 复查未能确认）"
        # 没有复查记录：账本刚写，这个时刻是 diagnose() 在失败那一刻探到的。
        return f"{left}，据失败时探测）"
    if rec.get("group_shut") is True:
        return f"；全员禁言，解禁时刻未知" + (f"（{stamp} 复查）" if stamp else "")
    if result == "muted":
        # 复查过、结论是「仍禁言」，但那个预测时刻已经过去了：既不能说「未复查」
        # （明明查了），也不能继续断言禁言（预测已到点）。
        return (f"；{stamp} 复查仍禁言，预测 {wg_clock.clock(until)} 已到点"
                if until else f"；{stamp} 复查仍禁言")
    if result == "unknown":
        return (f"；禁言应已于 {wg_clock.clock(until)} 解除，"
                f"{stamp} 复查未能确认" if until else f"；{stamp} 复查未能确认禁言状态")
    if until:
        return f"；禁言应已于 {wg_clock.clock(until)} 解除（未复查）"
    return ""


def recheck(post=None, now=None):
    """账本上的禁言还没定论，就现查一次 OneBot 并把结论写回账本（issue #145）。

    issue #145 的现场：账本把禁言时刻冻结在失败那一刻，管理员**提前**解禁后没有任何
    入口会重新探测；`reply.py --list` 继续报「禁言至 11:18:29」（那个时刻还在未来），
    Agent 据此放弃回待回的唤醒，而此刻 `send_group_msg` 完全能发出去。`recovered`
    字段从 #132 起就写着「供以后核对」，却只有读、没有写。

    **触发条件不能只看「已到点」**：提前解禁时 `mute_until` 必然还在未来，那正是
    issue 的实机症状（账本 11:18 解禁、10:24 实测已是 0）。所以按「这个预测还新不新」
    判：账本写入（`at`）距今超过 `RECHECK_AFTER` 秒就复查一次，与 `mute_until` 在
    未来还是过去无关——预测越老越不可信，而它恰好是 Agent 判断能不能发的唯一依据。

    三个前置（全满足才发请求，只读入口不能变成轮询器）：账本存在且没有定论
    （`recovered is not True`）、是禁言类记录（有 `mute_until`）、距上次复查超过
    `RECHECK_AFTER` 秒。**探测失败同样写 `rechecked_at`**，所以一直失败时也是每分钟
    最多重试一次，不会每次 `--list` 都打接口。

    三档结论互不冒充，沿用 #132 的纪律（假事实比没有输出更坏）：
      - 现查 `shut_up_timestamp` 仍在未来 → `muted`，以现查到的时刻为准（可能被
        重新禁了一次，也可能账本记早了）；
      - 现查为 0 且 `group_all_shut` 明确为 False → `released`，写 `recovered=True`
        + `recovered_at`，这是**实测**解禁，不是推断；
      - 探测失败 / `group_all_shut` 读不到 / 全员禁言 → `unknown`，措辞降级成
        「复查未能确认」，不冒充解禁。

    返回是否改写了账本；任何异常都吞掉——只读入口不许因为一次探测失败整条崩。
    """
    moment = time.time() if now is None else float(now)
    rec = read()
    if not rec or rec.get("recovered") is True:
        return False
    try:
        until = int(rec.get("mute_until") or 0)
    except (TypeError, ValueError):
        return False
    if not until:
        return False  # 不是禁言类失败（参数错 / 网络错），复查没有对象
    try:
        wrote_at = int(rec.get("at") or 0)
    except (TypeError, ValueError):
        wrote_at = 0
    # 账本刚写：diagnose() 那一刻已经查过，别紧接着再问一遍。
    if wrote_at and moment - wrote_at < RECHECK_AFTER:
        return False
    last_try = rec.get("rechecked_at")
    try:
        if last_try and moment - float(last_try) < RECHECK_AFTER:
            return False
    except (TypeError, ValueError):
        pass
    group_id = str(rec.get("group_id") or "")
    account = str(wg_config.config().get("self_account") or "")
    if not group_id or not account:
        return False
    try:
        gid, uid = int(group_id), int(account)
    except (TypeError, ValueError):
        return False
    post = post or _post
    fresh = dict(rec)
    fresh["rechecked_at"] = int(moment)
    # 先清掉上一轮的结论再探：探测失败时不许让旧的 group_shut 顶着**本次**复查的
    # 时刻继续说话（那是把两次不同的探测缝成一句假事实）。
    fresh["group_shut"] = None
    fresh["recheck_result"] = "unknown"
    try:
        member = post("get_group_member_info",
                      {"group_id": gid, "user_id": uid, "no_cache": True})
    except Exception:  # noqa: BLE001  复查失败只留时间戳，措辞降级成「未能确认」
        return _write(fresh)
    try:
        shut = int(member.get("shut_up_timestamp") or 0)
    except (TypeError, ValueError):
        shut = 0
    scope = _all_shut(post, gid)
    fresh["group_shut"] = scope
    if shut > moment:
        # 现查比账本更新：以现查为准，别拿过期时刻继续骗自己。
        fresh["mute_until"] = shut
        fresh["recheck_result"] = "muted"
    elif scope is False:
        fresh["recovered"] = True
        fresh["recovered_at"] = int(moment)
        fresh["recheck_result"] = "released"
    return _write(fresh)


def print_last():
    """`reply.py --list` 抬头调一次：有失败记录才出声，没有就一句不说。

    issue #145：账本里的禁言时刻是失败那一刻冻结的预测，管理员提前解禁后没有任何
    入口会重探——只读表头于是继续报「禁言至 未来时刻」，把 Agent 挡在能发的消息
    外面。复查放在这里而不是 `reply.py` 的调用点，是因为 `reply.py` 已经贴着
    `check_module_size.py` 的 1000 行红线（issue #11）：这个模块是失败账本的唯一
    归属地，复查属于「账本怎么读」的一部分，写在别处只是把行数搬家。
    """
    recheck()
    line = last_line()
    if line:
        print(line)


def send(group_id, bubble_segments, message_id, *, deliver, has_at, unmute=False):
    """发一轮气泡；首条就没出去时交给 `handle()`。返回最终已发条数。

    `deliver(group_id, bubbles, message_id, failure)` 由调用方给（`reply.py` 的
    OneBot 直发）。失败态用调用方自己的那条气泡序列重发，所以已落地的气泡不会
    再发一遍——这是 `onebot_send_bubbles()`「中途失败即停」的另一面。
    """
    failure = {}

    def attempt(sink=None):
        target = failure if sink is None else sink
        target.clear()
        return deliver(group_id, bubble_segments, message_id, target)

    sent = attempt()
    if sent == len(bubble_segments):
        return sent
    return handle(sent, attempt, failure, group_id=group_id,
                  bubbles=len(bubble_segments), has_at=has_at, unmute=unmute)


def handle(sent, attempt, failure, *, group_id, bubbles, has_at, unmute=False):
    """失败处置的唯一入口：说清原因、诊断禁言、可选解禁补发；返回最终已发条数。

    `attempt()` 由调用方提供（重发必须走同一条气泡序列，否则首条会在群里出现
    两次）；它的可选 sink 让补发那一轮写进自己的 dict，`failure` 留着原始原因——
    账本要记的是「为什么会失败」，不是「补发为什么又失败」。
    """
    if sent:
        print(f"SEND_FAILED: 前 {sent} 条已发出，第 {sent + 1} 条失败，不再重试")
        print(f"  原因：{describe(failure)}")
        record(group_id, failure)
        return sent
    print(f"SEND_FAILED: {describe(failure)}")
    started = time.time()
    mute, lines = diagnose(group_id, failure)
    for line in lines:
        print(line)
    if has_at:
        print("  未退回计划通道：带 @ 的消息只能走 OneBot（计划通道会降级成纯文本）")
    # 解禁时刻只算一次（绝对时刻），睡完再记账也不会漂。
    mute_until = (started + int(mute)) if mute else None
    record(group_id, failure, mute_until=mute_until)
    if not mute or not unmute:
        return sent
    # 诊断的两次探测已经吃掉几秒，额度要按**实际已用**算（#88 的总额口径）。
    wait, plan = wait_plan(mute, elapsed=time.time() - started)
    for line in plan:
        print(line)
    if wait is None:
        return sent
    SLEEP(wait)
    retry_failure = {}
    retried = attempt(retry_failure)
    print(f"  补发：{retried}/{bubbles} 条已发出")
    if retried != bubbles:
        print(f"  补发失败原因：{describe(retry_failure)}")
    record(group_id, failure, mute_until=mute_until, recovered=retried == bubbles)
    return retried
