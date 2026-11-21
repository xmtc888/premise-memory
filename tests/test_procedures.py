# -*- coding: utf-8 -*-
"""procedure 挖掘的契约测试。

两条判据都有实测依据，改动它们会破坏"信号 vs 噪音"的平衡：
  · 相似度约束（Jaccard >= 0.5）：不加会把"换任务"误判成"修复"（实测 1776→979）
  · 归一化保留写法差异：`python -c` 与 `.\\venv\\Scripts\\python -c` 必须不同
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import procedures                                              # noqa: E402
from store import Store                                        # noqa: E402


def test_normalize_keeps_interpreter_difference():
    """归一化必须保留"用哪个解释器"这个差异 —— 这正是要学的知识。"""
    a = procedures.normalize_command(r'python -c "import x"')
    b = procedures.normalize_command(r'.\venv\Scripts\python.exe -c "import x"')
    assert a != b
    assert "python" in a and "venv" in b


def test_normalize_masks_args():
    """每次不同的参数必须被抹掉，否则同一条步骤会被拆成无数条。"""
    a = procedures.normalize_command(r'cd F:\proj; python run.py 2>&1')
    b = procedures.normalize_command(r'cd F:\other; python run.py 2>&1')
    assert a == b


def test_token_overlap_judges_task_switch():
    """换任务（脚本名全变）重合率低；改参数（只差一小段）重合率高。"""
    fix_a = procedures.normalize_command("python tools/a.py 2>&1 | Select-Object -First 20")
    fix_b = procedures.normalize_command("python tools/a.py 2>&1 | Select-Object -Last 5")
    assert procedures._token_overlap(fix_a, fix_b) >= 0.5

    other = procedures.normalize_command("python tools/b.py 2>&1 | Out-String")
    assert procedures._token_overlap(fix_a, other) < 0.5


def _mk_store(tmp_path):
    st = Store(str(tmp_path / "p.db"))
    return st


def _insert_session(con, key, cwd="F:\\proj"):
    con.execute(
        "INSERT INTO sessions (session_key,agent,session_id,cwd,indexed_at) "
        "VALUES (?,?,?,?,?)", (key, "dsh", key.split(":", 1)[1], cwd, "2026-11-17T00:00:00Z"))


def _insert_call(con, key, seq, cmd, ok, code=0):
    con.execute(
        "INSERT INTO tool_calls (session_key,seq,ts,tool,command,ok,exit_code) "
        "VALUES (?,?,?,?,?,?,?)", (key, seq, "2026-11-17T00:00:00Z", "pwsh", cmd, ok, code))


def test_mine_repairs_pairs_failure_with_fix(tmp_path):
    st = _mk_store(tmp_path)
    con = st.con
    _insert_session(con, "dsh:s1")
    _insert_call(con, "dsh:s1", 0, "python tools/x.py | Select-Object -First 20", 0, 1)
    _insert_call(con, "dsh:s1", 1, "python tools/x.py | Select-Object -Last 5", 1, 0)
    con.commit()
    rows = procedures.mine_repairs(con)
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["kind"] == "repair" and r["raw_hits"] == 1 and r["exit_codes"] == [1]
    assert "first" in r["bad"] and "last" in r["good"]
    st.close()


def test_mine_repairs_skips_task_switch(tmp_path):
    """换了脚本的"成功"不是修复，不能被配成一对（相似度闸门）。"""
    st = _mk_store(tmp_path)
    con = st.con
    _insert_session(con, "dsh:s2")
    _insert_call(con, "dsh:s2", 0, "python tools/aaa.py 2>&1 | Select-Object -First 20", 0, 1)
    _insert_call(con, "dsh:s2", 1, "python tools/bbb.py 2>&1 | Out-String", 1, 0)
    con.commit()
    assert procedures.mine_repairs(con) == []
    st.close()


def test_mine_repairs_rejects_wrapper_with_other_script(tmp_path):
    """包装式命令（powershell -Command "<脚本>"）换脚本 ≠ 修复。

    2026-11-17 多 agent 数据实测的假阳性：归一化把引号内容抹成 `<q>`，
    任何脚本都长得一样；必须用保留引号内容的字符相似度才能挡住。
    """
    st = _mk_store(tmp_path)
    con = st.con
    _insert_session(con, "dsh:w1")
    _insert_call(con, "dsh:w1", 0,
                 'powershell -NoProfile -Command "& C:/py.exe -m pip install einops --quiet"',
                 0, 1)
    _insert_call(con, "dsh:w1", 1,
                 'powershell -NoProfile -Command "& C:/py.exe -c \'import torch; print(torch)\'"',
                 1, 0)
    con.commit()
    assert procedures.mine_repairs(con) == []
    st.close()


def test_mine_repairs_accepts_trivial_fix(tmp_path):
    """真修复（同一命令加一段输出截取）要被接受。"""
    st = _mk_store(tmp_path)
    con = st.con
    _insert_session(con, "dsh:f1")
    _insert_call(con, "dsh:f1", 0, "npx vite build 2>&1", 0, 1)
    _insert_call(con, "dsh:f1", 1, "npx vite build 2>&1 | tail -3", 1, 0)
    con.commit()
    rows = procedures.mine_repairs(con)
    assert len(rows) == 1, rows
    assert "tail" in rows[0]["good"]
    st.close()


def test_mine_rituals_groups_by_cwd(tmp_path):
    """仪式按目录分组：不同项目的 cd 目标不能混成一条。"""
    st = _mk_store(tmp_path)
    con = st.con
    for i, cwd in enumerate(["F:\\a", "F:\\a", "F:\\b", "F:\\b"]):
        key = "dsh:r%d" % i
        _insert_session(con, key, cwd=cwd)
        _insert_call(con, key, 0, "Get-ChildItem -Force | Select-Object Name", 1, 0)
    con.commit()
    rows = procedures.mine_rituals(con, min_sessions=2)
    assert len(rows) == 2, rows
    assert {r["cwd"] for r in rows} == {"f:\\a", "f:\\b"}
    st.close()


def test_mine_rituals_drops_placeholder_stub(tmp_path):
    """纯内联表达式壳（`python -c <q>`）不是可执行步骤，必须被过滤。"""
    st = _mk_store(tmp_path)
    con = st.con
    for i in range(3):
        key = "dsh:h%d" % i
        _insert_session(con, key)
        _insert_call(con, key, 0, 'python -c "import x"', 1, 0)
    con.commit()
    assert procedures.mine_rituals(con, min_sessions=3) == []
    st.close()


# ── 第 3 步：前提判定 ────────────────────────────────────────────────
# ⚠️ 夹具不能用 pytest 的 tmp_path：它在 %TEMP% 下，被 premises._NON_ASSET_RE
# 正确判成"非项目资产"→ unverified（那是**正确行为**，不是 bug）。
# 这些用例要测的是项目资产路径的三路判定，所以用仓库自身的真实文件/真实空目录。
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REAL_FILE = os.path.join(_REPO, "cli.py")


def test_judge_alive_when_path_exists():
    """命令里引用的路径还在 -> alive。"""
    v = procedures.judge({"sample": 'python "%s"' % _REAL_FILE, "cwd": _REPO}, {}, {})
    assert v["verdict"] == "alive", v


def test_judge_returns_certification_quadruple():
    """认证四件套：结论之外必须给出检验时刻、依据、失效条件。

    依据 Many Steps into One（arXiv 2610.17544）：成功/失败计数是糟糕的变更检测器，
    安全做法是"对照活事实认证、事实一变就重启证书"——所以证书必须能回答
    「何时检验」「凭什么」「何时作废」，缺一项就成了无依据断言。
    """
    v = procedures.judge({"sample": 'python "%s"' % _REAL_FILE, "cwd": _REPO}, {}, {})
    assert v["certified_at"], v                      # ISO 时间
    assert len(v["certified_at"]) >= 16
    assert "存在" in v["basis"], v                   # 依据摘要
    assert "作废" in v["recheck"], v                 # 失效条件


def test_judge_basis_counts_each_premise():
    """依据要按逐条前提计数（"2 条存在；1 条已不存在"），不能只给一个结论。"""
    gone = os.path.join(_REPO, "does-not-exist", "x.py")
    v = procedures.judge(
        {"sample": 'python "%s"; python "%s"' % (_REAL_FILE, gone), "cwd": _REPO}, {}, {})
    assert "存在" in v["basis"] and "已不存在" in v["basis"], v


def test_render_block_shows_certification_per_item(tmp_path, monkeypatch):
    """注入块里每条都要带认证标记（agent 唯一会读的地方，依据必须进块）。"""
    import premises
    st = _block_store(tmp_path)
    monkeypatch.setattr(premises, "live_dirs_by_entity",
                        lambda db: {"ent-a": ["f:\\proj"]})
    monkeypatch.setattr(premises, "dir_entity_map", lambda db: {"f:\\proj": "ent-a"})
    blk = procedures.select_for_entity(str(tmp_path / "b.db"), "ent-a", max_items=2)
    text = procedures.render_block(blk)
    # 每条都要有"何时校验"的日期标记（alive 用"✓…认证"，迁移用"⚠…校"）
    assert "11-17" in text or "认证" in text or "校" in text, text
    assert text.count("- 常用：") == len(blk["items"]), text
    st.close()


def test_judge_dead_when_path_gone_and_no_successor():
    """路径不存在、项目也没有活目录 -> dead（照做会失败，必须劝退）。"""
    gone = os.path.join(_REPO, "does-not-exist", "run.py")
    v = procedures.judge({"sample": 'python "%s"' % gone, "cwd": ""}, {}, {})
    assert v["verdict"] == "dead_no_successor", v


def test_judge_unverified_without_paths():
    """纯 shell 内建命令抽不到路径 -> unverified（不臆判失效）。"""
    v = procedures.judge({"sample": "cd <p>; Write-Output hi", "cwd": ""}, {}, {})
    assert v["verdict"] == "unverified", v


def test_judge_dead_wins_over_alive():
    """一条步骤里有任一 dead 前提就判 dead：部分失效的步骤照做同样会失败。"""
    gone = os.path.join(_REPO, "does-not-exist", "x.py")
    item = {"sample": 'python "%s"; python "%s"' % (_REAL_FILE, gone), "cwd": ""}
    v = procedures.judge(item, {}, {})
    assert v["verdict"] == "dead_no_successor", v


# ── 注入块（第三步收口：procedure 要真的被 agent 看到）──────────────────
def test_normalize_keeps_redirect_syntax():
    """`2>&1` 是重定向语法不是参数值：不能被数字归一化抹成 `N>&N`。"""
    shape = procedures.normalize_command("python tools/x.py 2>&1 | Select-Object -First 20")
    assert "2>&1" in shape, shape
    assert "n>&n" not in shape


def _block_store(tmp_path, cwd="f:\\proj"):
    """造一个只有仪式 + 修复的库（cwd 固定），供注入块用例。"""
    st = Store(str(tmp_path / "b.db"))
    con = st.con
    for i in range(3):
        key = "dsh:b%d" % i
        con.execute(
            "INSERT INTO sessions (session_key,agent,session_id,cwd,indexed_at) "
            "VALUES (?,?,?,?,?)", (key, "dsh", key.split(":")[1], cwd, "2026-11-17T00:00:00Z"))
        con.execute(
            "INSERT INTO tool_calls (session_key,seq,ts,tool,command,ok,exit_code) "
            "VALUES (?,?,?,?,?,?,?)",
            (key, 0, "2026-11-17T00:00:00Z", "pwsh",
             "Get-ChildItem -Force | Select-Object Name", 1, 0))
        # 第二条不同形态的仪式：用于测"条数上限"截断
        con.execute(
            "INSERT INTO tool_calls (session_key,seq,ts,tool,command,ok,exit_code) "
            "VALUES (?,?,?,?,?,?,?)",
            (key, 1, "2026-11-17T00:00:00Z", "pwsh",
             "python tools/compile_gate.py 2>&1", 1, 0))
    con.execute(
        "INSERT INTO tool_calls (session_key,seq,ts,tool,command,ok,exit_code) "
        "VALUES (?,?,?,?,?,?,?)",
        ("dsh:b0", 2, "2026-11-17T00:00:00Z", "pwsh",
         "python tools/x.py 2>&1 | Select-Object -First 20", 0, 1))
    con.execute(
        "INSERT INTO tool_calls (session_key,seq,ts,tool,command,ok,exit_code) "
        "VALUES (?,?,?,?,?,?,?)",
        ("dsh:b0", 3, "2026-11-17T00:00:00Z", "pwsh",
         "python tools/x.py 2>&1 | Select-Object -Last 5", 1, 0))
    con.commit()
    return st


def test_select_for_entity_scopes_by_cwd(tmp_path, monkeypatch):
    """procedure 按项目作用域挑：别的目录的仪式不能混进来。"""
    import premises
    st = _block_store(tmp_path)
    monkeypatch.setattr(premises, "live_dirs_by_entity",
                        lambda db: {"ent-a": ["f:\\proj"]})
    monkeypatch.setattr(premises, "dir_entity_map", lambda db: {"f:\\proj": "ent-a"})
    blk = procedures.select_for_entity(str(tmp_path / "b.db"), "ent-a", max_items=3)
    assert blk["items"], blk
    assert all((it.get("cwd") or "") == "f:\\proj" for it in blk["items"])

    blk2 = procedures.select_for_entity(str(tmp_path / "b.db"), "ent-other", max_items=3)
    assert blk2["items"] == []
    st.close()


def test_render_block_states_truncation(tmp_path, monkeypatch):
    """截断必须明说条数（静默丢条目正是这套系统一直在防的事）。"""
    import premises
    st = _block_store(tmp_path)
    monkeypatch.setattr(premises, "live_dirs_by_entity",
                        lambda db: {"ent-a": ["f:\\proj"]})
    monkeypatch.setattr(premises, "dir_entity_map", lambda db: {"f:\\proj": "ent-a"})
    blk = procedures.select_for_entity(str(tmp_path / "b.db"), "ent-a", max_items=1)
    text = procedures.render_block(blk)
    assert "未列出" in text and "条数上限" in text, text
    st.close()


def test_render_block_skips_dead_premise(tmp_path, monkeypatch):
    """前提已失效的 procedure 不进注入块（照做会失败），只报数量。"""
    import premises
    # 用一个"ASCII、不在系统/临时目录、且确实不存在"的项目路径：
    # 用 tmp_path 不行 —— 它在 %TEMP% 下会被正确判成"非项目资产"（→ unverified），
    # 测不到 dead 分支。
    gone_dir = "f:\\definitely-not-exist-xyz"
    st = _block_store(tmp_path, cwd=gone_dir)
    monkeypatch.setattr(premises, "live_dirs_by_entity", lambda db: {"ent-a": []})
    monkeypatch.setattr(premises, "dir_entity_map", lambda db: {gone_dir: "ent-a"})
    blk = procedures.select_for_entity(str(tmp_path / "b.db"), "ent-a", max_items=3)
    text = procedures.render_block(blk)
    assert blk["dead_skipped"] >= 1, blk
    assert "前提已失效未注入" in text, text
    st.close()
