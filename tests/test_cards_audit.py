# -*- coding: utf-8 -*-
"""前提巡检契约（2026-11-10，改进清单第 4 条）。

为什么必须钉：这是**唯一会自动改卡片状态**的功能，改错了就是在没有人类确认的情况下
把结论"降级"。所以四条安全边界逐条钉住：
  1. 默认只报告（apply=False 不写盘）；
  2. 命中失效前提只置 `stale`（**不置 dead、不删除** —— 路径没了 ≠ 结论错了）；
  3. 改状态必须留 `status_history`（可回滚）；
  4. dead 卡不参与巡检（已被推翻的东西不需要再巡检）。
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards as C  # noqa: E402


def _ev(quote="摘录"):
    return [{"agent": "dsh", "date": "2026-11-10", "quote": quote}]


def _analyze_dead(card):
    """假判据：claim 里带 GHOST 就报"真失效"。"""
    if "GHOST" in (card.get("claim") or ""):
        return [{"path": "F:/demo-ghost/x.py", "verdict": "dead_no_successor", "note": ""}]
    return []


def _analyze_alive(card):
    return [{"path": "F:/ok/x.py", "verdict": "alive", "note": ""}]


@pytest.fixture()
def lib(tmp_path):
    p = str(tmp_path / "cards.jsonl")
    C.upsert(C.build("有问题的结论 GHOST", _ev(), status="alive"), p)
    C.upsert(C.build("正常的结论", _ev(), status="alive"), p)
    C.upsert(C.build("已被推翻的 GHOST", _ev(), status="dead"), p)
    return p


# ── 默认只报告 ──────────────────────────────────────────────────────────
def test_dry_run_reports_but_does_not_write(lib):
    rep = C.audit_cards(_analyze_dead, lib)
    assert len(rep["candidates"]) == 1
    assert rep["candidates"][0]["from"] == "alive"
    assert rep["applied"] == 0
    rows, _ = C.load(lib)
    assert all(c.get("status") != "stale" for c in rows), "dry-run 不许改状态"


# ── 写回：stale + 留痕 ──────────────────────────────────────────────────
def test_apply_sets_stale_with_history(lib):
    rep = C.audit_cards(_analyze_dead, lib, apply=True)
    assert rep["applied"] == 1
    rows, _ = C.load(lib)
    hit = [c for c in rows if "GHOST" in c["claim"] and c["status"] == "stale"][0]
    assert "前提已失效" in hit["stale_reason"]
    hist = hit["status_history"]
    assert len(hist) == 1 and hist[0]["from"] == "alive" and hist[0]["to"] == "stale"
    assert hist[0]["at"]


def test_apply_never_marks_dead_or_deletes(lib):
    """路径没了 ≠ 结论错了：只降 stale，不判死、不删除。"""
    C.audit_cards(_analyze_dead, lib, apply=True)
    rows, _ = C.load(lib)
    assert len(rows) == 3, "不许删卡"
    assert [c["claim"] for c in rows if c["status"] == "dead"] == ["已被推翻的 GHOST"], \
        "原本 dead 的卡保持 dead，不参与巡检"


def test_dead_cards_not_scanned(lib):
    rep = C.audit_cards(_analyze_dead, lib, apply=True)
    assert rep["scanned"] == 2, "3 张里 dead 那张不该被扫描"


# ── 无失效前提时不改 ────────────────────────────────────────────────────
def test_alive_findings_do_not_change_status(lib):
    rep = C.audit_cards(_analyze_alive, lib, apply=True)
    assert rep["candidates"] == [] and rep["applied"] == 0
    rows, _ = C.load(lib)
    assert sum(1 for c in rows if c["status"] == "alive") == 2


def test_no_paths_counted_separately(lib):
    rep = C.audit_cards(lambda card: [], lib)
    assert rep["no_paths"] == 2, "没有可验证前提的卡单独计数（不是失败）"


# ── 幂等 ────────────────────────────────────────────────────────────────
def test_second_apply_does_not_duplicate_history(lib):
    C.audit_cards(_analyze_dead, lib, apply=True)
    C.audit_cards(_analyze_dead, lib, apply=True)      # 已 stale，默认不扫 stale
    rows, _ = C.load(lib)
    hit = [c for c in rows if c["status"] == "stale"][0]
    assert len(hit["status_history"]) == 1, "第二次不该再追加一条历史"


def test_audit_handles_broken_library(tmp_path):
    p = str(tmp_path / "bad.jsonl")
    io.open(p, "w", encoding="utf-8", newline="\n").write("{ broken\n")
    rep = C.audit_cards(_analyze_dead, p)
    assert rep["cards"] == 0 and rep["warnings"], rep
