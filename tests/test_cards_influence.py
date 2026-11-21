# -*- coding: utf-8 -*-
"""记忆影响披露的契约（2026-11-19）。

披露有两半，缺一半就没意义：
- **注入侧**：卡片行尾必须带 `c-<sha8>`，否则模型无法精确说出"用了哪条"；
- **核验侧**：`store.id_mentions` 必须在 assistant 回复里机械匹配到同一串 id。

**口径是这套东西的要害**：重启后首次实跑 `cards influence`，第一版按"全文任意出现 id"
统计，把上轮**讨论这套机制本身**的回复也算成了引用（3 次假阳性，2026-11-19）。
所以 strict 默认只认**行首披露句式**；`strict=False` 保留任意提及口径，仅供排查。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cards as C  # noqa: E402
import inject as I  # noqa: E402
from store import Store  # noqa: E402


def _ev(n=1):
    return [{"agent": "dsh", "date": "2026-11-19", "quote": "摘录%d" % i} for i in range(n)]


@pytest.fixture()
def lib(tmp_path):
    p = str(tmp_path / "cards.jsonl")

    def add(claim, status="alive", topic="t"):
        c = C.build(claim, _ev(), status=status, topic=topic, scopes="machine")
        C.upsert(c, p)
        return c

    return p, str(tmp_path / "cards-ack.jsonl"), add


# ── 注入侧：id 进块 ─────────────────────────────────────────────────────
def test_card_line_carries_id_by_default(lib):
    p, a, add = lib
    card = add("带 id 的结论")
    b = I.build_cards_block(cards_path=p, ack_path=a, max_cards=10)
    assert card["id"] in b["text"], "行尾必须带 id，否则披露无法被机械核验"
    assert b["include_ids"] is True


def test_card_ids_can_be_disabled(lib):
    p, a, add = lib
    card = add("不要 id 的结论")
    b = I.build_cards_block(cards_path=p, ack_path=a, max_cards=10, include_ids=False)
    assert card["id"] not in b["text"]
    assert b["include_ids"] is False


def test_id_rendered_even_when_scope_empty(lib):
    """模拟历史数据：`scopes` 显式为空的卡（`validate` 会拒，但导入的旧数据可能带）。

    此时行里没有作用域括号，id 仍必须出现 —— 披露不能因为卡旧就失去锚点。
    """
    p, a, add = lib
    card = C.build("无作用域的结论", _ev(), status="alive", topic="t", scopes=None)
    card["scopes"] = []
    with open(p, "a", encoding="utf-8") as f:       # 绕过 validate，模拟旧数据
        f.write(json.dumps(card, ensure_ascii=False) + "\n")
    b = I.build_cards_block(cards_path=p, ack_path=a, max_cards=10)
    assert ("（%s）" % card["id"]) in b["text"], b["text"]


# ── 核验侧：id_mentions（strict 口径）──────────────────────────────────
@pytest.fixture()
def db(tmp_path):
    """六条消息，覆盖 strict 要区分的三种情形。"""
    s = Store(str(tmp_path / "t.db"))
    rows = [
        # ① 行首披露句式 ⇒ strict 计
        ("dsh:A", "assistant", "2026-11-19T10:00:00",
         "受 agent-memory 卡片 c-aaaa1111 影响：改了排序"),
        # ② 行首披露（带列表前缀/本次前缀）⇒ strict 计
        ("dsh:A", "assistant", "2026-11-19T11:00:00",
         "本次受 agent-memory 卡片 c-aaaa1111 影响（第二处）"),
        # ③ 讨论机制时顺带提到 ⇒ strict **不计**（第一版的假阳性就来自这种行）
        ("dsh:A", "assistant", "2026-11-18T09:00:00",
         "我之前在回复里提到过 c-aaaa1111 这套机制"),
        # ④ 非行首的顺带提到 ⇒ strict 不计
        ("dsh:B", "assistant", "2026-11-18T10:00:00",
         "较早的 c-aaaa1111 引用"),
        # ⑤ 用户消息里的 id ⇒ 两种口径都不计（只统计 assistant）
        ("dsh:B", "user", "2026-11-19T12:00:00",
         "用户消息里提到 c-aaaa1111 不算引用"),
        ("dsh:B", "assistant", "2026-11-19T13:00:00", "无关内容"),
    ]
    for i, (sk, role, ts, text) in enumerate(rows, 1):
        s.con.execute(
            "INSERT INTO messages(session_key, agent, role, ts, seq, text) VALUES(?,?,?,?,?,?)",
            (sk, "dsh", role, ts, i, text))
    s.con.commit()
    yield s
    s.close()


def test_strict_counts_only_disclosure_lines(db):
    st = db.id_mentions(["c-aaaa1111"])
    assert st["c-aaaa1111"]["count"] == 2, "只有行首披露句式算引用"


def test_strict_ignores_mechanism_discussion(db):
    """第一版假阳性的回归测试：讨论"提过 c-xxx"的回复不能算引用。"""
    st = db.id_mentions(["c-aaaa1111"])
    sessions = st["c-aaaa1111"]["sessions"]
    assert sessions == {"dsh:A": 2}, sessions
    assert st["c-aaaa1111"]["last_ts"].startswith("2026-11-19T11:00")


def test_loose_counts_any_mention_but_never_user_role(db):
    st = db.id_mentions(["c-aaaa1111"], strict=False)
    assert st["c-aaaa1111"]["count"] == 4, "任意提及：①②③④；⑤是 user 消息，仍不计"
    assert st["c-aaaa1111"]["sessions"] == {"dsh:A": 3, "dsh:B": 1}


def test_since_filters(db):
    st = db.id_mentions(["c-aaaa1111"], since="2026-11-19")
    assert st["c-aaaa1111"]["count"] == 2, "since 之前的披露不该计入"


def test_unknown_id_is_zero_not_missing(db):
    st = db.id_mentions(["c-aaaa1111", "c-deadbeef"])
    assert st["c-deadbeef"]["count"] == 0
    assert st["c-deadbeef"]["last_ts"] == ""


def test_empty_ids_is_empty_dict(db):
    assert db.id_mentions([]) == {}
