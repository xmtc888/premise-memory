# -*- coding: utf-8 -*-
"""跨宿主指令片段安装器的契约。

## 为什么这些性质必须钉住

用户明确划了边界：**能改自己机器，不能替别人改配置**。所以安装器必须做到：
  ① **先预览**（--dry-run 打印将写入的块，不落盘）；
  ② **幂等**（重复安装不产生第二份块）；
  ③ **可精确撤销**（--revert 只删标记块，文件里其它内容逐字节不变）；
  ④ **不破坏原文件**（保留 CRLF / BOM，不把别人的文件改成 LF）。
这四条任何一条破掉，"别人自己跑一条命令"就不安全了。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import install_host as IH                                     # noqa: E402


def test_install_creates_marked_block(tmp_path):
    p = tmp_path / "AGENTS.md"
    r = IH.install("codex", project=str(tmp_path))
    assert r["action"] == "created"
    text = p.read_text(encoding="utf-8")
    assert IH.MARK_BEGIN in text and IH.MARK_END in text
    assert "context.md" in text and "只读一次" in text
    # 创建的块要带 created=1，撤销时据此决定是否删文件
    assert "created=1" in text


def test_install_is_idempotent(tmp_path):
    IH.install("codex", project=str(tmp_path))
    p = tmp_path / "AGENTS.md"
    before = p.read_text(encoding="utf-8")
    r2 = IH.install("codex", project=str(tmp_path))
    assert r2["action"] == "unchanged"
    assert p.read_text(encoding="utf-8") == before
    # 只能有一份块
    assert before.count(IH.MARK_BEGIN) == 1


def test_install_preserves_existing_content(tmp_path):
    p = tmp_path / "CLAUDE.md"
    original = "# 我的规则\n\n- 始终用中文\n"
    p.write_text(original, encoding="utf-8", newline="\n")
    IH.install("claude", project=str(tmp_path))
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# 我的规则")
    assert "始终用中文" in text
    assert IH.MARK_BEGIN in text


def test_revert_restores_original_exactly(tmp_path):
    """撤销后文件必须与安装前逐字节一致（这是"可撤销"的硬标准）。"""
    p = tmp_path / "AGENTS.md"
    original = "# 规则\n\n- 不要改生成目录\n"
    p.write_text(original, encoding="utf-8", newline="\n")
    IH.install("codex", project=str(tmp_path))
    assert p.read_text(encoding="utf-8") != original
    r = IH.revert("codex", project=str(tmp_path))
    assert r["action"] == "reverted"
    assert p.read_text(encoding="utf-8") == original


def test_revert_deletes_file_it_created(tmp_path):
    """文件是安装器创建的 ⇒ 撤销后整体删除（不留空壳）。"""
    IH.install("claude", project=str(tmp_path))
    p = tmp_path / "CLAUDE.md"
    assert p.exists()
    r = IH.revert("claude", project=str(tmp_path))
    assert r["action"] == "deleted-file"
    assert not p.exists()


def test_revert_when_not_installed_is_noop(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text("# 只读文件\n", encoding="utf-8")
    r = IH.revert("codex", project=str(tmp_path))
    assert r["action"] == "not-installed"
    assert p.read_text(encoding="utf-8") == "# 只读文件\n"


def test_preserves_crlf_and_bom(tmp_path):
    """别人的文件是 CRLF+BOM 时，不能被我们改成 LF 或丢 BOM。"""
    p = tmp_path / "AGENTS.md"
    p.write_bytes(b"\xef\xbb\xbf# \xe8\xa7\x84\xe5\x88\x99\r\n\r\n- x\r\n")
    IH.install("codex", project=str(tmp_path))
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")          # BOM 还在
    assert b"\r\n" in raw                          # 仍是 CRLF
    IH.revert("codex", project=str(tmp_path))
    raw2 = p.read_bytes()
    assert raw2.startswith(b"\xef\xbb\xbf")
    assert IH.MARK_BEGIN.encode() not in raw2


def test_dry_run_does_not_write(tmp_path):
    p = tmp_path / "AGENTS.md"
    orig = "# 规则\n"
    p.write_text(orig, encoding="utf-8")
    plan = IH.plan("codex", project=str(tmp_path))
    assert IH.MARK_BEGIN in plan["block"]
    assert p.read_text(encoding="utf-8") == orig     # 预览不落盘


def test_status_reports_installation(tmp_path):
    IH.install("codex", project=str(tmp_path))
    st = IH.status(IH.target_path("codex", str(tmp_path)))
    assert st["exists"] and st["installed"]


def test_unknown_host_rejected():
    import pytest
    with pytest.raises(ValueError):
        IH.target_path("vscode")
