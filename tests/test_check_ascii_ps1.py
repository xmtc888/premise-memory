# -*- coding: utf-8 -*-
"""`tools/check_ascii_ps1.py` 的契约：非 ASCII 脚本必须被拦，且报出的位置可定位。

## 为什么这条规则要机器守（2026-11-17 一天内三次踩坑）

PowerShell 5.1 读**无 BOM 的 .ps1** 按系统 ANSI 代码页（中文机 = GBK）解码：
一个中文字符就会 mojibake 并破坏解析，报错位置与真实错因完全对不上。
三次实例：refresh.ps1 中文注释 195 字节 / 早期 probe 脚本中文路径变 `F:\\鍟嗘満\\…` /
内联 `python -c` 被 PS 二次转义。靠自觉记不住，所以有 pre-commit 闸门 + 本测试。
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "check_ascii_ps1", os.path.join(ROOT, "tools", "check_ascii_ps1.py"))
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)


def test_pure_ascii_passes(tmp_path):
    p = tmp_path / "ok.ps1"
    p.write_bytes(b"# pure ascii\n$env:X = 1\n")
    ok, problems, bom = C.check_file(str(p))
    assert ok and problems == [] and bom is False


def test_cjk_is_reported_with_position(tmp_path):
    p = tmp_path / "bad.ps1"
    p.write_bytes("# 中文注释\nWrite-Output 1\n".encode("utf-8"))
    ok, problems, _bom = C.check_file(str(p))
    assert not ok
    assert problems, "必须报出违规字节"
    first = problems[0]
    assert first["line"] == 1
    assert first["col"] >= 1
    assert first["byte"] >= 0x80
    # 必须能给出"PS 5.1 会读成什么"（GBK 乱码），否则用户看不出后果
    assert first["gbk"], "缺少 GBK 预览"


def test_crlf_is_fine(tmp_path):
    p = tmp_path / "crlf.ps1"
    p.write_bytes(b"# ascii\r\n$env:X = 1\r\n")
    ok, _problems, _bom = C.check_file(str(p))
    assert ok


def test_bom_counts_as_violation(tmp_path):
    """BOM 的三个字节本身就是非 ASCII ⇒ 按"脚本纯 ASCII"规则算违规。

    语义澄清：BOM 会让 PS 5.1 改按 UTF-8 读（所以带 BOM 的中文脚本"能跑"），
    但本仓规则是**脚本只写 ASCII**（跨代码页可移植），所以 BOM 也要求去掉。
    """
    p = tmp_path / "bom.ps1"
    p.write_bytes(b"\xef\xbb\xbf# ascii\n")
    ok, problems, bom = C.check_file(str(p))
    assert ok is False and bom is True
    assert problems and problems[0]["byte"] == 0xEF


def test_scan_skips_dependency_dirs(tmp_path, monkeypatch):
    """扫仓库时不能扫进 .venv / dist / work（否则会报一堆无关文件）。"""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / ".venv").mkdir()
    (root / "work").mkdir()
    (root / "scripts" / "ok.ps1").write_bytes(b"$a=1\n")
    (root / "work" / "bad.ps1").write_bytes("# 中文\n".encode("utf-8"))
    monkeypatch.setattr(C, "REPO", str(root))
    found = [os.path.basename(p) for p in C.iter_repo_scripts(str(root))]
    assert found == ["ok.ps1"], found


def test_repo_itself_is_clean():
    """仓库自身的脚本必须全 ASCII（这条挂了说明有人又写了中文进去）。"""
    bad = []
    for path in C.iter_repo_scripts():
        ok, problems, _bom = C.check_file(path)
        if not ok:
            bad.append((os.path.relpath(path, ROOT), len(problems)))
    assert bad == [], "以下脚本含非 ASCII：%r" % (bad,)
