# -*- coding: utf-8 -*-
"""导出置顶 dead 段与去重键契约（2026-11-10，改进清单第 7 条）。

借鉴 `dsh-gokifu-compact` 的「failure memory that survives compaction / transposition detection」：
迁移/重排之后，"哪条被推翻过"必须**还在且显眼**；同时要能识别"同一断言被重复登记"。

三条硬规矩：
  1. **被推翻的卡置顶**（JSONL 与 Markdown 都是）——它比成立的结论更值钱；
  2. `dedupe_key` 用**规范化 claim** 而不是 id：id 含 topic，同一断言挂两个 topic 就是两个 id，
     而那正是要检测的重复；
  3. 重复**只报告不合并**（自动合并会丢证据）。
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards as C  # noqa: E402


def _ev(q="摘录"):
    return [{"agent": "dsh", "date": "2026-11-10", "quote": q}]


@pytest.fixture()
def lib(tmp_path):
    p = str(tmp_path / "cards.jsonl")
    a = C.build("仍成立的结论", _ev(), status="alive", topic="甲")
    d = C.build("被推翻的结论", _ev(), status="dead", topic="乙")
    s = C.build("前提已变的结论", _ev(), status="stale", topic="丙")
    u = C.build("未验证的结论", _ev(), status="unverified", topic="丁")
    for c in (a, d, s, u):
        C.upsert(c, p)
    return p


# ── JSONL：dead 置顶 + dedupe_key ───────────────────────────────────────
def test_jsonl_export_puts_dead_first(lib, tmp_path):
    dst = str(tmp_path / "out.jsonl")
    n, _ = C.export_to(dst, lib)
    rows = [json.loads(x) for x in io.open(dst, encoding="utf-8") if x.strip()]
    assert n == 4 and len(rows) == 4
    assert rows[0]["status"] == "dead", "被推翻的卡必须在最前"
    assert rows[1]["status"] == "stale"
    assert [r["status"] for r in rows[2:]] == ["alive", "unverified"]


def test_jsonl_export_adds_dedupe_key_without_mutating_library(lib, tmp_path):
    dst = str(tmp_path / "out.jsonl")
    C.export_to(dst, lib)
    rows = [json.loads(x) for x in io.open(dst, encoding="utf-8") if x.strip()]
    assert all(r.get("dedupe_key") for r in rows)
    # 库里不许被加上这个字段（导出是只读操作）
    assert all("dedupe_key" not in c for c in C.load(lib)[0])


def test_dedupe_key_ignores_topic_and_punctuation(lib):
    """同一句断言换个 topic / 加个标点，dedupe_key 必须相同（id 会不同）。"""
    a = C.build("打包闸门要机器检", _ev(), topic="甲")
    b = C.build("打包闸门要机器检。", _ev(), topic="乙")
    assert a["id"] != b["id"], "id 含 topic，本来就该不同"
    assert C.dedupe_key(a) == C.dedupe_key(b)


# ── 重复检测 ────────────────────────────────────────────────────────────
def test_find_duplicate_claims_groups_same_assertion(tmp_path):
    p = str(tmp_path / "c.jsonl")
    C.upsert(C.build("同一句断言", _ev(), topic="甲"), p)
    C.upsert(C.build("同一句断言", _ev(), topic="乙"), p)
    C.upsert(C.build("另一句断言", _ev(), topic="甲"), p)
    dups = C.find_duplicate_claims(C.load(p)[0])
    assert len(dups) == 1
    assert len(dups[0]["cards"]) == 2


def test_import_reports_duplicate_groups(lib, tmp_path):
    src = str(tmp_path / "in.jsonl")
    a = C.build("打包闸门要机器检", _ev(), topic="新topic")
    b = C.build("打包闸门要机器检", _ev(), topic="另一个topic")
    with io.open(src, "w", encoding="utf-8", newline="\n") as fh:
        for c in (a, b):
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    stats, warns = C.import_from(src, path=lib)
    assert stats["added"] == 2, "两个 id 不同，都会被加进去（不自动合并）"
    assert stats["duplicate_groups"] == 1
    assert any("同一断言" in w for w in warns), warns


# ── Markdown：三段式 ────────────────────────────────────────────────────
def test_md_export_sections_in_order(lib, tmp_path):
    dst = str(tmp_path / "out.md")
    C.export_md(dst, lib)
    md = io.open(dst, encoding="utf-8").read()
    i_dead = md.index("## 一、已被推翻")
    i_alive = md.index("## 二、仍成立")
    i_other = md.index("## 三、前提已变")
    assert i_dead < i_alive < i_other, "被推翻必须排最前"
    assert "被推翻的结论" in md[i_dead:i_alive]
    assert "仍成立的结论" in md[i_alive:i_other]
    assert "前提已变的结论" in md[i_other:]


def test_md_export_mentions_duplicates(lib, tmp_path):
    p = lib
    C.upsert(C.build("重复的断言", _ev(), topic="X"), p)
    C.upsert(C.build("重复的断言", _ev(), topic="Y"), p)
    dst = str(tmp_path / "out.md")
    C.export_md(dst, p)
    md = io.open(dst, encoding="utf-8").read()
    assert "重复登记" in md


def test_md_export_marks_superseded_by(lib, tmp_path):
    dst = str(tmp_path / "out.md")
    C.export_md(dst, lib)
    md = io.open(dst, encoding="utf-8").read()
    assert "已被推翻" in md
