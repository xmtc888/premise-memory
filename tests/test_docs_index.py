# -*- coding: utf-8 -*-
"""docs 层（编译产物优先）契约：配置、匹配、回落、BOM 容错。

为什么值得钉：
  ① 这一层的整个价值是"**先查编译过的东西**"。它一旦静默失效（比如配置读不出），
     就退化成普通会话检索，而用户还以为自己在用升级式检索 —— 必须有断言保证"要么生效、要么明说关闭"；
  ② Windows 上 PowerShell/记事本写出的 JSON 常带 BOM，`json.load(utf-8)` 会抛
     "Unexpected UTF-8 BOM" ⇒ 整层被吞掉。这个坑 2026-11-08 真踩过；
  ③ 查询按空白切词做 AND，中文不分词（单字忽略）——`切块`/`向量` 这种 2 字词在 FTS 上命不中，
     这一层正是为它们准备的。
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docs_index as D  # noqa: E402
import paths  # noqa: E402


@pytest.fixture()
def docs_tree(tmp_path):
    """造两篇 md：一篇命中、一篇不命中，外加一个子目录。"""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text(
        "# 标题甲\n\n## 第二节 向量与切块\n\n本项目多次强调不要向量切块的 RAG，结论见下。\n",
        encoding="utf-8")
    (root / "b.md").write_text(
        "# 标题乙\n\n## 别的章节\n\n这里只谈 OCR 与扫描件。\n", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "c.md").write_text(
        "## 深层\n\n向量检索的严格口径否证：top-12 只命中 1/4。\n", encoding="utf-8")
    return str(root)


def _write_config(tmp_path, monkeypatch, roots, bom=False):
    p = tmp_path / "docs-roots.json"
    payload = json.dumps({"roots": roots}, ensure_ascii=False)
    if bom:
        p.write_bytes(("\ufeff" + payload).encode("utf-8"))
    else:
        p.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(paths, "DOCS_ROOTS_FILE", str(p))
    monkeypatch.delenv("DOCS_MCP_ROOTS", raising=False)
    return str(p)


# ── 配置 ────────────────────────────────────────────────────────────────
def test_no_config_means_layer_off(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DOCS_ROOTS_FILE", str(tmp_path / "nope.json"))
    monkeypatch.delenv("DOCS_MCP_ROOTS", raising=False)
    dirs, source = D.roots()
    assert dirs == [] and source == "none"
    assert D.search("向量")[0] == []          # 关闭时返回空，不是异常


def test_config_is_read_and_bom_tolerated(tmp_path, monkeypatch, docs_tree):
    _write_config(tmp_path, monkeypatch, [docs_tree], bom=True)   # ← BOM 必须能读
    dirs, source = D.roots()
    assert source == "config" and dirs == [docs_tree]


def test_env_fallback(tmp_path, monkeypatch, docs_tree):
    monkeypatch.setattr(paths, "DOCS_ROOTS_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setenv("DOCS_MCP_ROOTS", "%s,%s" % (docs_tree, tmp_path / "missing"))
    dirs, source = D.roots()
    assert source == "env"
    assert dirs == [docs_tree], "不存在的目录要滤掉"


def test_broken_config_degrades_to_off(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(paths, "DOCS_ROOTS_FILE", str(p))
    monkeypatch.delenv("DOCS_MCP_ROOTS", raising=False)
    assert D.roots() == ([], "none")


# ── 匹配 ────────────────────────────────────────────────────────────────
def test_search_returns_file_heading_line_snippet(docs_tree):
    # 用两个词元（而不是连写「向量切块」）：标题是「向量与切块」，连写不会命中标题
    hits, source = D.search("向量 切块", dirs=[docs_tree])
    assert source == "explicit"
    assert hits, "应当命中 a.md"
    h = hits[0]
    assert h["file"].endswith("a.md")
    assert "向量与切块" in h["heading"]
    assert h["line"] >= 1
    assert h["heading_hit"] is True


def test_contiguous_query_still_matches_body(docs_tree):
    """连写查询命中不了标题（标题写成「向量与切块」），但能命中正文子串 —— 这是预期行为。"""
    hits, _ = D.search("向量切块", dirs=[docs_tree])
    assert hits and hits[0]["file"].endswith("a.md")
    assert "不要向量切块的 RAG" in hits[0]["snippet"]


def test_search_requires_all_terms(docs_tree):
    """AND：只给一个词命中多篇；给两个不相邻的词应当缩小到 0。"""
    assert D.search("OCR", dirs=[docs_tree])[0], "OCR 在 b.md"
    assert D.search("OCR 向量切块", dirs=[docs_tree])[0] == [], "两个词不同篇 ⇒ 无命中"


# ── 匹配粒度：段内可跨行，跨段不算（2026-11-09 实测改）──
def test_terms_in_same_paragraph_but_different_lines_hit(tmp_path):
    """同一段里分两句说了两件事 ⇒ 必须命中。

    原来要求「某一行同时含全部词」，实测 25 个真实多词查询里 74% 的文件被整篇丢弃
    （如「docs 根」丢 76%）。改成段落级后捡回 +73%。
    """
    root = tmp_path / "para"
    root.mkdir()
    (root / "p.md").write_text(
        "## 配置\n\n"
        "docs 层的根写在本机数据文件里。\n"
        "预算是另一回事，可以单独设。\n",
        encoding="utf-8")
    hits, _src, _meta = D.search_ex("docs 预算", dirs=[str(root)])
    assert hits, "同一段内跨行提到两个词，应当命中"
    assert hits[0]["file"].endswith("p.md")


def test_terms_in_different_paragraphs_do_not_hit(tmp_path):
    """落在**不同段**里 ⇒ 不命中。段落是精度边界：整篇都提到不算同一件事。"""
    root = tmp_path / "sep"
    root.mkdir()
    (root / "s.md").write_text(
        "## 一\n\ndocs 层的根在这里说。\n\n## 二\n\n预算是另一节的事。\n",
        encoding="utf-8")
    hits, _src, _meta = D.search_ex("docs 预算", dirs=[str(root)])
    assert hits == [], "跨段不算命中（否则 precision 会崩）"


def test_paragraph_match_reports_line_and_heading(tmp_path):
    root = tmp_path / "loc"
    root.mkdir()
    (root / "l.md").write_text(
        "# 文档甲\n\n## 第二节 配置\n\n前缀说明若干。\n讲到别名解析这一句。\n",
        encoding="utf-8")
    hits, _src, _meta = D.search_ex("别名 解析", dirs=[str(root)])
    assert hits
    h = hits[0]
    assert h["heading"].endswith("配置"), h["heading"]
    assert h["line"] >= 1


def test_search_substring_for_chinese(docs_tree):
    """中文不分词：2 字词按子串命中（这正是 FTS trigram 的盲区）。"""
    hits, _ = D.search("切块", dirs=[docs_tree])
    assert hits and hits[0]["file"].endswith("a.md")


def test_search_recurses_subdirs(docs_tree):
    hits, _ = D.search("严格口径", dirs=[docs_tree])
    assert hits and hits[0]["file"].endswith("c.md")


def test_heading_hit_ranks_first(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (root / "body.md").write_text("## 无关标题\n\n这里正文提到 PageIndex 一次。\n",
                                  encoding="utf-8")
    (root / "head.md").write_text("## PageIndex 架构\n\n正文也提到 PageIndex。\n",
                                  encoding="utf-8")
    hits, _ = D.search("PageIndex", dirs=[str(root)])
    assert hits[0]["file"].endswith("head.md"), "标题命中的要排前面"


def test_single_char_terms_ignored(docs_tree):
    assert D.search("向", dirs=[docs_tree])[0] == []


def test_limit_respected(docs_tree):
    hits, _ = D.search("向量", limit=1, dirs=[docs_tree])
    assert len(hits) <= 1


# ── 召回预算 / 超时（借鉴 TencentDB Agent Memory：maxTotalRecallChars + timeoutMs）──
def test_max_chars_truncates_total_budget(docs_tree):
    """总字符预算：取到预算为止，不许一次性灌满上下文。"""
    hits, _src, meta = D.search_ex("向量", limit=5, dirs=[docs_tree], max_chars=40)
    total = sum(len(h["snippet"]) for h in hits)
    assert 0 < total <= 40, (total, hits)
    assert meta["timed_out"] is False


def test_max_chars_zero_is_unlimited(tmp_path):
    """0 = 不限：长命中行必须完整返回（对照 40 字预算会被截断）。"""
    root = tmp_path / "long"
    root.mkdir()
    body = "向量" + "很长的正文" * 30          # 约 150 字，远超 40
    (root / "a.md").write_text("## 标题\n\n%s\n" % body, encoding="utf-8")
    full, _s1, _m1 = D.search_ex("向量", limit=5, dirs=[str(root)], max_chars=0)
    cut, _s2, _m2 = D.search_ex("向量", limit=5, dirs=[str(root)], max_chars=40)
    assert full and len(full[0]["snippet"]) > 100, len(full[0]["snippet"])
    assert sum(len(h["snippet"]) for h in cut) <= 40


def test_timeout_returns_partial_and_flags(tmp_path, monkeypatch):
    """超时不是错误：返回已找到的部分 + `timed_out=True`，由调用方如实提示。

    ⚠️ 不能靠 `timeout_ms=1` 制造超时（2026-11-17 实测 flaky）：30 个小文件在快机器上
    1ms 内就扫完了，断言随机失败。改为**打桩时钟**：第 1 次取 monotonic 用于算 deadline，
    之后立刻返回"已过 10 秒"，超时必然发生且可复现。
    """
    import docs_index as DI

    root = tmp_path / "slow"
    root.mkdir()
    for i in range(30):
        (root / ("f%02d.md" % i)).write_text("## 标题\n\n向量召回第 %d 段。\n" % i,
                                             encoding="utf-8")
    base = 1000.0
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        return base if calls["n"] == 1 else base + 10.0

    monkeypatch.setattr(DI.time, "monotonic", fake_monotonic)
    hits, _src, meta = DI.search_ex("向量", limit=50, dirs=[str(root)], timeout_ms=1)
    assert meta["timed_out"] is True
    assert meta["scanned"] < 30, "超时后不该继续扫完"
    assert meta["elapsed_ms"] >= 0


def test_no_timeout_scans_all(tmp_path):
    root = tmp_path / "fast"
    root.mkdir()
    for i in range(5):
        (root / ("f%d.md" % i)).write_text("## 标题\n\n向量召回。\n", encoding="utf-8")
    _hits, _src, meta = D.search_ex("向量", limit=10, dirs=[str(root)], timeout_ms=0)
    assert meta["timed_out"] is False
    assert meta["scanned"] == 5


def test_search_backward_compatible(docs_tree):
    """旧调用签名 `(hits, source)` 必须还能用（CLI 之外还有脚本在调）。"""
    hits, source = D.search("向量", dirs=[docs_tree])
    assert source == "explicit" and hits


# ── 统计 ────────────────────────────────────────────────────────────────
def test_stats_counts_md_files(docs_tree):
    st = D.stats(dirs=[docs_tree])
    assert st["files"] == 3, st
    assert st["source"] == "explicit"


def test_stats_when_off(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DOCS_ROOTS_FILE", str(tmp_path / "nope.json"))
    monkeypatch.delenv("DOCS_MCP_ROOTS", raising=False)
    st = D.stats()
    assert st["files"] == 0 and st["source"] == "none" and st["roots"] == []
