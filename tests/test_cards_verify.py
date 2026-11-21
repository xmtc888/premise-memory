# -*- coding: utf-8 -*-
"""机器状态类卡的**声明式活体检查**契约（2026-11-18）。

## 为什么需要这层

「codex 命令经 PATH 指向便携版 0.147」这类结论，证据不是"谁说过什么"，而是
**当时的机器状态**。用聊天消息（msg 锚点）复核它，等于把"可复核"降级成"有人说过"——
而机器状态会变（升级换目录、PATH 被改）。所以给它一个活体检查通道：
每天跑一次，过了就是"现在仍然成立"，不过就说明卡过期了。

## 硬约束

- **只允许声明式检查**（file_exists / dir_exists / path_env_contains），
  **不执行任意命令**：卡片是本机数据文件，让它能跑命令等于开后门；
- 检查失败要**报出来**（退出码 1），不能静默；
- 同一 claim 重新 add 时不能"推翻自己"（实测踩到，会渲染成自环）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards as C                                            # noqa: E402


# ── 声明式检查的解析与校验 ───────────────────────────────────────────────
def test_parse_verify_spec_ok():
    assert C.parse_verify_spec("dir_exists:C:\\x") == {"kind": "dir_exists", "path": "C:\\x"}
    assert C.parse_verify_spec("path_env_contains:C:\\y") == {
        "kind": "path_env_contains", "value": "C:\\y"}


def test_parse_verify_spec_rejects_unknown_kind():
    with pytest.raises(ValueError) as ei:
        C.parse_verify_spec("rm_rf:C:\\x")
    assert "未知检查类型" in str(ei.value)


def test_parse_verify_spec_rejects_empty_value():
    with pytest.raises(ValueError):
        C.parse_verify_spec("dir_exists:")


def test_validate_rejects_bad_verify():
    base = {"claim": "x", "status": "alive",
            "evidence": [{"agent": "dsh", "date": "2026-11-18", "quote": "q"}]}
    bad = dict(base, verify=[{"kind": "rm_rf", "path": "C:\\x"}])
    assert any("未知检查类型" in e for e in C.validate(bad))
    bad2 = dict(base, verify=[{"kind": "dir_exists"}])       # 缺 path
    assert any("缺 path" in e for e in C.validate(bad2))
    ok = dict(base, verify=[{"kind": "dir_exists", "path": "C:\\x"}])
    assert C.validate(ok) == []


# ── 检查执行 ─────────────────────────────────────────────────────────────
def test_run_verify_file_and_dir(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    card = {"verify": [
        {"kind": "file_exists", "path": str(f)},
        {"kind": "dir_exists", "path": str(tmp_path)},
        {"kind": "file_exists", "path": str(tmp_path / "nope.txt")},
    ]}
    res = C.run_verify(card)
    assert [r["ok"] for r in res] == [True, True, False]
    assert "不存在" in res[2]["detail"]


def test_run_verify_path_env(monkeypatch):
    monkeypatch.setenv("PATH", "C:\\Windows;C:\\Users\\x\\CodexPortable\\app\\resources")
    card = {"verify": [{"kind": "path_env_contains", "value": "c:\\users\\x\\codexportable"}]}
    res = C.run_verify(card)
    assert res[0]["ok"] is True          # 大小写不敏感


def test_verify_machine_cards_summary(tmp_path):
    ok_dir = str(tmp_path)
    cards = [
        {"id": "c1", "claim": "a", "verify": [{"kind": "dir_exists", "path": ok_dir}]},
        {"id": "c2", "claim": "b", "verify": [{"kind": "dir_exists", "path": str(tmp_path / "no")}]},
        {"id": "c3", "claim": "c"},                       # 无检查
    ]
    r = C.verify_machine_cards(cards)
    assert (r["cards"], r["with_checks"], r["passed"], r["failed"], r["unchecked"]) == (3, 2, 1, 1, 1)
    assert r["items"][0]["ok"] is True and r["items"][1]["ok"] is False


def test_run_verify_tolerates_broken_check():
    """坏掉的检查项（未知类型）不能抛异常——要变成一条失败结果报出来。"""
    res = C.run_verify({"verify": [{"kind": "nope", "path": "x"}]})
    assert res and res[0]["ok"] is False and "未知检查类型" in res[0]["detail"]


# ── 自环防呆（实测踩到）───────────────────────────────────────────────
def test_upsert_drops_self_supersede(tmp_path, monkeypatch):
    """同一 claim 重新写入时，supersedes 里带自己的 id 必须被剔掉。

    2026-11-18 实测：`cards add --supersedes-card <同一张卡>` 命中同一 claim ⇒
    卡片"推翻它自己"，渲染成 `⇢ 推翻了 c-xxxx`（就是它自己），关系图变自环。
    """
    monkeypatch.setattr(C, "cards_file", lambda: str(tmp_path / "cards.jsonl"))
    ev = [{"agent": "dsh", "date": "2026-11-18", "quote": "q"}]
    first = C.build("同一句断言", ev, topic="t")
    cid = first["id"]
    second = C.build("同一句断言", ev, topic="t", supersedes=[cid])
    state, _msg = C.upsert(second)
    assert state in ("added", "updated")
    got = C.load()[0][0]
    assert got["id"] == cid
    assert got.get("supersedes") in (None, [], []), got.get("supersedes")
    assert not got.get("superseded_by"), "不能把自己标成被自己推翻"
