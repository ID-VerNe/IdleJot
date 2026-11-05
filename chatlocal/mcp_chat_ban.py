"""OneBot ``group_ban`` 通知的事实落盘（issue #390）。

OneBot 11 在禁言 / 解禁时推送 notice：

    {"post_type": "notice", "notice_type": "group_ban",
     "sub_type": "ban" | "lift_ban", "group_id": …, "operator_id": …,
     "user_id": …, "duration": …}

issue #390 之前这条通知在 ``MCPChat.receive`` 落进「不是消息」的早退（``post_type``
门只给 recall 开了口子）：感知禁言全靠发消息失败后探 ``shut_up_timestamp``，既
不知道是谁禁的，更不知道是谁提前解的。本模块只做两件事：

1. ``parse(event)``：相关性判定 + 归一。只认本机（``user_id == self_id``）或全员
   禁言（``user_id == 0``）的 ``ban`` / ``lift_ban``——别人的禁言不是本机的事实。
   ``operator_id`` / ``group_id`` / ``self_id`` 缺一不记：「不知道是谁」的事实落了
   盘，读的人就得替它编一个操作者。
2. ``record_group_ban(event)``：把事实写进 ``state/wg_group_ban.json``（原子替换，
   按群号分组，``ban`` / ``lift_ban`` 各留最新一条）。写失败不抛：接收线程不能
   因为一次磁盘抖动挂掉（同 ``wg_send_failure._write`` 的纪律）。

这份 JSON 的读者在 ``water-group/daemon/wg_group_ban.py``（``reply.py --list`` 的
事实行）。daemon 进程不连事件 WS、chatlocal 又不能 flat import daemon 目录，所以
契约隔着进程边界各持一半，schema 变更两边一起改——与 ``wg_state.json``
（collector 写、up/watch 读）同构。

明确不做（#391 / #392 的地盘）：事件到达即清账本 / 立刻 WAKE / ``probe()`` 多源
校验；到期时刻的渲染在读者那边一律 ``wg_clock.stamp()`` 全日期格式，不碰
``_mute_tail`` 的旧口径。
"""
import json
import os
import tempfile
import time

from .config import STATE

FACTS = STATE / 'wg_group_ban.json'


def _as_id(value):
    """OneBot 的 id 字段可能是 int / str / None：归一成非空十进制字符串。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ''
    return str(number) if number >= 0 else ''


def parse(event, *, now=None):
    """group_ban notice → ``(group_id, fact)``；与本机无关或字段不全返回 None。

    事件到达这里之前已被 ``EventReceiver`` 核过账号（``self_id`` 与账号不符的帧
    会让整条接收停机，见 ``onebot_events.py`` 的 account_mismatch），所以
    ``self_id`` 就是本机账号，不另读 config。
    """
    if not isinstance(event, dict):
        return None
    if event.get('post_type') != 'notice' or event.get('notice_type') != 'group_ban':
        return None
    if event.get('sub_type') not in ('ban', 'lift_ban'):
        return None
    group, operator, user, self_id = (_as_id(event.get(key))
                                      for key in ('group_id', 'operator_id', 'user_id', 'self_id'))
    if not (group and operator and self_id):
        return None
    if user != self_id and user != '0':
        return None
    try:
        duration = max(0, int(event.get('duration') or 0))
    except (TypeError, ValueError):
        duration = 0
    fact = {'action': event['sub_type'], 'at': int(time.time() if now is None else now),
            'operator_id': operator, 'user_id': user, 'duration': duration}
    return group, fact


def record_group_ban(event, *, now=None):
    """落盘一条事实；不相关或写失败返回 False（不抛，见模块 docstring）。"""
    parsed = parse(event, now=now)
    if parsed is None:
        return False
    group, fact = parsed
    data = _read()
    entry = data['groups'].setdefault(group, {})
    entry[fact['action']] = fact
    return _write(data)


def _read():
    """整份事实文件；文件不存在 / 读坏时返回空骨架，不把坏文件传染成异常。"""
    try:
        data = json.loads(FACTS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = None
    groups = data.get('groups') if isinstance(data, dict) else None
    return {'groups': groups if isinstance(groups, dict) else {}}


def _write(data):
    """原子替换（同目录临时名 + os.replace）；失败返回 False，不抛给接收线程。"""
    try:
        FACTS.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(FACTS.parent), prefix=FACTS.name + '.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False)
            os.replace(tmp, FACTS)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:  # noqa: BLE001  丢一条事实可由下一条事件补，挂掉接收线程不可补
        return False
    return True
