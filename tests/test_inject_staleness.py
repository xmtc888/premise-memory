# -*- coding: utf-8 -*-
"""注入块过期自报（2026-11-09）：`inject --check` 与 doctor 都靠它。

为什么必须钉：这是"日更任务悄悄停了"的唯一机器信号。
实测教训：demo-kb 的 .agent-memory/context.md 停在 2026-11-03，5.9 天没人发现，
而 agent 会照着里面"旧目录已改名"这类事实工作 —— 过期事实比没有事实更危险。
"""
import io
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inject  # noqa: E402


def _write(path, check_at, marker=True):
    head = ('<!-- agent-memory:auto-generated  check_at=%s  stale_after_days=3 -->\n'
            % check_at) if marker else "# 别的东西\n"
    io.open(path, "w", encoding="utf-8", newline="\n").write(head + "## 环境事实\n")
    return str(path)


def _stamp(days_ago: float) -> str:
    t = datetime.now() - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def test_fresh_block_is_not_stale(tmp_path):
    p = _write(tmp_path / "c.md", _stamp(0.2))
    f = inject.freshness(p, stale_days=3)
    assert f["exists"] is True and f["stale"] is False
    assert 0 <= f["age_days"] < 1


def test_old_block_is_stale(tmp_path):
    """5.9 天 / 阈值 3 ⇒ 必须报过期（正是 demo-kb 当时的实际情况）。"""
    p = _write(tmp_path / "c.md", _stamp(5.9))
    f = inject.freshness(p, stale_days=3)
    assert f["stale"] is True
    assert 5.5 < f["age_days"] < 6.5, f["age_days"]
    assert "过期" in f["reason"] and "日更" in f["reason"]


def test_threshold_boundary(tmp_path):
    p = _write(tmp_path / "c.md", _stamp(2.0))
    assert inject.freshness(p, stale_days=3)["stale"] is False
    assert inject.freshness(p, stale_days=1)["stale"] is True


def test_missing_file_reported_not_raised(tmp_path):
    f = inject.freshness(str(tmp_path / "nope.md"))
    assert f["exists"] is False and f["stale"] is True
    assert "还没生成" in f["reason"]


def test_file_without_check_at_is_stale(tmp_path):
    """没有 check_at 就无从判断新鲜度 ⇒ 按过期处理（不许假装新鲜）。"""
    p = _write(tmp_path / "c.md", "", marker=False)
    f = inject.freshness(p)
    assert f["stale"] is True
    assert "check_at" in f["reason"]


def test_check_at_comes_from_header_not_mtime(tmp_path):
    """判据是文件头的 check_at，不是 mtime：文件可以被复制/git 检出，mtime 会骗人。"""
    p = _write(tmp_path / "c.md", _stamp(10))
    os.utime(p, None)                       # mtime = 现在
    f = inject.freshness(p, stale_days=3)
    assert f["stale"] is True, "mtime 新不代表内容新"
    assert f["age_days"] > 9


def test_default_threshold_is_three_days():
    assert inject.STALE_AFTER_DAYS == 3


def test_build_injection_header_has_stale_days(tmp_path):
    """生成的头里要写明阈值，读的人（或 agent）才知道多少天算过期。"""
    p = _write(tmp_path / "c.md", _stamp(0.1))
    text = io.open(p, encoding="utf-8").read()
    assert "stale_after_days=3" in text
