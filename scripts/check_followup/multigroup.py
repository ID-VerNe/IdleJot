"""多群追问窗口：从 `__init__.py` 原样搬出的 #206 用例（issue #320 §A）。

这一族自成一档：它不碰本进程的 `followup` 窗口状态，而是**另建临时库 + 另起子进程**
复现「一个会话只看一个群」（#164 / #206）的现场。夹具名（`TMP` / `ROOT` /
`scratch_dir` / `pin_state_env`）一律从 `._fixture` 取，不在这里另建一套。

搬动只换容器、不换正文：docstring、断言、`print("  ok  <desc>")` 逐字保留，
调用序仍由 `__init__.main()` 那张表决定。
"""
import json
import os
import sqlite3
import subprocess
import sys

from ._fixture import ROOT, TMP, pin_state_env, scratch_dir


def check_multigroup_session_pinned():
    """issue #206：多群机器上 active_session_id() 必须按本会话钉死的群过滤。

    复现 issue #206 的现场：`chat_sessions` 里两条 `active=1`（一会话一群，#164），
    改前 `active_session_id()` 的 SQL 没有群过滤，取到的是「后创建的那一个」——
    钉死群 111 的会话把追问窗口绑到群 222 的会话上，`since()` 整窗口读别群，
    `stale()` 还会因那条会话重建而误判收窗。

    本夹具钉死群 111（env），库里塞两条 active 会话（111 / 222），断言：
      - `active_session_id()` 取本群那条（s111），不是整表最后一行的 s222；
      - `open()` 默认走 active_session_id，窗口 session = s111；
      - `since(s111)` 不会返回群 222 的消息；
      - `stale()` 在本群会话存活时为 None。
    对照组：把 env 撤掉（未钉死 + 多群）退化成改前整表取最后一行——
    单群机器不触发本 bug，但多群未钉死也不该静默选群，这里只断言「行为退化、
    不报错」，与 `wg_isolation.note()` 的口径一致（未钉死时读侧不隔离）。
    """
    import wg_isolation

    saved_env = os.environ.pop("IDLEJOT_WG_CONVERSATION", None)
    saved_env2 = os.environ.pop("TULPA_WG_CONVERSATION", None)
    # 临时库 + 临时状态目录：本用例不能共用 TMP——会污染既有窗口文件。
    # 走 scratch_dir（#322 C）：与 _fixture 同一套 atexit 清理，不再手写 mkdtemp
    # ——手写那份一旦在 pin_state_env 之前抛异常，目录就没人回收了。
    tmp2 = scratch_dir("wg_followup_multigroup_")
    db2 = tmp2 / "multigroup.sqlite3"
    saved_db = os.environ.get("IDLEJOT_WG_DB")
    saved_state = os.environ.get("IDLEJOT_WG_STATE")
    os.environ["IDLEJOT_WG_DB"] = str(db2)
    os.environ["TULPA_WG_DB"] = str(db2)
    pin_state_env(tmp2)  # state 两腿都钉（#281），还原仍按 saved_state 复原
    os.environ["IDLEJOT_WG_CONVERSATION"] = "111:group:111"
    try:
        conn = sqlite3.connect(db2)
        conn.execute("CREATE TABLE chat_sessions (id TEXT, conversation_id TEXT, active INTEGER)")
        conn.execute("CREATE TABLE chat_inbox (id INTEGER PRIMARY KEY, session_id TEXT, payload TEXT, at REAL NOT NULL, media TEXT NOT NULL DEFAULT '[]')")
        conn.execute("INSERT INTO chat_sessions VALUES ('s111', '111:group:111', 1)")
        conn.execute("INSERT INTO chat_sessions VALUES ('s222', '111:group:222', 1)")
        # s111 的接话：在锚点之后、属于本群
        conn.execute("INSERT INTO chat_inbox VALUES (101, 's111', ?, 1792500004.0, '[]')",
                     (json.dumps({"sender": "甲", "content": "本群接话", "conversation_id": "111:group:111", "timestamp": 1792500004000}, ensure_ascii=False),))
        # s222 的闲聊：同 id 空间里、属于别群——绝不能被 since(s111) 捞到
        conn.execute("INSERT INTO chat_inbox VALUES (102, 's222', ?, 1792500005.0, '[]')",
                     (json.dumps({"sender": "乙", "content": "别群闲聊", "conversation_id": "111:group:222", "timestamp": 1792500005000}, ensure_ascii=False),))
        conn.commit()
        conn.close()

        # 重读 DB / WINDOW：本用例改了 os.environ，但 followup 的 DB / WINDOW 是
        # import 期求值的——同一进程里改不了它们指向，只能在子进程里验。
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        script = tmp2 / "probe.py"
        script.write_text(
            "import sys; sys.path.insert(0, r'" + str(ROOT / "water-group" / "daemon") + "');\n"
            "import followup, json, sqlite3;\n"
            "print('active=', followup.active_session_id());\n"
            "w = followup.open(100, max_messages=3, idle_seconds=600);\n"
            "print('session=', w['session']);\n"
            "rows = followup.since(100, w['session']);\n"
            "print('ids=', [r['id'] for r in rows]);\n"
            "print('convs=', [r.get('conversation_id') for r in rows]);\n"
            "print('stale=', followup.stale(w));\n"
            "followup.close();\n",
            encoding="utf-8")
        proc = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                              env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        out = proc.stdout or ""
        err = proc.stderr or ""
        assert proc.returncode == 0, f"probe 退出 {proc.returncode}:\n{out}\n{err}"
        assert "active= s111" in out, f"未按本群过滤，取到别群会话：{out}"
        assert "session= s111" in out, f"窗口绑到别群会话：{out}"
        assert "ids= [101]" in out, f"since() 混进了别群消息：{out}"
        assert "111:group:222" not in out, f"别群 conversation_id 漏进追问行：{out}"
        assert "stale= None" in out, f"本群会话存活却判 stale：{out}"

        # 对照组：撤掉 env + 给一份「两个群都 enabled」的夹具 config——
        # `wg_isolation.resolve()` 第三档要求**唯一**启用群，两个都不唯一，落到
        # 未钉死（`ambiguous()` 为真），`active_session_id()` 于是退化成改前的整表
        # 取最后一行（s222）。这才是 #206 的真实现场：多群未钉死。
        # 改前这里只 `assert "active=" in out2`——撤回 env 后第三档若被真实 config
        # 顶成「唯一启用群」，s111 正好命中，断言照样绿，「未钉死」根本没被验到
        # （#322 B：对照组假绿）。现在钉住具体会话，两条分支才互相排斥。
        cfg = tmp2 / "config-multigroup.json"
        cfg.write_text(json.dumps({"self_account": "111", "groups": {
            "111": {"enabled": True}, "222": {"enabled": True}}}), encoding="utf-8")
        env2 = dict(env)
        env2["IDLEJOT_WG_CONFIG"] = str(cfg)
        for key in ("IDLEJOT_WG_CONVERSATION", "TULPA_WG_CONVERSATION"):
            env2.pop(key, None)
        proc2 = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                               env=env2, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
        out2 = proc2.stdout or ""
        assert proc2.returncode == 0, f"未钉死退化崩溃：{out2}\n{proc2.stderr}"
        # 未钉死时 active_session_id 退化成整表取最后一行（s222）——改前行为，
        # 与单群机器等价。bug 只在多群未钉死时由 wg_isolation.note() 告警，不在这里拦；
        # 但「确实退化了」必须被钉住，否则上一段的过滤断言可能根本没生效过。
        assert "active= s222" in out2, f"未钉死没退化成整表最后一行：{out2}"
    finally:
        for key, value in (("IDLEJOT_WG_DB", saved_db),
                           ("TULPA_WG_DB", saved_db),
                           ("IDLEJOT_WG_STATE", saved_state),
                           ("TULPA_WG_STATE", saved_state)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if saved_env is not None:
            os.environ["IDLEJOT_WG_CONVERSATION"] = saved_env
        else:
            os.environ.pop("IDLEJOT_WG_CONVERSATION", None)
        if saved_env2 is not None:
            os.environ["TULPA_WG_CONVERSATION"] = saved_env2
        else:
            os.environ.pop("TULPA_WG_CONVERSATION", None)
        import shutil
        shutil.rmtree(tmp2, ignore_errors=True)
    print("  ok  多群追问窗口绑本群（active_session_id 按 wg_isolation 过滤，issue #206）")
