# -*- coding: utf-8 -*-
"""注入块契约：压缩、正确后继、预算、不产生荒谬规则。

## 为什么这几条必须钉住

注入块是**唯一真正进到 agent 上下文里的东西**——它错了就是主动误导，比不注入更糟。
实测踩过的两个错：
  ① 后继目录按字母序 → 写成 `f:\\kbwk-demo`，正确答案是 `f:\\demo-kb`（最近在用的那个）
  ② 容器目录混进规则表 → 产生 `c:\\users → f:\\demo-kb` 这种荒谬规则
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inject import _collapse, _render_dead, build_injection, MARKER   # noqa: E402
from topics import ENTITIES                            # noqa: E402

#: 真库用例依赖**本机数据**（`index/sessions.db` + 本机实体表）。
#: 换台机器装完 install 后，实体名是新机器的目录名，这些断言无从成立——
#: 所以显式 skip 并说明原因（不是静默跳过，也不是让它永久红）。
#: 不依赖真库的 `_collapse` 用例照常跑。
_REAL_DB_ENTITY = "示例知识库"

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "index", "sessions.db")


def _real_db_ready() -> bool:
    """真库可用 = 实体表里有这个实体 **且** 库里真的有条目。

    2026-11-20 修：原来只判断 `实体 in ENTITIES`。测试夹具也提供这个实体之后，
    空库/不存在的库会被当成"真库"，四个用例从 skip 变成 sqlite3.OperationalError。
    判据要落在**数据**上，不是落在"名字存在"上。
    """
    if _REAL_DB_ENTITY not in ENTITIES or not os.path.isfile(DB):
        return False
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
        try:
            return con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] > 0
        finally:
            con.close()
    except sqlite3.Error:
        return False


_HAS_REAL_DATA = _real_db_ready()
needs_real_db = pytest.mark.skipif(
    not _HAS_REAL_DATA,
    reason="本机没有 %r 实体的真实会话（换机后正常）；真库类用例跳过，"
           "_collapse 等纯逻辑用例仍然有效" % _REAL_DB_ENTITY)


def _items(*paths, verdict="dead_with_successor", when="2026-10-28"):
    return [{"verdict": verdict, "paths": [p], "when": when, "note": "", "text": ""}
            for p in paths]


# ── 压缩：同目录多条路径 → 一条规则 ─────────────────────────────────────
def test_collapse_by_old_dir():
    live = {"E": ["f:\\new"]}
    items = _items("F:\\old\\a.py", "F:\\old\\b.py", "F:\\other\\c.py")
    rules = _collapse(items, live, "E")
    assert set(rules) == {"f:\\old", "f:\\other"}
    assert rules["f:\\old"]["n"] == 2


def test_collapse_skips_container_dirs():
    """`c:\\users` 是容器不是项目，不能变成「已迁移」规则。"""
    live = {"E": ["f:\\new"]}
    rules = _collapse(_items("C:\\Users\\xmtx\\.codex\\x.json"), live, "E")
    assert rules == {}


def test_collapse_whole_dir_ref_not_counted_as_resolved():
    """整目录引用（`F:\\old` 后无子路径）不能算"可解析"——那会让提示说"相对路径多不变"。"""
    live = {"E": ["f:\\new"]}
    rules = _collapse(_items("F:\\old"), live, "E")
    assert rules["f:\\old"]["resolved"] == 0


# ── 失效清单：只能说"真的没了"的路径 ───────────────────────────────────
def test_dead_render_skips_container_dirs():
    """容器不是项目：`c:\\users` 不能作为「已不存在」的结论出现。"""
    items = _items("C:\\Users\\xmtx\\.codex\\gone.json", verdict="dead_no_successor")
    assert _render_dead(items) == []


def test_dead_render_skips_paths_that_still_exist(monkeypatch):
    """路径还在就不能说"已不存在"（同一条结论里没死的那条也不能报）。"""
    monkeypatch.setattr("inject.os.path.exists", lambda p: True)
    items = _items("F:\\gone-proj-fake\\x.py", verdict="dead_no_successor")
    assert _render_dead(items) == []


def test_dead_render_keeps_really_missing_project():
    items = _items("F:\\gone-proj-fake\\x.py", verdict="dead_no_successor")
    lines = _render_dead(items)
    assert len(lines) == 1 and "gone-proj-fake" in lines[0]


# ── 后继选择：要按证据，不能按顺序/字母序（两次踩坑） ──────────────────
def test_per_rule_successor_picks_dir_where_files_exist(tmp_path):
    """谁能真正解析出文件谁就是后继。

    实测两次坑：① 字母序选到 `f:\\kbwk-demo`；② "最近使用"选到 `f:\\示例知识库`
    （那是跑采集的会话 cwd，天然最新），正确答案 `f:\\demo-kb` 被挤掉。
    """
    d_has = tmp_path / "has"
    d_no = tmp_path / "no"
    (d_has / "server" / "api").mkdir(parents=True)
    (d_has / "server" / "api" / "wiki.py").write_text("x", encoding="utf-8")
    d_no.mkdir()
    old = "F:\\old-proj-fake"           # 不能用 tmp_path：它在 C:\Users 下会被容器规则过滤
    live = {"E": [str(d_no), str(d_has)]}      # 故意把"没有的"排在前面
    rules = _collapse(_items(old + "\\server\\api\\wiki.py"), live, "E")
    rule = list(rules.values())[0]
    assert rule["succ"] == str(d_has), rule
    assert rule["evidence"] > 0


def test_no_evidence_rule_points_nowhere():
    """一条都解析不出来时不能瞎指：succ 留空。"""
    old = "F:\\nonexistent-proj-xyz"
    rules = _collapse(_items(old + "\\server\\api\\wiki.py"), {"E": ["f:\\something"]}, "E")
    rule = list(rules.values())[0]
    assert rule["succ"] == "", rule
    assert rule["evidence"] == 0


# ── 真实库上的注入块 ─────────────────────────────────────────────────────
@needs_real_db
def test_injection_shape_and_budget():
    r = build_injection(DB, "示例知识库", max_chars=1400)
    t = r["text"]
    assert MARKER in t and "check_at=" in t
    assert "→" in t, "必须有旧目录→新目录的换算表"
    assert len(t) <= 1400, "必须遵守字符预算"
    assert "c:\\users" not in t.lower(), "容器目录不能出现在规则表里"


@needs_real_db
def test_injection_points_to_right_successor():
    """后继必须是「最近还在用的」目录（f:\\demo-kb），不是字母序第一个。"""
    r = build_injection(DB, "示例知识库")
    line = [ln for ln in r["text"].splitlines() if "demo-wiki" in ln and "→" in ln]
    assert line, r["text"]
    assert "demo-kb" in line[0], line[0]


@needs_real_db
def test_injection_is_much_smaller_than_item_count():
    """40 条迁移条目必须压成个位数规则（否则注入就是灌上下文）。"""
    r = build_injection(DB, "示例知识库")
    assert r["stats"]["migrate_items"] >= 10
    assert r["stats"]["rules"] <= 5, r["stats"]
    assert r["stats"]["est_tokens"] <= 500, r["stats"]


@needs_real_db
def test_dead_section_present():
    r = build_injection(DB, "示例知识库")
    assert "已确认失效" in r["text"]
    assert "勿照做" in r["text"]


def test_freshness_survives_block_prepended(tmp_path):
    """MARKER 不在文件最开头时也要能查到 check_at。

    2026-11-17 实测踩坑：procedure 块被拼在环境事实之前，300+ 字符把它挤出
    `fh.read(400)` 的窗口，`inject --check` 误报"文件头没有 check_at"。
    """
    from inject import MARKER, freshness
    p = tmp_path / "ctx.md"
    p.write_text("# 前缀块\n\n" + ("- 常用：`x`\n" * 40) + "\n"
                 "%s  check_at=2026-11-17T10:00:00  stale_after_days=3 -->\n"
                 % MARKER, encoding="utf-8")
    f = freshness(str(p), 3, now="2026-11-17T12:00:00")
    assert f["exists"] and not f["stale"], f
    assert f["check_at"] == "2026-11-17T10:00:00", f


# ── 措辞：目录还在 ≠ 目录不存在（2026-11-17 实测误报）────────────────────
def test_existing_dir_not_reported_as_missing(monkeypatch, tmp_path):
    """目录本身还在、只是里面的文件不在原位时，不能说"已不存在"。

    实测踩坑：`f:\\agent-memory` 目录明明存在，却被渲染成
    「`f:\agent-memory` 已不存在」——这是注入里的主动误导（agent 会以为整个项目没了）。

    2026-11-20 修（两个坑，都是"换运行环境就红"）：
    ① 原来用**仓库根目录 + `docs\\research\\gone.md`**：私有仓里 `docs/research` 恰好存在，
       导出树（不导 docs/）里不存在 ⇒ 措辞变成"文件不在原位"的另一支。
       改用 `tests/` 子目录 —— 本测试就住在里面，任何检出都有。
    ② `check_at` 原先写死 2026-11-17，超过 `stale_after_days` 后语义变化；现在用当前时间。
    另注：`_collapse` 过滤临时目录（噪音表），所以 **不能**用 `tmp_path` 当"项目目录"。
    """
    import datetime
    import inject as I
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    alive_dir = os.path.join(repo, "tests")
    fake = {"items": [{"verdict": "dead_with_successor",
                       "paths": [alive_dir + "\\gone.md"],
                       "when": "2026-11-17", "note": "", "text": ""}],
            "live_dirs": {"E": ["f:\\some-other-live-dir"]},
            "check_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    monkeypatch.setattr(I, "check_conclusions", lambda db, entity, **kw: fake)
    r = I.build_injection(DB, "E", max_chars=4000)
    text = r["text"]
    assert "gone.md" in text, text
    # 判据：说的是"目录还在、文件不在原位"，而不是"已不存在"。
    # 不要把断言绑在某一行上 —— 同一段里既有规则行也有"新位置没找到"行（2026-11-20 改）。
    assert "目录还在" in text, text
    assert "已不存在" not in text, text
