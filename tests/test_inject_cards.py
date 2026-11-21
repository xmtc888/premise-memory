# -*- coding: utf-8 -*-
"""注入预算与优先序契约（2026-11-10，改进清单第 3 条）。

为什么必须钉：注入是**每轮成本**，无界注入会把上下文挤爆、噪声盖掉信号
（市场里做注入的插件 `dsh-memoir`「bounded Hot Memory」/`dsh-memento`「Bounded」都把它写进定位）。
而"静默丢条目"正是这套系统一直在防的事 —— 所以**截断必须报，且要说清是哪条预算卡住的**。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards as C  # noqa: E402
import inject as I  # noqa: E402


def _ev(n=1, quote="摘录"):
    return [{"agent": "dsh", "date": "2026-11-10", "quote": "%s%d" % (quote, i)}
            for i in range(n)]


@pytest.fixture()
def cardlib(tmp_path, monkeypatch):
    """造一个小卡片库，并让 inject 默认读它。"""
    p = str(tmp_path / "cards.jsonl")

    def add(claim, status="alive", topic="t", n_ev=1, scopes="machine", updated="2026-11-10T00:00:00"):
        c = C.build(claim, _ev(n_ev), status=status, topic=topic, scopes=scopes)
        c["updated_at"] = updated
        C.upsert(c, p)
        return c

    add("成立的结论", "alive", n_ev=2, updated="2026-11-01T00:00:00")
    add("未验证的结论", "unverified", n_ev=1, updated="2026-11-05T00:00:00")
    add("前提已变的结论", "stale", n_ev=1, updated="2026-11-06T00:00:00")
    add("被推翻的结论", "dead", n_ev=1, updated="2026-11-07T00:00:00")
    add("项目专属结论", "alive", scopes="project:f:/demo", n_ev=1, updated="2026-11-08T00:00:00")
    return p


def _block(path, **kw):
    kw.setdefault("cards_path", path)
    return I.build_cards_block(**kw)


# ── 优先序 ──────────────────────────────────────────────────────────────
def test_alive_comes_before_unverified_and_stale(cardlib):
    b = _block(cardlib, max_cards=10)
    lines = b["text"].split("\n")
    assert lines[0].startswith("- [成立]")
    assert any("未验证" in ln for ln in lines)
    assert any("前提已变" in ln for ln in lines)
    # 未验证必须排在 前提已变 之前
    i_unv = next(i for i, ln in enumerate(lines) if "未验证" in ln)
    i_stale = next(i for i, ln in enumerate(lines) if "前提已变" in ln)
    assert i_unv < i_stale


def test_dead_excluded_by_default_and_included_on_demand(cardlib):
    b = _block(cardlib, max_cards=10)
    assert "已被推翻" not in b["text"]
    b2 = _block(cardlib, max_cards=10, include_dead=True)
    assert "- [已被推翻] 被推翻的结论" in b2["text"]


def test_more_evidence_first_within_same_status(cardlib):
    """同 status 下证据条数多的先上（可核验性高的优先）。"""
    b = _block(cardlib, max_cards=1)
    assert "成立的结论" in b["text"], b["text"]


# ── 预算 ────────────────────────────────────────────────────────────────
def test_card_budget_truncates_and_reports(cardlib):
    b = _block(cardlib, max_cards=2)
    assert b["picked"] == 2 and b["truncated"] >= 1
    assert b["bound_by"] == "cards"
    txt = I.render_cards_block(b)
    assert "另有 **" in txt and "条数上限 2 条" in txt and "--max-cards" in txt


def test_char_budget_truncates_and_reports(cardlib):
    b = _block(cardlib, max_cards=10, max_chars=10)
    assert b["picked"] == 0 and b["bound_by"] == "chars"
    txt = I.render_cards_block(b)
    assert "字符上限 10 字符" in txt and "--card-chars" in txt


def test_no_truncation_when_everything_fits(cardlib):
    b = _block(cardlib, max_cards=99, max_chars=99999)
    assert b["truncated"] == 0 and b["bound_by"] == ""
    assert "未列出" not in I.render_cards_block(b)


# ── 过滤 ────────────────────────────────────────────────────────────────
def test_topic_filter(cardlib):
    b = _block(cardlib, topic="不存在的话题")
    assert b["picked"] == 0 and b["candidates"] == 0
    txt = I.render_cards_block(b)
    assert "没有符合条件的卡片" in txt


def test_scope_filter_uses_prefix_matching(cardlib):
    b = _block(cardlib, scope="project:f:/demo/sub", max_cards=10)
    assert "项目专属结论" in b["text"], b["text"]
    assert "成立的结论" not in b["text"], "machine 卡不该被 project 过滤命中"


def test_empty_library_does_not_crash(tmp_path):
    b = _block(str(tmp_path / "none.jsonl"))
    assert b["picked"] == 0 and b["candidates"] == 0
    assert "没有符合条件的卡片" in I.render_cards_block(b)
