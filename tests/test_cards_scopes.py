# -*- coding: utf-8 -*-
"""结构化作用域契约（2026-11-10，改进清单第 2 条）。

为什么需要：原来的 `scope` 是**自由文本**（"agent-memory >=1.5.3；seed 与 last_search_via"），
机器无法据此过滤 —— "按当前 cwd 注入相关结论""只导出一个项目的卡"都做不了。
借鉴 DSH 市场 `dsh-engram` 的 dual-scope（用户级 + 工作区级）。

三条硬规矩：
  1. **旧卡没有 scopes 字段 ⇒ 视为 machine**（向后兼容，不许把历史卡变没）；
  2. **项目作用域按路径前缀匹配**（`project:F:/x` 覆盖 `project:F:/x/docs`，反向也成立）；
  3. 写了非法 scope ⇒ **报错而不是静默丢弃**（静默丢弃会让卡片在最常见的过滤里消失）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards as C  # noqa: E402


def _ev():
    return [{"agent": "dsh", "date": "2026-11-10", "quote": "测试摘录"}]


# ── 解析与归一化 ────────────────────────────────────────────────────────
def test_parse_scopes_accepts_string_and_list():
    assert C.parse_scopes("machine") == ["machine"]
    assert C.parse_scopes("machine, project:F:\\demo-base") == ["machine", "project:f:/demo-base"]
    assert C.parse_scopes(["machine"]) == ["machine"]
    assert C.parse_scopes(None) == []
    assert C.parse_scopes("") == []


def test_parse_scopes_normalizes_path_form():
    """反斜杠/大小写/尾斜杠都要归一 —— 否则同一个项目会变成两个作用域。"""
    a = C.parse_scopes("project:F:\\Demo\\Base\\")
    b = C.parse_scopes("project:f:/demo/base")
    assert a == b == ["project:f:/demo/base"]


def test_parse_scopes_dedupes_keeping_order():
    assert C.parse_scopes("machine,machine,project:f:/a") == ["machine", "project:f:/a"]


def test_parse_scopes_keeps_invalid_for_validation():
    """非法值原样保留，交给 validate 报错（不静默丢）。"""
    assert C.parse_scopes("everywhere") == ["everywhere"]


# ── 旧卡兼容 ────────────────────────────────────────────────────────────
def test_legacy_card_without_scopes_is_machine():
    legacy = {"claim": "老卡", "status": "alive", "evidence": _ev()}
    assert C.card_scopes(legacy) == ["machine"]


def test_build_without_scopes_defaults_to_machine():
    """不传 scopes ⇒ machine。不能是空列表：空列表是错误（任何过滤都查不到）。"""
    c = C.build("新卡", _ev())
    assert c["scopes"] == ["machine"]
    assert C.validate(c) == []


def test_build_with_scopes():
    c = C.build("新卡", _ev(), scopes="machine,project:F:/demo")
    assert c["scopes"] == ["machine", "project:f:/demo"]
    assert C.validate(c) == []


# ── 校验 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scopes,needle", [
    ("everywhere", "非法 scope"),
    ("project:", "缺路径"),
    ("", "空列表"),
])
def test_invalid_scopes_rejected(scopes, needle):
    c = C.build("卡", _ev(), scopes=scopes)
    errs = C.validate(c)
    assert any(needle in e for e in errs), errs


def test_non_list_scopes_rejected_by_validate_directly():
    """`build()` 会先规范化，所以"必须是列表"只能在直接喂 validate 时命中
    （真实来源：手改 JSON / 外部导入）。"""
    c = C.build("卡", _ev())
    c["scopes"] = {"a": 1}
    assert any("必须是列表" in e for e in C.validate(c)), C.validate(c)


def test_explicit_empty_scopes_is_error_not_legacy():
    """显式空列表 ≠ 缺省：缺省是 machine，空列表是在任何过滤里都查不到（静默消失）。"""
    c = C.build("卡", _ev())
    c["scopes"] = []
    assert any("空列表" in e for e in C.validate(c)), C.validate(c)


# ── 匹配 ────────────────────────────────────────────────────────────────
def test_scope_match_machine_exact():
    c = C.build("全机卡", _ev(), scopes="machine")
    assert C.scope_matches(c, "machine") is True
    assert C.scope_matches(c, "project:f:/demo") is False


def test_project_scope_prefix_both_directions():
    """项目作用域按前缀匹配：父子目录互相覆盖（否则子项目查不到父项目的结论）。"""
    c = C.build("项目卡", _ev(), scopes="project:f:/demo")
    assert C.scope_matches(c, "project:f:/demo") is True
    assert C.scope_matches(c, "project:f:/demo/docs") is True, "子目录应命中父项目"
    assert C.scope_matches(c, "project:f:/other") is False


def test_scope_match_empty_filter_matches_all():
    c = C.build("卡", _ev(), scopes="machine")
    assert C.scope_matches(c, "") is True


# ── search 过滤 ─────────────────────────────────────────────────────────
def test_search_filters_by_scope(tmp_path):
    p = str(tmp_path / "cards.jsonl")
    C.upsert(C.build("全机结论", _ev(), topic="t", scopes="machine"), p)
    C.upsert(C.build("项目结论", _ev(), topic="t", scopes="project:f:/demo"), p)
    assert len(C.search("", p)) == 2
    assert [c["claim"] for c in C.search("", p, scope="machine")] == ["全机结论"]
    assert [c["claim"] for c in C.search("", p, scope="project:f:/demo")] == ["项目结论"]
    assert [c["claim"] for c in C.search("", p, scope="project:f:/demo/sub")] == ["项目结论"]


def test_render_shows_scopes():
    c = C.build("卡", _ev(), scopes="machine")
    assert "scopes=machine" in C.render(c)
