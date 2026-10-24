"""每用户扫码凭证存储（issue #6 / .plan/user-ck-qr-login.md §4.4）。

一个群友一个文件：`~/.water-group/users/<qq>.json`。`auth` 字段整体 DPAPI 加密
（win32crypt.CryptProtectData，绑定当前 Windows 用户）后 base64 落盘——文件被拷到
别的机器/用户下无法解密；配合用户目录隔离构成双重保护（Windows 上无谓的 600 权限
位依赖 DPAPI 已足够，§4.4）。

边界（写死在文档里，别悄悄扩）：
  - 本模块只认 digits QQ 号，拒绝一切路径穿越形状（`../`、带斜杠、空号）；
  - 日志、异常、返回值里绝不出现 auth 任何字段——调用方只拿结构化字典；
  - 解密/解析失败一律按「未绑定」处理（返回 None），损坏文件不炸调用方；
  - 保存走 tmp + os.replace 原子替换，崩溃不留半个 JSON。
"""
import base64
import json
import os
import re
import time

from wg_paths import users_dir

QQ_RE = re.compile(r"^\d{5,12}$")

# auth 里允许落盘的字段（扫码 Set-Cookie / stoken 派生，plan §3.2/§3.3）。
# 未知字段一律丢弃：存储面只放已知形状，上游加字段也拿不准语义。
AUTH_FIELDS = ("account_id", "ltuid", "ltoken", "cookie_token",
               "account_mid_v2", "stoken", "stuid")

GAMES = ("genshin", "starrail", "zzz")


def safe_qq(qq):
    """归一化 QQ 号；不是 5-12 位数字返回 None（路径穿越防护）。"""
    qq = str(qq or "").strip()
    return qq if QQ_RE.match(qq) else None


def user_path(qq):
    """users/<qq>.json；qq 非法时抛 ValueError（保存路径不容错）。"""
    normalized = safe_qq(qq)
    if not normalized:
        raise ValueError(f"非法 QQ 号: {qq!r}")
    return users_dir() / f"{normalized}.json"


def _dpapi_encrypt(raw: bytes) -> str:
    import win32crypt  # pywin32，Windows-only 依赖（pyproject 已钉）

    blob = win32crypt.CryptProtectData(raw, "idlejot-water-group-user-ck")
    if isinstance(blob, str):
        blob = blob.encode("latin-1")
    return base64.b64encode(blob).decode("ascii")


def _dpapi_decrypt(blob_b64: str) -> bytes:
    import win32crypt

    blob = base64.b64decode(blob_b64)
    out = win32crypt.CryptUnprotectData(blob)
    # pywin32 的返回是 (描述, 明文)；描述是 CryptProtectData 传进去的那串
    raw = out[1] if isinstance(out, tuple) and len(out) > 1 else out
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return raw


def _encrypt_auth(auth: dict) -> str:
    raw = json.dumps(auth, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _dpapi_encrypt(raw)


def _decrypt_auth(blob_b64: str) -> dict:
    raw = _dpapi_decrypt(blob_b64)
    auth = json.loads(raw.decode("utf-8"))
    return auth if isinstance(auth, dict) else {}


def normalize_auth(auth) -> dict:
    """只留已知字段的字符串值；空值丢弃。"""
    src = auth if isinstance(auth, dict) else {}
    return {k: str(src[k]) for k in AUTH_FIELDS if str(src.get(k) or "").strip()}


def save_user(qq, record):
    """写入绑定记录。record 的 auth 是明文 dict，落盘前整体 DPAPI 加密。"""
    path = user_path(qq)
    normalized = safe_qq(qq)
    auth = normalize_auth((record or {}).get("auth"))
    if not auth:
        raise ValueError("拒绝保存空凭证记录")
    payload = {
        "qq": normalized,
        "bound_at": int((record or {}).get("bound_at") or time.time()),
        "device_id": str((record or {}).get("device_id") or ""),
        "auth": _encrypt_auth(auth),
        "auth_encrypted": True,
        "uids": _normalize_uids((record or {}).get("uids")),
        "active_uid": _normalize_active((record or {}).get("active_uid")),
        "stoken_expires_at": (record or {}).get("stoken_expires_at"),
        # 续期与失效提醒的时间轴（#41-C）：只存整数时间戳，字段语义由 qr_login 定。
        "last_renew_at": (record or {}).get("last_renew_at"),
        "ck_invalid_at": (record or {}).get("ck_invalid_at"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return payload


def load_user(qq):
    """读绑定记录；auth 解密后随记录返回。缺失/损坏/解密失败 → None。"""
    normalized = safe_qq(qq)
    if not normalized:
        return None
    path = users_dir() / f"{normalized}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("auth"):
        return None
    record = dict(payload)
    try:
        record["auth"] = _decrypt_auth(str(payload["auth"]))
    except Exception:  # noqa: BLE001  换机器/换用户/文件被改：按未绑定处理
        return None
    record["qq"] = normalized
    return record


def delete_user(qq):
    """删除绑定记录；返回是否删掉了文件。"""
    normalized = safe_qq(qq)
    if not normalized:
        return False
    path = users_dir() / f"{normalized}.json"
    try:
        path.unlink()
        return True
    except OSError:
        return False


def ck_string(record) -> str:
    """auth → miyoushe.Client 吃的 Cookie 字符串（plan §3.3：取数层零改动）。"""
    auth = (record or {}).get("auth") or {}
    return "; ".join(f"{k}={auth[k]}" for k in AUTH_FIELDS
                     if str(auth.get(k) or "").strip())


def active_uid(record, game="genshin") -> str:
    """当前生效 uid；没显式设过就退回唯一 discovered uid（降级路径兜底）。"""
    active = ((record or {}).get("active_uid") or {}).get(game) or ""
    if str(active).strip():
        return str(active)
    uids = ((record or {}).get("uids") or {}).get(game) or []
    return str(uids[0]) if uids else ""


def bind_uid(record, uid, game="genshin"):
    """把 uid 记进记录并设为该游戏 active_uid，返回新记录（不落盘）。

    UID 只是取数参数，不是凭证：调用方必须先确认 record 里有 auth（已扫码），
    否则单绑 UID 取不到任何数据。
    """
    record = dict(record or {})
    uids = {key: list(value or []) for key, value in (record.get("uids") or {}).items()}
    values = uids.setdefault(game, [])
    uid = str(uid)
    if uid not in values:
        values.append(uid)
    record["uids"] = uids
    active = dict(record.get("active_uid") or {})
    active[game] = uid
    record["active_uid"] = active
    return record


def _normalize_uids(uids) -> dict:
    src = uids if isinstance(uids, dict) else {}
    out = {}
    for game in GAMES:
        values = [str(u) for u in (src.get(game) or []) if str(u or "").strip()]
        if values:
            out[game] = values
    return out


def _normalize_active(active) -> dict:
    src = active if isinstance(active, dict) else {}
    return {g: str(v) for g, v in src.items() if g in GAMES and str(v or "").strip()}
