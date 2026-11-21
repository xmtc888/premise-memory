# -*- coding: utf-8 -*-
"""注入块的「成本可见」契约（claude-mem 的头部做法）。

## 为什么要这条

注入是**每轮成本**。如果块里不写"这一块花多少 token"，读者就无法判断
"该不该为它付上下文"，也无法在预算紧时做取舍——那注入就成了黑箱收费。
本仓的做法：**块 = 索引层**（短），细节层用展开命令（按需付费），
并且块头明确写出各段成本。

两条不可退让：
  ① MARKER 必须在**第一行**（`inject --check` / doctor 靠它识别文件头）；
  ② 成本行必须在 MARKER **之后**（机器识别优先，人读信息其次）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli import _insert_cost_line                                  # noqa: E402
from inject import MARKER, freshness                               # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "index", "sessions.db")


def test_cost_line_inserted_after_marker():
    text = "%s  check_at=2026-11-17T00:00:00  stale_after_days=3 -->\n## 环境事实\n\n内容\n" % MARKER
    out = _insert_cost_line(text, "> 本块约 100 token（环境事实 约 100 token）")
    lines = out.splitlines()
    assert lines[0].startswith(MARKER), lines[0]
    assert lines[1] == ""
    assert "本块约 100 token" in lines[2]
    # 原内容仍然完整
    assert "## 环境事实" in out and "内容" in out


def test_cost_line_without_marker_goes_first():
    """没有 MARKER（例如只注入卡片块）时成本行放最前，不能丢内容。"""
    out = _insert_cost_line("### 相关结论\n- [成立] x\n", "> 本块约 10 token")
    assert out.startswith("> 本块约 10 token")
    assert "### 相关结论" in out


def test_generated_block_keeps_freshness_readable(tmp_path):
    """插入成本行后，freshness 仍能从文件头读到 check_at（回归 2026-11-17 的坑）。"""
    text = "%s  check_at=2026-11-17T10:00:00  stale_after_days=3 -->\n## 环境事实\n" % MARKER
    out = _insert_cost_line(text, "> 本块约 600 token（环境事实 约 500 token）")
    p = tmp_path / "ctx.md"
    p.write_text(out, encoding="utf-8")
    f = freshness(str(p), 3, now="2026-11-17T12:00:00")
    assert f["exists"] and not f["stale"], f
    assert f["check_at"] == "2026-11-17T10:00:00", f


def test_real_inject_has_cost_line():
    """真实注入命令要带上成本行与展开指引（跳过硬编码依赖：无库则跳过）。"""
    import pytest
    if not os.path.exists(DB):
        pytest.skip("本机无索引库")
    from cli import cmd_inject

    class Args:
        db = DB
        entity = "示例知识库"
        cards = False
        procedures = True
        max_chars = 1400
        stale_days = 3
        write = ""
        topic = ""
        scope_filter = ""
        max_cards = 5
        card_chars = 900
        include_dead = False
        max_procedures = 3
        max_procedure_repairs = 1
        procedure_chars = 500
        check = False

    rc = cmd_inject(Args())
    assert rc == 0
