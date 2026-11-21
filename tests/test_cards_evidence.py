# -*- coding: utf-8 -*-
"""证据核验契约（2026-11-10）：quote 必须能回到原文。

为什么必须钉：`validate()` 只保证"quote 非空、不太长"，**从不检查它是否真出自原文**。
而"可溯源"是这套卡片的核心承诺 —— 没有这一步，卡片可以被随便编一段话就录进来。
借鉴 DSH 市场里 `dsh-memento`「auditable」/`dsh-engram`「evidence」的共识：
记忆的可信度来自证据可回查，不来自算法。

这里用**注入的 fetch_text**（dict 模拟会话库），不碰真库。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards as C  # noqa: E402


def _fetcher(table):
    return lambda mid: table.get(int(mid))


def _card(quote, msg=None):
    ev = {"agent": "dsh", "date": "2026-11-10", "quote": quote}
    if msg is not None:
        ev["msg"] = msg
    return C.build("测试断言", [ev])


ORIG = "打包闸门必须机器检，不靠手写白名单。实测两天内漏了两次。"


def test_quote_in_original_is_verified():
    r = C.verify_evidence(_fetcher({1: ORIG}), _card("打包闸门必须机器检", msg=1))
    assert r["verified"] == [1] and not r["mismatch"]
    assert r["verifiable_ratio"] == 1.0


def test_mutated_quote_is_mismatch():
    """改一个字就该被抓到 —— 这是整个功能的立身之本。"""
    r = C.verify_evidence(_fetcher({1: ORIG}), _card("打包闸门必须人工检", msg=1))
    assert r["mismatch"], r
    assert r["verifiable_ratio"] == 0.0


def test_whitespace_and_case_insensitive():
    """原文的换行/缩进 vs 摘录里一行：应视为同一句（不让格式差异造成假失败）。"""
    orig = "第一行\n    第二行 content"
    r = C.verify_evidence(_fetcher({7: orig}), _card("第一行 第二行 CONTENT", msg=7))
    assert r["verified"] == [1], r


def test_elision_allows_skipping_but_each_segment_must_match():
    """允许用「…」跳过原文，但每一段都得找到；编出来的一段必须被抓。"""
    ok = C.verify_evidence(_fetcher({3: ORIG}), _card("打包闸门必须机器检…漏了两次", msg=3))
    assert ok["verified"] == [1], ok
    bad = C.verify_evidence(_fetcher({3: ORIG}),
                            _card("打包闸门必须机器检…其实一次都没漏", msg=3))
    assert bad["mismatch"], bad


def test_no_msg_anchor_counts_as_unverifiable_not_failure():
    """跨机卡片没有锚点 ⇒ 如实计入 no_anchor，**不许**当成失败（也不进分母）。"""
    r = C.verify_evidence(_fetcher({}), _card("随便什么话"))
    assert r["no_anchor"] == [1]
    assert not r["mismatch"] and r["verifiable_ratio"] == 0.0


def test_missing_message_is_reported_separately():
    """有锚点但库里没这条消息（跨机/库被重建）⇒ 单独计数，不混进"不符"。"""
    r = C.verify_evidence(_fetcher({}), _card("随便什么话", msg=999))
    assert r["missing_msg"] == [1] and not r["mismatch"]


def test_too_short_quote_is_not_evidence():
    """全是省略号或过短的摘录无法构成证据 ⇒ 归入 no_anchor（防"用两个字蒙过去"）。"""
    r = C.verify_evidence(_fetcher({1: ORIG}), _card("……", msg=1))
    assert r["no_anchor"] == [1]
    r2 = C.verify_evidence(_fetcher({1: ORIG}), _card("打包", msg=1))
    assert r2["no_anchor"] == [1], "3 字以下不构成证据"


def test_verify_cards_aggregates_and_lists_bad(tmp_path):
    p = str(tmp_path / "cards.jsonl")
    good = C.build("好卡", [{"agent": "dsh", "date": "2026-11-10",
                             "quote": "打包闸门必须机器检", "msg": 1}])
    bad = C.build("坏卡", [{"agent": "dsh", "date": "2026-11-10",
                            "quote": "这句话原文里没有", "msg": 1}])
    C.upsert(good, p)
    C.upsert(bad, p)
    agg = C.verify_cards(_fetcher({1: ORIG}), p)
    assert agg["cards"] == 2 and agg["verified"] == 1 and agg["mismatch"] == 1
    assert agg["verifiable_ratio"] == 0.5
    assert [b["claim"] for b in agg["bad_cards"]] == ["坏卡"]


def test_verify_never_raises_on_garbage(tmp_path):
    """坏数据不许把校验本身搞崩（这是校验器的底线）。"""
    p = str(tmp_path / "cards.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"claim":"没有 evidence","status":"alive"}\n')
        fh.write('{"claim":"evidence 不是列表","status":"alive","evidence":"x"}\n')
    agg = C.verify_cards(_fetcher({}), p)
    assert agg["cards"] == 2 and agg["evidence"] == 0
