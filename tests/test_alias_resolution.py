# -*- coding: utf-8 -*-
"""别名解析契约（2026-11-06）：**数据没丢、查询侧不认旧名**这一类问题不许再出现。

实测背景（本机 1,281 会话的库）：
    timeline '示例知识库' '架构与选型'  → 64 个会话 / 21 条结论
    timeline 'demo-kb'   '架构与选型'  →  0 个会话 /  0 条结论   ← 修前
    search   'demo-kb'  （limit 2）    → 只 2 条逐字命中        ← 修前

本文件钉四件事：
  ① `aliases` 的解析语义（canonical / 全部写法 / 自由文本展开）；
  ② 别名表缺失或损坏时**静默退化**，检索不许变砖；
  ③ `build_timeline` 用别名查询与用 canonical 查询**结果一致**；
  ④ `store.search` 在主查询命中不足时按别名补检，并如实回报 `last_alias_note`。
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aliases  # noqa: E402
import paths  # noqa: E402
from collectors.base import CollectResult, Record, SessionMeta, session_key  # noqa: E402
from store import Store, ingest  # noqa: E402
from timeline import build_timeline, render  # noqa: E402

ENTITIES = {"entities": {
    "示例知识库": ["demo-wiki", "demo-kb", "kbnx-demo"],
    "示例社区": ["明珠"],
}}


@pytest.fixture()
def entity_file(tmp_path, monkeypatch):
    p = tmp_path / "entities.json"
    p.write_text(json.dumps(ENTITIES, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(paths, "ENTITIES_FILE", str(p))
    aliases.reset()
    yield str(p)
    aliases.reset()


# ── ① 解析语义 ────────────────────────────────────────────────────────────
def test_canonical_and_variants(entity_file):
    assert aliases.canonical_of("demo-kb") == "示例知识库"
    assert aliases.canonical_of("示例知识库") == "示例知识库"
    vs = aliases.variants("demo-kb")
    assert "示例知识库" in vs and "demo-wiki" in vs and "kbnx-demo" in vs
    # 未知名字原样返回 ⇒ 调用方 SQL 形状不用变
    assert aliases.variants("完全没听过的词") == ["完全没听过的词"]


def test_describe_only_when_resolved(entity_file):
    assert "→ 示例知识库" in aliases.describe("demo-kb")
    assert aliases.describe("示例知识库") == ""      # 本来就是 canonical，不必提示
    assert aliases.describe("没听过的") == ""


def test_free_text_expansion_skips_short_aliases(entity_file):
    """自由文本只认 ≥3 字的写法：2 字别名（「明珠」）在长查询里太容易误撞。"""
    hit = aliases.expansion("demo-kb 的检索链路怎么定的")
    assert hit and hit[0][0] == "示例知识库"
    assert aliases.expansion("明珠那个项目的预算") == []
    # 短别名仍可用于**显式**实体参数
    assert aliases.canonical_of("明珠") == "示例社区"


# ── ② 缺失 / 损坏 ⇒ 静默退化 ──────────────────────────────────────────────
def test_missing_file_degrades_silently(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ENTITIES_FILE", str(tmp_path / "nope.json"))
    aliases.reset()
    assert aliases.canonical_of("demo-kb") is None
    assert aliases.variants("demo-kb") == ["demo-kb"]
    assert aliases.expansion("demo-kb 架构") == []


def test_broken_file_degrades_silently(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(paths, "ENTITIES_FILE", str(p))
    aliases.reset()
    assert aliases.variants("demo-kb") == ["demo-kb"]


# ── ③ timeline：别名查询 == canonical 查询 ────────────────────────────────
def _mk(store, sid, texts, entity, intent):
    sk = session_key("dsh", sid)
    meta = SessionMeta(agent="dsh", session_id=sid, title="会话 " + sid,
                       cwd="F:\\proj", started_at="2026-11-01T00:00:00Z")
    recs = [Record(session_key=sk, role=r, text=t,
                   ts="2026-11-01T00:00:0%dZ" % i, seq=i)
            for i, (r, t) in enumerate(texts)]
    meta.message_count = len(recs)
    ingest(store, [CollectResult(meta=meta, records=recs)])
    store.set_topics(sk, [entity], [intent])
    return sk


def test_timeline_resolves_alias_to_same_sessions(tmp_path, entity_file):
    db = str(tmp_path / "t.db")
    st = Store(db)
    _mk(st, "s1", [("assistant", "结论：改用 PageIndex 树导航，不再用 chunk 检索。")],
        "示例知识库", "架构与选型")
    _mk(st, "s2", [("assistant", "决定：默认候选数 60 改为 15。")],
        "示例知识库", "架构与选型")
    _mk(st, "s3", [("assistant", "结论：明珠的预算 978861。")], "示例社区", "架构与选型")
    st.close()

    canon = build_timeline(db, "示例知识库", "架构与选型")
    alias = build_timeline(db, "demo-kb", "架构与选型")
    assert canon["sessions"] == alias["sessions"] == 2
    assert alias["alias_applied"] is True
    assert alias["entity"] == "示例知识库" and alias["entity_input"] == "demo-kb"
    # 未知实体不会被别名表"猜"到别的话题上
    unknown = build_timeline(db, "没听过的实体", "架构与选型")
    assert unknown["sessions"] == 0 and unknown["alias_applied"] is False


def test_timeline_render_reports_alias(entity_file):
    tl = {"entity": "示例知识库", "entity_input": "demo-kb", "entity_labels": ["a", "b"],
          "alias_applied": True, "intent": "", "sessions": 0, "with_conclusions": 0,
          "entries": [], "conflicts": []}
    out = render(tl)
    assert "别名解析" in out and "demo-kb" in out and "示例知识库" in out
    # 没有别名时不许凭空多一行
    plain = dict(tl, alias_applied=False)
    assert "别名解析" not in render(plain)


# ── ④ search：主查询不足时按别名补检 ──────────────────────────────────────
def test_search_tops_up_with_alias_variants(tmp_path, entity_file):
    db = str(tmp_path / "t.db")
    st = Store(db)
    _mk(st, "s1", [("assistant", "demo-wiki 的检索链路是树索引加精排。")],
        "示例知识库", "检索与排序")
    rows_direct = st.search("demo-kb", limit=5)
    assert st.last_alias_note, "命中不足时必须按别名补检并回报"
    assert any("demo-wiki" in (r["snippet"] or "") for r in rows_direct)
    st.close()


def test_search_does_not_top_up_when_query_already_full(tmp_path, entity_file):
    db = str(tmp_path / "t.db")
    st = Store(db)
    _mk(st, "s1", [("assistant", "demo-kb 出现三次：demo-kb 与 demo-kb。")],
        "示例知识库", "检索与排序")
    st.search("demo-kb", limit=1)
    assert st.last_alias_note == "", "主查询已打满 limit 时不该再补检（避免静默改结果集）"
    st.close()


def test_search_survives_broken_alias_table(tmp_path, monkeypatch):
    """别名层坏掉时检索必须照常工作（这是"静默退化"的另一半）。"""
    p = tmp_path / "bad.json"
    p.write_text("{ nope", encoding="utf-8")
    monkeypatch.setattr(paths, "ENTITIES_FILE", str(p))
    aliases.reset()
    db = str(tmp_path / "t.db")
    st = Store(db)
    _mk(st, "s1", [("assistant", "demo-kb 的检索链路")], "示例知识库", "检索与排序")
    rows = st.search("demo-kb", limit=5)
    assert rows and st.last_alias_note == ""
    st.close()
