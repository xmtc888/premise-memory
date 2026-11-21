# -*- coding: utf-8 -*-
"""前提校验契约：三路判定 + 五类抽取假阳性必须被挡。

## 为什么这些测试是"防止自己骗自己"

真 🔴 的数量从 58 一路收敛到 2，**每一次缩减都是修判据，不是放宽阈值**：
  58 →(迁移重指向) 40 迁移 + 14 →(系统/临时目录不算资产、别名回退) 6
     →(markdown 链接截断、通配符) 4 →(GBK 乱码) 3 →(乱码被判成"新实体") 2
每一条假阳性都会让"仍有效的经验"被误标失效——那与本项目目标相反。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from premises import (ALIVE, DEAD_NO_SUCCESSOR, DEAD_WITH_SUCCESSOR, UNVERIFIED,  # noqa: E402
                      extract_paths, is_project_asset, judge_path, project_dir_of)


# ── 抽取：五类假阳性 ─────────────────────────────────────────────────────
def test_markdown_link_only_takes_target():
    """`[x.md](F:\\DP\\docs\\x.md)（含 15 行）` 不能抽出带 `)（含` 的脏路径。"""
    got = extract_paths("结论落档在 [x.md](F:\\DP\\docs\\x.md)（含 15 行验证状态表）。")
    assert got == ["F:\\DP\\docs\\x.md"], got


def test_wildcard_path_skipped():
    """`F:\\DP\\work\\waves\\int-0*.json` 是通配符，不是实体路径。"""
    assert extract_paths("改成全路径即可（F:\\DP\\work\\waves\\int-0*.json 就没被判）") == []


def test_cjk_punctuation_cuts():
    assert extract_paths("备份在 F:\\demo-wiki\\backups，随时可回退") == ["F:\\demo-wiki\\backups"]


def test_line_number_tail_stripped():
    assert extract_paths("根因在 F:\\kbnx-demo\\server\\api\\wiki.py#L29 的正则") \
        == ["F:\\kbnx-demo\\server\\api\\wiki.py"]


def test_project_dir_takes_two_segments():
    assert project_dir_of("F:\\demo-wiki\\server\\api\\wiki.py") == "f:\\demo-wiki"


# ── 非项目资产 ───────────────────────────────────────────────────────────
def test_system_and_temp_are_not_assets():
    assert not is_project_asset("C:\\Windows\\evil.txt")        # 路径穿越测试载荷
    assert not is_project_asset("C:\\Users\\xmtx\\AppData\\Local\\Temp\\probe.py")
    assert is_project_asset("F:\\demo-kb\\server\\searcher.py")


# ── 三路判定 ─────────────────────────────────────────────────────────────
LIVE = {"示例知识库": ["f:\\demo-kb", "f:\\kbnx-demo"]}
D2E = {"f:\\demo-kb": "示例知识库", "f:\\kbnx-demo": "示例知识库"}


def test_alive_when_exists():
    """已存在的**项目资产**判 alive。

    注意不能用 pytest 的 `tmp_path`：它在 `%TEMP%` 下，会被"非项目资产"规则挡成 unverified
    （这条本身是正确行为，测试写错会误判成 bug）。
    """
    p = os.path.abspath(__file__)          # F:\agent-memory\tests\...
    assert os.path.exists(p)
    assert judge_path(p, LIVE, D2E)[0] == ALIVE


def test_dead_with_successor_for_migrated_project():
    """项目迁移（`F:\\demo-wiki` → `F:\\demo-kb`）：判"迁移"，**不能**判失效。

    这是 GLM 评审 E1 指出的根本性错误：迁移意味着前提换名活着。
    """
    v, note = judge_path("F:\\demo-wiki\\server\\api\\wiki.py", LIVE, D2E)
    assert v == DEAD_WITH_SUCCESSOR
    assert "迁移" in note


def test_dead_no_successor_for_vanished_ascii_project():
    v, _n = judge_path("F:\\CAD2025\\setup.exe", LIVE, D2E)
    assert v == DEAD_NO_SUCCESSOR


def test_mojibake_path_is_unverified_not_dead():
    """GBK 乱码伪路径不能判失效（那会话本身在讨论编码问题）。"""
    v, note = judge_path("F:\\閿欒鎺掓煡\\work", LIVE, D2E)
    assert v == UNVERIFIED and "乱码" in note


def test_non_asset_path_is_unverified():
    assert judge_path("C:\\Windows\\evil.txt", LIVE, D2E)[0] == UNVERIFIED


def test_ports_not_judged_as_stale():
    """端口无监听测的是"存活"不是"存在"——评审明确不进失效判定（本函数不管端口）。"""
    from premises import extract_ports
    assert extract_ports("服务在端口 11434 上跑") == ["11434"]


# ── 认证四件套（2026-11-17，对齐 Many Steps into One 的证书模型）─────────
def test_check_conclusions_items_carry_certification():
    """每条非-alive 结论都必须带 certified_at / basis / recheck。

    依据 Many Steps into One（arXiv 2610.17544）：计数不是变更检测器，
    安全做法是"认证 + 事实变了重启证书"。证书缺"依据/失效条件"就退化成无据断言。
    """
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "index", "sessions.db")
    if not os.path.exists(db):
        import pytest
        pytest.skip("本机无索引库")
    from premises import check_conclusions
    r = check_conclusions(db, "示例知识库")
    if not r["items"]:
        import pytest
        pytest.skip("该实体当前没有非-alive 结论")
    for it in r["items"]:
        assert it.get("certified_at"), it
        assert it.get("basis"), it
        assert "作废" in (it.get("recheck") or ""), it
