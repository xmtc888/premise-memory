# -*- coding: utf-8 -*-
"""结论卡契约（2026-11-08）：卡片层是"检索之上再加一层编译产物"，所以它的边界必须钉死。

为什么这些测试值得写：
  ① 卡片是**跨机迁移**的载体 —— 本体坏了、被截断、被手工编辑是常态，读侧必须"跳过坏行继续用"，
     否则一张卡的语法错误就能让整库检索变砖（这正是我们刚在 changelog 控制字符事件里吃过的教训）；
  ② `supersede` 是这套东西的价值所在（"过去这么认为、现在不成立"），链条写错就变成永久谣言；
  ③ 导入必须幂等 —— 同一份导出文件重复导入不应产生重复卡。
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards as C  # noqa: E402


def _ev(agent="dsh", date="2026-11-08", quote="原文摘录", session=""):
    e = {"agent": agent, "date": date, "quote": quote}
    if session:
        e["session"] = session
    return e


@pytest.fixture()
def path(tmp_path):
    return str(tmp_path / "cards.jsonl")


# ── 校验 ────────────────────────────────────────────────────────────────
def test_valid_card_passes():
    assert C.validate(C.build("断言", [_ev()])) == []


@pytest.mark.parametrize("card_kwargs,needle", [
    ({"claim": ""}, "缺 claim"),
    ({"status": "nope"}, "status 非法"),
    ({"evidence": []}, "缺 evidence"),
    ({"evidence": [{"agent": "dsh", "date": "2026-11-08"}]}, "缺 quote"),
    ({"evidence": [{"agent": "dsh", "quote": "x"}]}, "日期格式"),
    ({"evidence": [{"date": "2026-11-08", "quote": "x"}]}, "缺 agent"),
])
def test_invalid_cards_rejected(card_kwargs, needle):
    kwargs = {"claim": "断言", "evidence": [_ev()]}
    kwargs.update(card_kwargs)
    errs = C.validate(C.build(**kwargs))
    assert any(needle in e for e in errs), errs


def test_status_accepts_chinese_aliases():
    c = C.build("断言", [_ev()], status="已被推翻")
    assert c["status"] == "dead"
    assert C.validate(c) == []


def test_supersede_and_superseded_by_on_same_card_rejected():
    c = C.build("断言", [_ev()], status="dead", supersedes=["c-aaaaaaaa"])
    c["superseded_by"] = "c-bbbbbbbb"
    assert any("成环" in e for e in C.validate(c))


# ── 增改 ────────────────────────────────────────────────────────────────
def test_upsert_add_then_idempotent(path):
    card = C.build("断言", [_ev()])
    assert C.upsert(card, path)[0] == "added"
    assert C.upsert(card, path)[0] == "exists"      # 同内容重复写入不产生第二张
    assert len(C.load(path)[0]) == 1


def test_upsert_update_keeps_created_at(path):
    card = C.build("断言", [_ev()])
    C.upsert(card, path)
    created = C.load(path)[0][0]["created_at"]
    card2 = dict(card, claim="断言（改）", id=card["id"])
    state, _ = C.upsert(card2, path)
    assert state == "updated"
    rows = C.load(path)[0]
    assert len(rows) == 1
    assert rows[0]["created_at"] == created
    assert rows[0]["claim"] == "断言（改）"


def test_upsert_rejects_invalid(path):
    state, msg = C.upsert({"claim": "", "evidence": []}, path)
    assert state == "invalid" and "缺 claim" in msg
    assert C.load(path)[0] == []                    # 非法卡不许落盘


def test_id_is_content_derived():
    a = C.build("同一句断言", [_ev()], topic="t")
    b = C.build("同一句断言", [_ev(quote="不同的摘录")], topic="t")
    assert a["id"] == b["id"], "id 只由 claim+topic 派生，改证据不该换 id"
    assert a["id"].startswith("c-")


# ── 推翻链 ──────────────────────────────────────────────────────────────
def test_supersede_links_both_sides(path):
    old = C.build("旧结论", [_ev()])
    new = C.build("新结论", [_ev()])
    C.upsert(old, path)
    C.upsert(new, path)
    ok, msg = C.supersede(old["id"], new["id"], path)
    assert ok, msg
    by_id = {c["id"]: c for c in C.load(path)[0]}
    assert by_id[old["id"]]["status"] == "dead"
    assert by_id[old["id"]]["superseded_by"] == new["id"]
    assert new["id"] in [by_id[old["id"]]["superseded_by"]]
    assert old["id"] in by_id[new["id"]]["supersedes"]


def test_supersede_missing_or_self(path):
    c = C.build("只有一张", [_ev()])
    C.upsert(c, path)
    assert C.supersede("c-deadbeef", c["id"], path)[0] is False
    assert C.supersede(c["id"], c["id"], path)[0] is False


# ── 查 ──────────────────────────────────────────────────────────────────
def test_search_matches_claim_topic_and_quote(path):
    C.upsert(C.build("打包清单要含全部模块", [_ev(quote="漏了 aliases.py 会崩")],
                     topic="打包", scope="v1.5.3"), path)
    C.upsert(C.build("另一种结论", [_ev(quote="无关")], topic="别的"), path)
    assert len(C.search("模块", path)) == 1
    assert len(C.search("打包", path)) == 1
    assert len(C.search("会崩", path)) == 1          # 命中 evidence.quote
    assert len(C.search("", path)) == 2             # 空查询 = 全部


def test_search_status_filter(path):
    C.upsert(C.build("甲", [_ev()], status="alive"), path)
    C.upsert(C.build("乙", [_ev()], status="dead"), path)
    assert len(C.search("", path, status="dead")) == 1
    assert len(C.search("", path, status="已被推翻")) == 1      # 认得中文别名


# ── 读侧韧性 ────────────────────────────────────────────────────────────
def test_missing_file_is_empty_not_error(path):
    rows, warns = C.load(path)
    assert rows == [] and warns == []


def test_broken_lines_are_skipped_with_warning(path):
    io.open(path, "w", encoding="utf-8", newline="\n").write(
        json.dumps(C.build("好卡", [_ev()]), ensure_ascii=False) + "\n"
        "{ 这不是 json\n"
        "[1,2,3]\n"
        "\n"
        "# 注释行\n")
    rows, warns = C.load(path)
    assert len(rows) == 1, rows
    assert len(warns) == 2, warns                    # 坏 JSON + 非对象各一条
    assert all("行" in w for w in warns)


def test_save_is_atomic_no_tmp_left(path):
    C.upsert(C.build("断言", [_ev()]), path)
    assert not os.path.exists(path + ".tmp")


# ── 迁移 ────────────────────────────────────────────────────────────────
def test_export_import_roundtrip(path, tmp_path):
    C.upsert(C.build("甲", [_ev()]), path)
    C.upsert(C.build("乙", [_ev()], status="dead"), path)
    dst = str(tmp_path / "exported.jsonl")
    n, _ = C.export_to(dst, path)
    assert n == 2
    empty = str(tmp_path / "other.jsonl")
    stats, _ = C.import_from(dst, path=empty)
    assert stats["added"] == 2 and stats["total"] == 2
    assert len(C.load(empty)[0]) == 2


def test_import_is_idempotent(path, tmp_path):
    C.upsert(C.build("甲", [_ev()]), path)
    dst = str(tmp_path / "e.jsonl")
    C.export_to(dst, path)
    stats, _ = C.import_from(dst, path=path)
    assert stats["added"] == 0 and stats["skipped"] == 1
    assert len(C.load(path)[0]) == 1


def test_import_merge_prefers_newer_updated_at(path, tmp_path):
    src = str(tmp_path / "incoming.jsonl")
    old = C.build("甲", [_ev(quote="旧摘录")])
    old["updated_at"] = "2026-01-01T00:00:00"
    C.upsert(old, path)

    newer = dict(old, evidence=[_ev(quote="新摘录")], updated_at="2026-12-31T00:00:00")
    io.open(src, "w", encoding="utf-8", newline="\n").write(
        json.dumps(newer, ensure_ascii=False) + "\n")
    stats, _ = C.import_from(src, path=path)
    assert stats["updated"] == 1
    assert C.load(path)[0][0]["evidence"][0]["quote"] == "新摘录"


def test_import_replace_overwrites(path, tmp_path):
    C.upsert(C.build("甲", [_ev()]), path)
    src = str(tmp_path / "incoming.jsonl")
    io.open(src, "w", encoding="utf-8", newline="\n").write(
        json.dumps(C.build("乙", [_ev()]), ensure_ascii=False) + "\n")
    stats, _ = C.import_from(src, mode="replace", path=path)
    assert stats["total"] == 1
    assert C.load(path)[0][0]["claim"] == "乙"


def test_import_warns_about_session_bound_evidence(path, tmp_path):
    src = str(tmp_path / "incoming.jsonl")
    card = C.build("甲", [_ev(session="dsh:session-abc")])
    io.open(src, "w", encoding="utf-8", newline="\n").write(
        json.dumps(card, ensure_ascii=False) + "\n")
    _stats, warns = C.import_from(src, path=path)
    assert any("无法回查原文" in w for w in warns), warns


# ── 误编码容错（2026-11-07 GLM 复核发现：读侧只接了 OSError）────────────────
def test_non_utf8_file_degrades_to_empty_table(path):
    """cards.jsonl 被记事本 ANSI / PowerShell 5.1 的 `>` 重定向写成 GBK 时：

    `io.open(encoding="utf-8")` 抛 **UnicodeDecodeError**，而它不继承 OSError ⇒ 原来会
    让 list/search/stats/validate/import/export **全部 traceback**。规矩 3 说"读不了就当空表"，
    所以这里断言：不抛异常、返回空表、给出 warning。
    """
    card = C.build("本来是张好卡", [_ev()])
    io.open(path, "w", encoding="gbk", newline="\n").write(
        json.dumps(card, ensure_ascii=False) + "\n")
    rows, warns = C.load(path)
    assert rows == []
    assert wants_unicode_warning(warns), warns


def wants_unicode_warning(warns) -> bool:
    return any("读不了" in w and "UnicodeDecodeError" in w for w in warns)


def test_utf8_bom_file_is_read_not_truncated(path):
    """带 BOM 的 cards.jsonl（Windows 记事本默认）：`utf-8` 读会把首行判成坏 JSON ⇒ 丢一张卡。

    改 `utf-8-sig` 后应完整读回。
    """
    card = C.build("带 BOM 也要读得到", [_ev()])
    io.open(path, "w", encoding="utf-8-sig", newline="\n").write(
        json.dumps(card, ensure_ascii=False) + "\n")
    rows, warns = C.load(path)
    assert len(rows) == 1, warns
    assert rows[0]["claim"] == "带 BOM 也要读得到"
    assert warns == []


# ── msg 锚点（等价 node_id 下钻，2026-11-09 借鉴 TencentDB Agent Memory）──
def test_evidence_msg_anchor_validation():
    good = C.build("带锚点", [{"agent": "dsh", "date": "2026-11-09", "quote": "摘录", "msg": 42}])
    assert C.validate(good) == []
    # 字符串数字也接受（CLI 从参数传进来就是字符串）
    assert C.validate(C.build("带锚点", [{"agent": "dsh", "date": "2026-11-09",
                                          "quote": "摘录", "msg": "42"}])) == []
    bad = C.build("坏锚点", [{"agent": "dsh", "date": "2026-11-09",
                              "quote": "摘录", "msg": "abc"}])
    assert any("msg 必须是整数" in e for e in C.validate(bad)), C.validate(bad)


def test_render_shows_msg_anchor():
    card = C.build("带锚点", [{"agent": "dsh", "date": "2026-11-09",
                               "quote": "摘录", "msg": 42}])
    assert "#42" in C.render(card)


# ── 写前快照 / 恢复（借鉴 BackupManager + fail-soft）──
def test_save_creates_backup_before_overwrite(path):
    C.upsert(C.build("第一张", [_ev()]), path)
    assert C.backups(path) == []          # 首次写：没有旧文件可备份
    C.upsert(C.build("第二张", [_ev()]), path)
    baks = C.backups(path)
    assert len(baks) == 1, baks
    rows, _ = C.load(baks[0])
    assert [c["claim"] for c in rows] == ["第一张"], "快照必须是写入前的内容"


def test_backups_are_pruned_to_max(path, monkeypatch):
    monkeypatch.setattr(C, "MAX_BACKUPS", 3)
    for i in range(6):
        C.upsert(C.build("第%d张" % i, [_ev(quote="q%d" % i)]), path)
    assert len(C.backups(path)) == 3


def test_restore_brings_back_previous_state(path):
    C.upsert(C.build("甲", [_ev()]), path)
    C.upsert(C.build("乙", [_ev()]), path)
    C.save([], path)                       # 误操作：清空
    assert C.load(path)[0] == []
    ok, msg = C.restore("", path)
    assert ok, msg
    claims = [c["claim"] for c in C.load(path)[0]]
    assert "乙" in claims or "甲" in claims, claims


def test_restore_without_backup_fails_soft(path):
    C.upsert(C.build("唯一一张", [_ev()]), path)
    ok, msg = C.restore("", path)
    assert ok is False and "没有可用的快照" in msg


def test_restore_bad_snapshot_does_not_touch_current(path, tmp_path):
    C.upsert(C.build("好卡", [_ev()]), path)
    bad = tmp_path / "cards.jsonl.20260101-000000.bak"
    bad.write_bytes(b"\xff\xfe\x00 not utf8")
    ok, msg = C.restore(str(bad), path)
    assert ok is False
    assert len(C.load(path)[0]) == 1, "快照读不了时不许动当前文件"


# ── Markdown 导出（白盒：顶层存可读文档，能被 docs 层检索）──
def test_export_md_groups_by_topic(path, tmp_path):
    C.upsert(C.build("甲的断言", [_ev(quote="甲的证据")], topic="检索"), path)
    C.upsert(C.build("乙的断言", [_ev(quote="乙的证据")], topic="打包", scope="v1.5.9"), path)
    dst = str(tmp_path / "cards.md")
    n, _ = C.export_md(dst, path)
    assert n == 2
    md = io.open(dst, encoding="utf-8").read()
    assert "## 检索" in md and "## 打包" in md
    assert "甲的断言" in md and "甲的证据" in md
    assert "适用范围：v1.5.9" in md
    assert "status=alive" in md


def test_export_md_marks_dead_cards(path, tmp_path):
    old = C.build("旧结论", [_ev()])
    new = C.build("新结论", [_ev()])
    C.upsert(old, path)
    C.upsert(new, path)
    C.supersede(old["id"], new["id"], path)
    dst = str(tmp_path / "cards.md")
    C.export_md(dst, path)
    md = io.open(dst, encoding="utf-8").read()
    assert "`status=dead`" in md
    assert "已被 `%s` 推翻" % new["id"] in md
