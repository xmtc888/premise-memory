# -*- coding: utf-8 -*-
"""矛盾检测契约（2026-11-10，改进清单第 6 条）。

为什么必须钉：这是**唯一会指出"你两张卡在打架"**的功能，误报会让人失去信任、漏报等于没有。
所以两条验收标准写死：
  ① 造一对"否定翻转"的矛盾卡，**必须抓到**；
  ② 现有真实卡（同 topic 但极性相同/内容不同）**误报必须为 0**。
另外：已建立 supersedes 关系的配对**不重复报**（那是已知矛盾）；只报不改。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards as C  # noqa: E402


def _ev(q="摘录"):
    return [{"agent": "dsh", "date": "2026-11-10", "quote": q}]


@pytest.fixture()
def lib(tmp_path):
    return str(tmp_path / "cards.jsonl")


def _add(lib, claim, topic="t", status="alive"):
    c = C.build(claim, _ev(), status=status, topic=topic)
    C.upsert(c, lib)
    return c


# ── ① 必须抓到 ──────────────────────────────────────────────────────────
def test_detects_negation_flip(lib):
    a = _add(lib, "打包闸门要机器检")
    b = _add(lib, "打包闸门不要机器检")
    pairs = C.detect_conflicts(lib)
    assert len(pairs) == 1, pairs
    p = pairs[0]
    assert {p["a"]["id"], p["b"]["id"]} == {a["id"], b["id"]}
    assert p["similarity"] >= 0.9
    assert (p["a"]["neg"] != p["b"]["neg"]), "必须标出哪条含否定"


def test_detects_impossible_vs_possible(lib):
    _add(lib, "向量检索不可用")
    _add(lib, "向量检索可用")
    assert len(C.detect_conflicts(lib)) == 1


# ── ② 不许误报 ──────────────────────────────────────────────────────────
def test_same_polarity_similar_claims_are_not_conflicts(lib):
    """同极性 + 高相似 = 重复，不是矛盾（重复的问题交给 supersede/人工，不在此报）。"""
    _add(lib, "卡片要带证据")
    _add(lib, "卡片要带证据")
    assert C.detect_conflicts(lib) == []


def test_different_facts_are_not_conflicts(lib):
    """`机器检` vs `人工检` 是不同主张，不是否定翻转 —— 相似度低，不该报。"""
    _add(lib, "打包闸门要机器检")
    _add(lib, "打包闸门要人工检")
    assert C.detect_conflicts(lib) == []


def test_different_topics_not_compared(lib):
    _add(lib, "打包闸门要机器检", topic="A")
    _add(lib, "打包闸门不要机器检", topic="B")
    assert C.detect_conflicts(lib) == []


# ── 已知矛盾不重复报 ────────────────────────────────────────────────────
def test_superseded_pair_not_reported(lib):
    a = _add(lib, "打包闸门要机器检")
    b = _add(lib, "打包闸门不要机器检")
    C.supersede(a["id"], b["id"], lib)
    assert C.detect_conflicts(lib) == [], "已经用 supersede 裁决过的配对，不该再报"


def test_dead_cards_not_compared(lib):
    _add(lib, "打包闸门要机器检", status="dead")
    _add(lib, "打包闸门不要机器检")
    assert C.detect_conflicts(lib) == [], "dead 是历史，不参与矛盾检测"


# ── 相似度函数本身 ──────────────────────────────────────────────────────
def test_similarity_strips_negation_and_modals_symmetrically():
    """只剥否定会造成不对称（要X vs 不要X 相似度 0.62）；正负情态要成对剥。"""
    assert C.claim_similarity("打包闸门要机器检", "打包闸门不要机器检") > 0.95
    assert C.claim_similarity("完全不同的两句话在这里", "打包闸门要机器检") < 0.3


def test_has_negation_marks_polarity():
    assert C.has_negation("不要机器检") is True
    assert C.has_negation("禁止全盘扫描") is True
    assert C.has_negation("必须机器检") is False


def test_real_library_has_no_false_positive():
    """真实卡片库（若存在）必须 0 误报 —— 这条是验收标准本身。"""
    path = C.cards_file()
    if not path or not os.path.exists(path):
        pytest.skip("本机没有真实卡片库")
    pairs = C.detect_conflicts(path)
    assert pairs == [], "真实卡出现误报说明判据太松：%r" % [
        (p["a"]["claim"][:20], p["b"]["claim"][:20], p["similarity"]) for p in pairs]
