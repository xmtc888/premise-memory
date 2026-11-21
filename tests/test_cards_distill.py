# -*- coding: utf-8 -*-
"""SOP 结晶契约（2026-11-10，改进清单第 8 条）。

`cards distill` 是**零 LLM 的确定性重排**：同样的卡片永远产出同样的文本。
四条硬规矩：
  1. 结构固定（前提 → 结论 → 证据 → 被推翻历史），便于人扫读与后续 diff；
  2. **前提只取还活着的卡**（dead 卡的 scope 是当时条件，不能当现在的前提）；
  3. 被推翻的卡进"勿照做"一节且划删除线；
  4. 产出是**草稿**（文件头明说"不是结论文档"），不自动提交。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards as C  # noqa: E402


def _ev(q="摘录", msg=None):
    e = {"agent": "dsh", "date": "2026-11-10", "quote": q}
    if msg is not None:
        e["msg"] = msg
    return [e]


@pytest.fixture()
def lib(tmp_path):
    p = str(tmp_path / "cards.jsonl")
    alive = C.build("用机器检闸门", _ev("实测两天漏两次", 11), status="alive",
                    topic="打包", scope="v1.5+")
    stale = C.build("旧打包脚本还能用", _ev("路径还在"), status="stale", topic="打包")
    dead = C.build("手写白名单就够", _ev("当初这么想"), status="dead",
                   topic="打包", scope="v1.0 时代")
    C.upsert(alive, p)
    C.upsert(stale, p)
    C.upsert(dead, p)
    C.supersede(dead["id"], alive["id"], p)
    return p


def test_four_sections_present(lib, tmp_path):
    dst = str(tmp_path / "sop.md")
    r = C.distill("打包", dst=dst, path=lib)
    md = io.open(dst, encoding="utf-8").read()
    for head in ("# 打包 — 操作要点", "## 一、前提", "## 二、结论", "## 三、证据",
                 "## 四、历史：被推翻的做法"):
        assert head in md, head
    assert r["cards"] == 3


def test_prereq_excludes_dead_cards_scope(lib, tmp_path):
    """dead 卡的 scope（"v1.0 时代"）是历史条件，不能当现在的前提。"""
    md = C.distill("打包", path=lib)["text"]
    prereq = md.split("## 一、前提")[1].split("## 二、结论")[0]
    assert "v1.5+" in prereq
    assert "v1.0 时代" not in prereq, "已推翻卡的适用条件不该进前提"


def test_dead_goes_to_history_with_strikethrough(lib):
    md = C.distill("打包", path=lib)["text"]
    hist = md.split("## 四、历史")[1]
    assert "~~手写白名单就够~~" in hist
    assert "已推翻" in hist or "推翻" in hist


def test_draft_header_says_not_a_conclusion(lib):
    md = C.distill("打包", path=lib)["text"]
    assert "草稿" in md and "不是结论" in md


def test_deterministic_output(lib):
    a = C.distill("打包", path=lib)["text"]
    b = C.distill("打包", path=lib)["text"]
    assert a == b, "零 LLM 的产出必须可复现"


def test_counts_anchored_evidence(lib):
    r = C.distill("打包", path=lib)
    assert r["evidence"] == 3 and r["anchored"] == 1, r
    assert "无 `msg` 锚点" in r["text"], "无锚点必须如实提示"


def test_empty_topic_reports_reason(lib):
    r = C.distill("不存在的话题", path=lib)
    assert r["cards"] == 0 and r["reason"]


def test_status_counts_in_header(lib):
    r = C.distill("打包", path=lib)
    assert r["by_status"]["alive"] == 1
    assert r["by_status"]["stale"] == 1
    assert r["by_status"]["dead"] == 1


def test_distill_writes_only_when_dst_given(lib, tmp_path):
    r = C.distill("打包", path=lib)          # 不给 dst：只返回文本
    assert r["text"] and r["written"] == ""
    assert not list(tmp_path.glob("*.md"))
