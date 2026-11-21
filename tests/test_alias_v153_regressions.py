# -*- coding: utf-8 -*-
"""v1.5.3 的 3 条回归测试：钉住 GLM 复核提出的两条可达缺陷 + 重复别名检测。"""
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


def _entities(tmp_path, monkeypatch, doc):
    p = tmp_path / "entities.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(paths, "ENTITIES_FILE", str(p))
    aliases.reset()
    return str(p)


def _seed(store, text, entity):
    sk = session_key("dsh", "s1")
    meta = SessionMeta(agent="dsh", session_id="s1", title="t", cwd="F:\\proj",
                       started_at="2026-11-01T00:00:00Z")
    recs = [Record(session_key=sk, role="assistant", text=text,
                   ts="2026-11-01T00:00:01Z", seq=0)]
    meta.message_count = 1
    ingest(store, [CollectResult(meta=meta, records=recs)])
    store.set_topics(sk, [entity], ["检索与排序"])
    return sk


def test_alias_topup_does_not_dirty_main_query_flags(tmp_path, monkeypatch):
    """GLM 发现 1：别名写法含 `-` 时会触发宽松重试，把「查询含语法字符」的提示写给用户。

    复现条件：主查询是纯中文（不含 `- : .`），命中的别名写法含 `-`。
    修法：`search()` 在补检前后快照/恢复 `last_query_relaxed` 与 `last_search_via`。
    """
    _entities(tmp_path, monkeypatch, {"entities": {"示例知识库": ["demo-wiki"]}})
    st = Store(str(tmp_path / "t.db"))
    _seed(st, "demo-wiki 的检索链路是树索引加精排。", "示例知识库")

    rows = st.search("示例知识库", limit=5)          # 主查询纯中文，无 FTS5 语法字符
    assert rows and st.last_alias_note, "补检应当发生（否则这条用例没测到目标路径）"
    assert st.last_query_relaxed is False, "补检内部的重试不许算到用户查询头上"
    assert st.last_search_via == "fts5", "补检走的 LIKE 回退不许改写主查询的走法"
    st.close()


def test_last_alias_error_resets_between_searches(tmp_path, monkeypatch):
    """GLM 发现 1 附带：上一次的补检错误不许串进下一次查询的输出。"""
    _entities(tmp_path, monkeypatch, {"entities": {"示例知识库": ["demo-wiki"]}})
    st = Store(str(tmp_path / "t.db"))
    _seed(st, "demo-wiki 的检索链路", "示例知识库")
    st.last_alias_error = "上一轮的假错误"
    st.search("完全不相关的一次查询", limit=5)
    assert st.last_alias_error == ""
    st.close()


def test_duplicates_reports_alias_bound_to_two_entities(tmp_path, monkeypatch):
    """GLM 发现 3：一条别名挂两处时反查先到先得 —— 至少要能被 `duplicates()` 看见。"""
    _entities(tmp_path, monkeypatch, {"entities": {
        "甲项目": ["x-shared", "甲"],
        "乙项目": ["x-shared", "乙"],
    }})
    dups = aliases.duplicates()
    assert "x-shared" in dups
    assert set(dups["x-shared"]) == {"甲项目", "乙项目"}
    # 反查行为本身保持"先到先得"（不改语义，只加可观测性）
    assert aliases.canonical_of("x-shared") in ("甲项目", "乙项目")
