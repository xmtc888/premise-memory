# -*- coding: utf-8 -*-
"""写入审计日志契约（2026-11-10，改进清单第 5 条）。

为什么必须钉：卡片现在有**两处会自动改状态**（巡检 apply）和**多个 agent** 写卡，
没有审计就追不到"谁把它变成 stale 的"。
四条硬规矩：
  1. **fail-soft**：审计写失败绝不许影响主操作（写卡才是业务）；
  2. 记**改前/改后**两个哈希（只记一个等于没记）；
  3. 坏行不许让读取崩（NDJSON 是给人看/给机器读的混合体）；
  4. 文件要有界（append-only 也要滚动，否则永久增长）。
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards as C  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """卡片库 + 审计文件都放临时目录。"""
    lib = str(tmp_path / "cards.jsonl")
    aud = str(tmp_path / "cards-audit.ndjson")
    monkeypatch.setattr(C, "audit_file", lambda: aud)
    monkeypatch.setenv("AGENT_NAME", "test-agent")
    return lib, aud


def _ev(q="摘录"):
    return [{"agent": "dsh", "date": "2026-11-10", "quote": q}]


# ── 基本记录 ────────────────────────────────────────────────────────────
def test_add_writes_audit_with_hash_and_by(env):
    lib, aud = env
    c = C.build("断言甲", _ev(), topic="t")
    C.upsert(c, lib)
    recs = C.read_audit(path=aud)
    assert len(recs) == 1
    r = recs[0]
    assert r["op"] == "add" and r["card_id"] == c["id"] and r["by"] == "test-agent"
    assert r["after"] and not r["before"], "新增没有改前内容"


def test_update_records_before_and_after_differ(env):
    """只记一个哈希等于没记：必须能看出"从什么改成了什么"。"""
    lib, aud = env
    c = C.build("断言乙", _ev("原文一"), topic="t")
    C.upsert(c, lib)
    C.upsert(dict(c, evidence=_ev("原文二")), lib)
    recs = C.read_audit(path=aud)
    upd = [r for r in recs if r["op"] == "update"]
    assert len(upd) == 1
    assert upd[0]["before"] and upd[0]["after"]
    assert upd[0]["before"] != upd[0]["after"], "改前改后哈希相同说明快照拿错了"


def test_supersede_records_both_cards(env):
    lib, aud = env
    old = C.build("旧结论", _ev(), topic="t")
    new = C.build("新结论", _ev(), topic="t")
    C.upsert(old, lib)
    C.upsert(new, lib)
    C.supersede(old["id"], new["id"], lib)
    recs = C.read_audit(path=aud)
    ops = [r["op"] for r in recs]
    assert "supersede" in ops and "superseded_by" in ops
    sup = [r for r in recs if r["op"] == "supersede"][0]
    assert sup["before"] and sup["after"] and sup["before"] != sup["after"]
    assert "被 %s 推翻" % new["id"] in sup["note"]


def test_audit_apply_records_stale_change(env):
    """巡检自动置 stale 必须留审计 —— 否则没人知道是谁改的。"""
    lib, aud = env
    C.upsert(C.build("有问题的结论 GHOST", _ev(), topic="t"), lib)

    def analyze(card):
        if "GHOST" in card["claim"]:
            return [{"path": "F:/ghost/x", "verdict": "dead_no_successor", "note": ""}]
        return []

    C.audit_cards(analyze, lib, apply=True)
    recs = C.read_audit(path=aud)
    hit = [r for r in recs if r["op"] == "audit_stale"]
    assert len(hit) == 1 and "前提已失效" in hit[0]["note"]


# ── 查询过滤 ────────────────────────────────────────────────────────────
def test_read_filters_by_card_id_and_newest_first(env):
    lib, aud = env
    a = C.build("断言A", _ev(), topic="t")
    b = C.build("断言B", _ev(), topic="t")
    C.upsert(a, lib)
    C.upsert(b, lib)
    only_a = C.read_audit(card_id=a["id"], path=aud)
    assert [r["card_id"] for r in only_a] == [a["id"]]
    allrecs = C.read_audit(path=aud)
    assert len(allrecs) == 2
    assert allrecs[0]["card_id"] == b["id"], "应为新 → 旧"


def test_read_missing_file_returns_empty(tmp_path):
    assert C.read_audit(path=str(tmp_path / "nope.ndjson")) == []


def test_broken_lines_are_skipped(env):
    lib, aud = env
    C.upsert(C.build("断言C", _ev(), topic="t"), lib)
    with io.open(aud, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("{ 这不是 json\n")
        fh.write("[1,2,3]\n")
    recs = C.read_audit(path=aud)
    assert len(recs) == 1, recs


# ── fail-soft 与滚动 ────────────────────────────────────────────────────
def test_audit_failure_does_not_break_writes(env, monkeypatch, tmp_path):
    """审计目录不可写时，写卡必须照常成功（业务 > 副业）。"""
    lib, _aud = env
    bad = str(tmp_path / "no-such-dir" / "x" / "audit.ndjson")
    monkeypatch.setattr(C, "audit_file", lambda: bad)
    # 让目录创建失败：把父级设成一个文件
    os.makedirs(os.path.join(tmp_path, "no-such-dir"), exist_ok=True)
    with io.open(os.path.join(tmp_path, "no-such-dir", "x"), "w", encoding="utf-8") as fh:
        fh.write("blocker")
    state, msg = C.upsert(C.build("断言D", _ev(), topic="t"), lib)
    assert state == "added", "审计失败不该影响写卡"
    assert len(C.load(lib)[0]) == 1


def test_trim_keeps_last_lines(env, monkeypatch):
    """append-only 也要有界：超限时只留最后 N 行。"""
    lib, aud = env
    monkeypatch.setattr(C, "_AUDIT_MAX_LINES", 3)
    monkeypatch.setattr(C, "_AUDIT_MAX_BYTES", 1)      # 强制每次触发滚动
    for i in range(6):
        C.append_audit("noop", "c-%d" % i, note="x" * 20, path=aud)
    recs = C.read_audit(path=aud)
    assert len(recs) == 3, len(recs)
    assert recs[0]["card_id"] == "c-5", "保留的应该是最后几条"
