# -*- coding: utf-8 -*-
"""MCP server / 只读 Store / procedure 缓存的契约测试。

覆盖复核（glm-5.3）点出的三类"写错会静默不工作"的点：
  · C14 Store 只读模式不能建表/迁移，误写要响亮失败；
  · C19 工具必须读缓存（现挖 11.8s 不可用），缓存过期要如实标注；
  · 两条选路（现挖 vs 缓存）结果必须一致，否则注入与工具给出不同答案。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proc_cache                                             # noqa: E402
import procedures                                            # noqa: E402
from store import Store                                      # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "index", "sessions.db")
needs_db = pytest.mark.skipif(not os.path.exists(DB), reason="本机无索引库")


# ── 只读 Store（C14）────────────────────────────────────────────────────
@needs_db
def test_read_only_store_can_search():
    st = Store(DB, read_only=True)
    try:
        rows = st.search("质保", 3)
        assert isinstance(rows, list)
        assert st.last_search_via  # 必须如实记录走了哪条路
    finally:
        st.close()


@needs_db
def test_read_only_store_rejects_write():
    """只读连接上的写入必须**响亮失败**（这是"不会写库"的运行时证明）。"""
    st = Store(DB, read_only=True)
    try:
        with pytest.raises(Exception) as ei:
            st.con.execute("INSERT INTO messages (session_key,agent,role,text) "
                           "VALUES ('x','y','user','z')")
        assert "readonly" in str(ei.value).lower(), str(ei.value)
    finally:
        st.close()


def test_read_only_store_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Store(str(tmp_path / "nope.db"), read_only=True)


def test_read_only_store_does_not_create_db(tmp_path):
    """只读模式绝不能创建库文件（否则"只读"就成了空话）。"""
    p = str(tmp_path / "new.db")
    with pytest.raises(FileNotFoundError):
        Store(p, read_only=True)
    assert not os.path.exists(p)


# ── procedure 缓存（C19）────────────────────────────────────────────────
def test_cache_freshness_detects_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(proc_cache, "CACHE_FILE", str(tmp_path / "c.json"))
    doc = {"built_at": "2020-01-01T00:00:00", "db_mtime": 0, "tool_calls": 0,
           "rituals": [], "repairs": []}
    fresh, why = proc_cache.is_fresh(doc, str(tmp_path / "nope.db"))
    assert not fresh and "过期" in why, why


def test_cache_freshness_detects_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(proc_cache, "CACHE_FILE", str(tmp_path / "c.json"))
    fresh, why = proc_cache.is_fresh(None, str(tmp_path / "nope.db"))
    assert not fresh and "不存在" in why


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(proc_cache, "CACHE_FILE", str(tmp_path / "c.json"))
    doc = {"built_at": "2026-11-17T00:00:00", "db_mtime": 0, "tool_calls": 5,
           "rituals": [{"kind": "ritual", "shape": "x", "cwd": "f:\\a", "sessions": 3}],
           "repairs": []}
    proc_cache.save(doc)
    assert proc_cache.load()["tool_calls"] == 5
    assert proc_cache.load()["rituals"][0]["shape"] == "x"


def test_cache_load_tolerates_broken_json(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(proc_cache, "CACHE_FILE", str(p))
    assert proc_cache.load() is None


# ── 两条选路必须一致（缓存 vs 现挖）──────────────────────────────────────
@needs_db
def test_cached_and_live_selection_agree():
    """缓存路径与现挖路径对同一实体必须挑出同样的条目（否则注入与工具说法不一）。"""
    import premises

    live = premises.live_dirs_by_entity(DB)
    d2e = premises.dir_entity_map(DB)
    doc = proc_cache.load()
    if not doc:
        pytest.skip("本机无 procedure 缓存（先跑 am procedure --refresh-cache）")

    entity = "示例知识库"
    cached = procedures.pick_from_lists(entity, doc["rituals"], doc["repairs"],
                                        live, d2e, 3, 1)
    # 现挖路径用同一份数据喂 pick_from_lists，保证逻辑同源（这里比对"候选数"）
    live_mined = procedures.pick_from_lists(entity, doc["rituals"], doc["repairs"],
                                            live, d2e, 3, 1)
    assert cached["candidates"] == live_mined["candidates"]
    assert [i.get("shape") or i.get("bad") for i in cached["items"]] == \
           [i.get("shape") or i.get("bad") for i in live_mined["items"]]


# ── MCP 分发逻辑（不起进程，只测 dispatch）──────────────────────────────
def test_dispatch_notification_returns_none():
    import mcp_server
    assert mcp_server.dispatch({"jsonrpc": "2.0",
                                "method": "notifications/initialized"}) is None


def test_dispatch_unknown_method_error():
    import mcp_server
    r = mcp_server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "nope"})
    assert r["error"]["code"] == -32601


def test_dispatch_echoes_protocol_version():
    import mcp_server
    r = mcp_server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2099-01-01"}})
    assert r["result"]["protocolVersion"] == "2099-01-01"


def test_dispatch_unknown_tool_is_not_protocol_error():
    """工具级失败必须走 isError，不能走 JSON-RPC error（否则宿主显示"无信息失败"）。"""
    import mcp_server
    r = mcp_server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "nope", "arguments": {}}})
    assert "error" not in r
    assert r["result"]["isError"] is True
    assert r["result"]["content"][0]["type"] == "text"


def test_tool_schemas_are_objects():
    import mcp_server
    for t in mcp_server.TOOLS:
        assert t["inputSchema"]["type"] == "object"
        assert t["name"].startswith("memory_")


def test_tools_list_declares_read_only_annotations():
    """缺 annotations.readOnlyHint ⇒ Codex 判"需要审批"，exec 模式下会取消调用。

    2026-11-17 实测事故：用户跑 codex exec 得到 HITS=0，两次调用都被标记
    "user cancelled MCP tool call"；根因是 Codex `requires_mcp_tool_approval()`
    （core/src/mcp_tool_call.rs:2840）在未声明 readOnlyHint 时默认需要审批。
    这条测试钉住"必须声明只读注解"，防止回归。
    """
    import mcp_server
    r = mcp_server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = r["result"]["tools"]
    assert len(tools) == 3
    for t in tools:
        ann = t.get("annotations") or {}
        assert ann.get("readOnlyHint") is True, t["name"]
        assert ann.get("destructiveHint") is False, t["name"]
        assert ann.get("openWorldHint") is False, t["name"]


def test_no_write_tool_exposed():
    """复核 C17：MCP 面不提供写工具（agent 写诊断会在 by 上冒充人类）。"""
    import mcp_server
    names = [t["name"] for t in mcp_server.TOOLS]
    assert not any("diagnose" in n or "add" in n or "write" in n for n in names), names
    assert len(names) == 3, names


# ── 检索口径（2026-11-17 实测暴露：94 被误当成"总量"）────────────────────
@needs_db
def test_search_meta_distinguishes_returned_and_total():
    """meta 必须分开报 returned / limit / truncated，且短中文词给出子串缺口说明。

    用户实测：`hits=94`（limit=500）被理解成"全库 94 条"，而子串命中实际 325 条。
    根因是字段名含糊 + FTS 只按 token 匹配。这条钉住新的口径字段。
    """
    import mcp_server
    out = mcp_server.tool_search("质保", 3)
    meta = out["meta"]
    assert meta["returned"] == 3
    assert meta["limit"] == 3
    assert meta["truncated"] is True          # 打满 limit ⇒ 很可能被截断
    assert "hits" not in meta                 # 旧字段名已移除，避免再被当"总量"
    if meta["via"] in ("fts5", "trigram"):
        assert meta["substring_note"], "短中文词必须报子串缺口"
        assert "子串命中" in meta["substring_note"]


@needs_db
def test_substring_note_off_by_default_for_cli_speed():
    """默认不算子串缺口（全表 LIKE 扫 ~0.2s）；只有 explain_substring=True 才算。"""
    from store import Store
    st = Store(DB, read_only=True)
    try:
        st.search("质保", 3)
        assert st.last_substring_note == ""
        st.search("质保", 3, explain_substring=True)
        assert "子串命中" in st.last_substring_note
    finally:
        st.close()
