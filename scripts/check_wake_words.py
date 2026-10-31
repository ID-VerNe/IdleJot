"""issue #279 gate: self-name / wake-word 合并规则在全仓单一出处。

改前 daemon ``wg_up_wake.wake_words()`` 只取顶层 ``self_names``，per-group
``wake_words`` 配了但顶层没配的群 daemon 不认（issue #279 复现命令就是
``w.wake_words.__doc__ or 'returns config().get(self_names) only'``）。本门禁
钉三组对照：

  1. 只顶层 ``self_names``：``wake_reason`` 仍认（A 不破坏顶层判定）。
  2. per-group ``wake_words``（顶层不配该词）：改前 daemon 返回空串（漏认），
     改后返回 ``keyword:<词>``——issue 的真实差异，翻转用例。
  3. per-group ``self_names`` 优先于 ``wake_words``（``or`` 短路，与
     ``mcp_chat_wait.py:144`` 同口径）。

用例 2 是判据翻转：改前必须红、改后必须绿。用例 1、3 是行为不变 / 口径对齐。
persona 不单独设用例（daemon 事件流不带 ``persona_name``，preset id 不会出现在
群友正文里，钉不住翻转——见 issue #279 plan 追评的"daemon persona_name 缺口"）。

    .venv/Scripts/python.exe scripts/check_wake_words.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_fixture_support  # noqa: F401,E402  (先钉数据根，见 issue #79/#89)

sys.path.insert(0, str(ROOT / "water-group" / "daemon"))

import wg_up_wake  # noqa: E402
import wg_wake_words  # noqa: E402  (issue #279: 合并规则的单一出处)

SELF = "10001"
ACCOUNT = "10001"
GROUP = "222"


def _reset_config(cfg):
    """钉 config 到给定 dict（wg_up_wake.config 委托 wg_config，但测试直接打桩
    wg_up_wake.config 即可让 wake_words / wake_reason 都读到）。"""
    wg_up_wake.config = lambda: cfg


def case_top_only():
    """只顶层 self_names：wake_reason 认顶层关键词（A 不破坏顶层判定）。"""
    _reset_config({"self_account": SELF, "self_names": ["鲸鱼"],
                   "slash_commands": {"prefix": "#"}})
    ev = {"is_self": False, "content": "鲸鱼 在吗",
          "conversation_id": f"{ACCOUNT}:group:{GROUP}"}
    got = wg_up_wake.wake_reason(ev)
    ok = got == "keyword:鲸鱼"
    print(("PASS" if ok else "FAIL"), "只顶层 self_names ->", repr(got))
    return ok


def case_per_group_wake_words():
    """per-group wake_words（顶层不配该词）：改前 daemon 漏认返回 ''，改后认。

    这是 issue #279 复现命令指向的差异——翻转用例：改前红、改后绿。
    """
    _reset_config({"self_account": SELF, "self_names": ["鲸鱼"],
                   "slash_commands": {"prefix": "#"},
                   "groups": {GROUP: {"enabled": True, "wake_words": ["群专属"]}}})
    ev = {"is_self": False, "content": "群专属 在吗",
          "conversation_id": f"{ACCOUNT}:group:{GROUP}"}
    got = wg_up_wake.wake_reason(ev)
    ok = got == "keyword:群专属"
    print(("PASS" if ok else "FAIL"), "per-group wake_words ->", repr(got),
          "" if ok else "(want 'keyword:群专属')")
    return ok


def case_per_group_self_names_over_wake_words():
    """per-group self_names 优先于 wake_words（or 短路，与 mcp_chat_wait:144 同口径）。

    配了 self_names 就不读 wake_words：正文命中 self_names、不命中 wake_words。
    """
    _reset_config({"self_account": SELF, "self_names": ["鲸鱼"],
                   "slash_commands": {"prefix": "#"},
                   "groups": {GROUP: {"enabled": True,
                                      "self_names": ["本群名"],
                                      "wake_words": ["群专属"]}}})
    ev_hit = {"is_self": False, "content": "本群名 在吗",
              "conversation_id": f"{ACCOUNT}:group:{GROUP}"}
    ev_miss = {"is_self": False, "content": "群专属 在吗",
               "conversation_id": f"{ACCOUNT}:group:{GROUP}"}
    got_hit = wg_up_wake.wake_reason(ev_hit)
    got_miss = wg_up_wake.wake_reason(ev_miss)
    ok = got_hit == "keyword:本群名" and got_miss == ""
    print(("PASS" if ok else "FAIL"),
          "per-group self_names 优先于 wake_words ->",
          repr(got_hit), repr(got_miss))
    return ok


def case_helper_direct():
    """wg_wake_words.wake_keywords 直接验合并规则：顶层 + per-group + persona + account。

    casefold set；cfg_account 占位符（YOUR_）不进词表；account 进词表。
    """
    kw = wg_wake_words.wake_keywords(
        cfg={"self_names": ["鲸鱼"], "self_account": "YOUR_QQ_ACCOUNT",
             "groups": {GROUP: {"wake_words": ["群专属"]}}},
        group_id=GROUP, persona_name="小鲸鱼2号", account=ACCOUNT)
    # persona_name 整体进词表 + split 后的 token（"小鲸鱼2号" 无空白，split 只回自身，
    # 所以这里只多一个 token）；account 进；YOUR_ 占位符不进。
    ok = (kw == {"鲸鱼", "群专属", "小鲸鱼2号", ACCOUNT}
          and "your_qq_account" not in kw)
    print(("PASS" if ok else "FAIL"), "helper 合并规则 ->", sorted(kw))
    return ok


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cases = [case_top_only, case_per_group_wake_words,
             case_per_group_self_names_over_wake_words, case_helper_direct]
    bad = 0
    for fn in cases:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001  门禁自己坏了也算红
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
            ok = False
        bad += 0 if ok else 1
    print(f"{len(cases) - bad}/{len(cases)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
