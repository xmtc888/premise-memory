# -*- coding: utf-8 -*-
"""人类诊断（diagnoses.py）的契约测试。

## 为什么这些断言重要（WASP-Mem, arXiv 2611.04003）

论文实测：**人定的规则化组织会拖低性能**，而**一轮人类诊断胜过四轮自判 7.0~10.9%**。
所以诊断层有三条不可退让的性质：
  ① **id 稳定**：诊断是"人对某条候选说的话"，候选重挖不能让它对不上；
  ② **原话不改写**：诊断存的是人写的原文（改写＝又用规则替代人）；
  ③ **落盘原子**：写一半断电不能留坏 JSON（诊断丢了比没写更糟）。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diagnoses                                               # noqa: E402


ITEM = {
    "bad": "cd <p> .\\venv\\scripts\\python <p> 2>&1 | select-object -first <n>",
    "good": "cd <p> .\\venv\\scripts\\python <p> 2>&1 | select-object -last <n>",
    "sample_bad": "cd F:\\demo-wiki; .\\venv\\Scripts\\python.exe x.py 2>&1 | Select-Object -First 16",
    "sample_good": "cd F:\\demo-wiki; .\\venv\\Scripts\\python.exe x.py 2>&1 | Select-Object -Last 6",
    "cwd": "f:\\demo-wiki",
}


def test_stable_id_ignores_counts_and_order(tmp_path):
    """同一个形态对永远同一个 id（与出现次数、排序无关）。"""
    a = diagnoses.stable_id(ITEM["bad"], ITEM["good"])
    b = diagnoses.stable_id(ITEM["bad"], ITEM["good"])
    assert a == b
    assert a.startswith("rp_") and len(a) == 15
    # 形态不同就不同 id
    assert a != diagnoses.stable_id(ITEM["good"], ITEM["bad"])


def test_record_keeps_human_wording_verbatim(tmp_path):
    """诊断原话必须**逐字**存下来（不改写、不摘要）。"""
    path = str(tmp_path / "d.json")
    text = "PowerShell 的 Select-Object -First 在管道提前关闭时抛错；改 -Last 更稳。"
    rec = diagnoses.record(ITEM, text, path=path)
    assert rec["diagnosis"] == text
    assert diagnoses.get(rec["id"], path=path)["diagnosis"] == text


def test_record_rejects_empty_diagnosis(tmp_path):
    """空诊断要报错：诊断就该是人写的原话，空白等于没诊断。"""
    with pytest.raises(ValueError):
        diagnoses.record(ITEM, "   ", path=str(tmp_path / "d.json"))


def test_load_survives_broken_file(tmp_path):
    """坏 JSON 不能阻塞主流程（诊断是增量数据，坏了当空处理并允许重写）。"""
    p = tmp_path / "d.json"
    p.write_text("{not json", encoding="utf-8")
    doc = diagnoses.load(str(p))
    assert doc["items"] == {}
    rec = diagnoses.record(ITEM, "诊断", path=str(p))
    assert diagnoses.get(rec["id"], path=str(p))


def test_save_is_atomic_no_tmp_left(tmp_path):
    """写盘用临时文件 + replace：成功后不能留下 .tmp。"""
    p = str(tmp_path / "d.json")
    diagnoses.record(ITEM, "诊断", path=p)
    assert os.path.exists(p)
    assert not os.path.exists(p + ".tmp")
    json.loads(open(p, encoding="utf-8").read())      # 必须是合法 JSON


def test_mine_repairs_attaches_stable_id(tmp_path):
    """挖掘出的候选必须自带 id（否则人没法引用它做诊断）。"""
    from store import Store
    import procedures

    st = Store(str(tmp_path / "s.db"))
    con = st.con
    con.execute(
        "INSERT INTO sessions (session_key,agent,session_id,cwd,indexed_at) "
        "VALUES ('dsh:s1','dsh','s1','F:\\\\proj','2026-11-17T00:00:00Z')")
    con.execute(
        "INSERT INTO tool_calls (session_key,seq,ts,tool,command,ok,exit_code) "
        "VALUES ('dsh:s1',0,'2026-11-17T00:00:00Z','pwsh',"
        "'python x.py 2>&1 | Select-Object -First 20',0,1)")
    con.execute(
        "INSERT INTO tool_calls (session_key,seq,ts,tool,command,ok,exit_code) "
        "VALUES ('dsh:s1',1,'2026-11-17T00:00:00Z','pwsh',"
        "'python x.py 2>&1 | Select-Object -Last 5',1,0)")
    con.commit()
    rows = procedures.mine_repairs(con)
    st.close()
    assert rows and rows[0]["id"].startswith("rp_"), rows
    # 同一形态对再挖一次，id 必须一致（诊断靠它对得上）
    st2 = Store(str(tmp_path / "s2.db"))
    con2 = st2.con
    con2.execute(
        "INSERT INTO sessions (session_key,agent,session_id,cwd,indexed_at) "
        "VALUES ('dsh:s9','dsh','s9','F:\\\\proj','2026-11-17T00:00:00Z')")
    con2.execute(
        "INSERT INTO tool_calls (session_key,seq,ts,tool,command,ok,exit_code) "
        "VALUES ('dsh:s9',0,'2026-11-17T00:00:00Z','pwsh',"
        "'python x.py 2>&1 | Select-Object -First 20',0,1)")
    con2.execute(
        "INSERT INTO tool_calls (session_key,seq,ts,tool,command,ok,exit_code) "
        "VALUES ('dsh:s9',1,'2026-11-17T00:00:00Z','pwsh',"
        "'python x.py 2>&1 | Select-Object -Last 5',1,0)")
    con2.commit()
    rows2 = procedures.mine_repairs(con2)
    st2.close()
    assert rows2[0]["id"] == rows[0]["id"], (rows[0]["id"], rows2[0]["id"])
