# -*- coding: utf-8 -*-
"""人工确认账本契约（2026-11-19）。

为什么必须钉：注入是**每轮成本**，同一张卡天天推同一句话就是噪音。但"少推"绝不能
变成"静默丢"——跳过数必须报（`acked_skipped`），且任何内容变化都要自动恢复推送，
否则一次确认就把结论永久埋掉。

三条失效路径各一条测试：**内容指纹变 / 超 TTL / 撤销**。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards as C  # noqa: E402
import inject as I  # noqa: E402


def _ev(n=1):
    return [{"agent": "dsh", "date": "2026-11-19", "quote": "摘录%d" % i} for i in range(n)]


@pytest.fixture()
def lib(tmp_path):
    """造一个卡片库 + 一个**同目录**账本；全程不碰真实 index/。"""
    p = str(tmp_path / "cards.jsonl")
    a = str(tmp_path / "cards-ack.jsonl")

    def add(claim, status="alive", topic="t", n_ev=1):
        c = C.build(claim, _ev(n_ev), status=status, topic=topic, scopes="machine")
        C.upsert(c, p)
        return c

    return p, a, add


def _block(p, a, **kw):
    kw.setdefault("cards_path", p)
    kw.setdefault("ack_path", a)
    kw.setdefault("max_cards", 10)
    return I.build_cards_block(**kw)


# ── 基本行为 ────────────────────────────────────────────────────────────
def test_ack_skips_that_card_and_reports_count(lib):
    p, a, add = lib
    add("成立的结论")
    add("另一条结论")
    target = C.search("成立的结论", path=p)[0]
    ok, _msg = C.ack(target["id"], cards_path=p, ack_path=a)
    assert ok
    b = _block(p, a)
    assert "成立的结论" not in b["text"]
    assert "另一条结论" in b["text"]
    assert b["acked_skipped"] == 1, "跳过数必须报，不许静默少推"
    txt = I.render_cards_block(b)
    assert "已确认" in txt and "--include-acked" in txt


def test_skip_acked_false_ignores_ledger(lib):
    p, a, add = lib
    add("成立的结论")
    target = C.search("成立的结论", path=p)[0]
    C.ack(target["id"], cards_path=p, ack_path=a)
    b = _block(p, a, skip_acked=False)
    assert "成立的结论" in b["text"]
    assert b["acked_skipped"] == 0


def test_ack_does_not_hide_card_from_search_or_list(lib):
    """账本只管注入；`search`/`list` 是"查全部"，不能被确认影响。"""
    p, a, add = lib
    add("成立的结论")
    target = C.search("成立的结论", path=p)[0]
    C.ack(target["id"], cards_path=p, ack_path=a)
    assert C.search("成立的结论", path=p), "确认不该让卡在检索里消失"


# ── 三条失效路径 ────────────────────────────────────────────────────────
def test_content_change_brings_card_back(lib):
    """status 被巡检改成 stale ⇒ 指纹变 ⇒ 自动重现（这是最该重现的时机）。"""
    p, a, add = lib
    add("成立的结论")
    target = C.search("成立的结论", path=p)[0]
    C.ack(target["id"], cards_path=p, ack_path=a)
    assert "成立的结论" not in _block(p, a)["text"]

    rows, _ = C.load(p)
    for r in rows:
        if r["id"] == target["id"]:
            r["status"] = "stale"
    C.save(rows, p)

    b = _block(p, a)
    assert "成立的结论" in b["text"], "内容变了必须重新推"
    assert b["acked_skipped"] == 0


def test_ttl_expiry_brings_card_back(lib):
    p, a, add = lib
    add("成立的结论")
    target = C.search("成立的结论", path=p)[0]
    # 手工写一条 2020 年的确认：内容指纹一致，但早已过期
    C.save_acks({target["id"]: {"id": target["id"], "sig": C.card_sig(target),
                                "ack_at": "2020-01-01T00:00:00", "by": "human"}}, a, p)
    b = _block(p, a, ack_ttl_days=30)
    assert "成立的结论" in b["text"], "过期必须重新推"
    assert b["acked_skipped"] == 0


def test_unack_brings_card_back(lib):
    p, a, add = lib
    add("成立的结论")
    target = C.search("成立的结论", path=p)[0]
    C.ack(target["id"], cards_path=p, ack_path=a)
    ok, _msg = C.unack(target["id"], p, a)
    assert ok
    assert "成立的结论" in _block(p, a)["text"]


# ── 账本自身健壮性 ──────────────────────────────────────────────────────
def test_ack_unknown_card_fails_cleanly(lib):
    p, a, _add = lib
    ok, msg = C.ack("c-deadbeef", cards_path=p, ack_path=a)
    assert not ok and "找不到" in msg


def test_ack_is_idempotent(lib):
    p, a, add = lib
    add("成立的结论")
    target = C.search("成立的结论", path=p)[0]
    C.ack(target["id"], cards_path=p, ack_path=a)
    C.ack(target["id"], cards_path=p, ack_path=a)
    reg, _w = C.load_acks(a, p)
    assert len(reg) == 1, "重复确认不该堆行"


def test_broken_ledger_does_not_break_injection(lib):
    """账本坏了按「没确认」处理：宁可多推，也不许注入整块消失。"""
    p, a, add = lib
    add("成立的结论")
    with open(a, "w", encoding="utf-8") as f:
        f.write("这不是 json\n")
    reg, warns = C.load_acks(a, p)
    assert reg == {} and warns
    assert "成立的结论" in _block(p, a)["text"]


def test_ledger_defaults_next_to_cards_file(lib, tmp_path):
    """默认账本路径 = 卡片文件同目录：测试/多库天然隔离，不污染真实 index/。"""
    p, _a, _add = lib
    assert C._ack_path(p) == os.path.join(str(tmp_path), "cards-ack.jsonl")


def test_ack_states_are_reported(lib):
    p, a, add = lib
    add("成立的结论")
    target = C.search("成立的结论", path=p)[0]
    C.ack(target["id"], cards_path=p, ack_path=a)
    rows = C.acks(cards_path=p, ack_path=a)
    assert len(rows) == 1 and rows[0]["state"] == "valid"
    # 卡片被删掉后，状态必须变成 missing，而不是继续"免打扰"
    C.save([r for r in C.load(p)[0] if r["id"] != target["id"]], p)
    assert C.acks(cards_path=p, ack_path=a)[0]["state"] == "missing"
    removed, remain = C.prune_acks(cards_path=p, ack_path=a)
    assert removed == 1 and remain == 0
