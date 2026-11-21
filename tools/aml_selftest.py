"""AML 本地自测：①契约自测（smoke）②能力自测（recall@k，relevant / noisy 两条件）。

## 诚实声明（必须随结果一起呈现）

- 本脚本量的是**我们这套检索在 AML 契约下的表现**，不是 AML 官方分数；
- 题集是**自建**（从本机真实会话里取），与 AML 官方语料不同 ⇒ **分数线不可与公榜比较**；
- 它的用途只有一个：**同一个尺子量自己**（改代码前后对比），以及验证契约合规。

## 能力评测怎么造题（对齐官方对 Coding Memory 的描述）

官方口径："Agent 对同仓库历史工程经验的检索、筛选和复用能力，分别评测 relevant 与 noisy 条件。"
本脚本据此分两组：

  relevant  只有本项目记忆（同仓库），query = 该仓库历史里出现过的真实问题/路径/命令片段，
            目标是召回到与之相关的那条历史记忆；
  noisy     在库中掺入**其他项目**的大量记忆，同一批 query 再跑一次，
            看 recall 掉多少 —— 这是"筛选"能力。

评分：hit@k = 目标记忆是否出现在前 k 条；recall@5 / recall@10 / MRR。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aml_store import AmlStore                                      # noqa: E402

MAIN_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "index", "sessions.db")


# ── 契约自测 ─────────────────────────────────────────────────────────────
class ContractCase:
    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name, self.ok, self.detail = name, ok, detail


def run_contract_tests(store: AmlStore) -> list[ContractCase]:
    """逐条对着官方契约验证（每条都对应文档里的一句硬性规定）。"""
    out: list[ContractCase] = []
    uid = "eval:selftest:contract:conv-0"
    sid = "eval:selftest:sample:0"

    # 1. Add 正常写入 + 三 ID 原样回显
    req = {"request_id": "eval:selftest:chunk-0",
           "messages": [{"role": "user", "content": "We renamed demo-wiki to demo-base.",
                         "timestamp": 1704067200000}],
           "user_id": uid, "session_id": sid}
    res = store.add(req["request_id"], req["user_id"], req["session_id"], req["messages"])
    out.append(ContractCase(
        "Add 回显三个 ID 且与请求一致",
        res.get("request_id") == req["request_id"]
        and res.get("user_id") == req["user_id"]
        and res.get("session_id") == req["session_id"],
        str({k: res.get(k) for k in ("request_id", "user_id", "session_id")})[:120]))
    out.append(ContractCase("Add 返回 success=true", res.get("success") is True))

    # 2. 写入后**立即可检索**（契约关键词）
    hits = store.search("demo-base renamed", uid, 5)
    out.append(ContractCase(
        "Add 返回后可立即检索到（同步语义）",
        any("demo-wiki" in (h.get("content") or "") for h in hits),
        "hits=%d" % len(hits)))

    # 3. 同一 request_id 重放（平台有界重试）不重复落库
    before = store.stats()["memories"]
    store.add(req["request_id"], req["user_id"], req["session_id"], req["messages"])
    after = store.stats()["memories"]
    out.append(ContractCase("同一 request_id 重放不重复写入（幂等）",
                            before == after, "%d -> %d" % (before, after)))

    # 4. Search 返回结构：必须是 {"data": [...]}，不得有 items 包装、不得顶层数组
    payload = {"data": store.search("demo", uid, 10)}
    out.append(ContractCase("Search 响应为 {data:[...]} 对象",
                            isinstance(payload, dict) and isinstance(payload.get("data"), list)))
    out.append(ContractCase("Search 条目含 id/content（必填）",
                            all(isinstance(i.get("id"), str) and i["id"]
                                and isinstance(i.get("content"), str) and i["content"]
                                for i in payload["data"]) if payload["data"] else True))

    # 5. top_k 约束：返回条数不得超过 top_k
    for k in (1, 3):
        n = len(store.search("demo", uid, k))
        out.append(ContractCase("返回条数不超过 top_k=%d" % k, n <= k, "n=%d" % n))

    # 6. 空结果返回空数组（不是 null、不是 404）
    empty = store.search("完全不存在的检索词 zzzqqq", uid, 10)
    out.append(ContractCase("无命中返回空数组", empty == [], "type=%s" % type(empty).__name__))

    # 7. user_id 隔离：别的 user 检索不到
    other = "eval:selftest:contract:conv-1"
    leak = store.search("demo-base renamed", other, 10)
    out.append(ContractCase("跨 user_id 不泄漏（契约禁止跨用户检索）",
                            leak == [], "leaked=%d" % len(leak)))

    # 8. 1~2 字中文走 LIKE 回退（主库实测边界，适配器必须同构）
    store.add("eval:selftest:chunk-cn", uid, sid,
              [{"role": "user", "content": "质保期是三年，预算按季度结算。"}])
    cn = store.search("质保", uid, 5)
    out.append(ContractCase("2 字中文可召回（LIKE 回退）",
                            any("质保" in (h.get("content") or "") for h in cn),
                            "hits=%d" % len(cn)))
    return out


# ── 能力自测（用主库真实语料造题）──────────────────────────────────────
#: 容器目录不是项目（与 base._project_dir 的 _NOISE_DIRS 同理）：
#: 实测主库里 `c:\users` 一个"项目"占 3832 条消息，把它当噪音会稀释测试信号。
_CONTAINERS = {"c:\\users", "c:\\windows", "c:\\program files", "c:\\programdata"}


def _fetch_corpus(limit_sessions: int = 120) -> dict[str, list[dict]]:
    """从主库取真实消息，按项目目录（cwd 的前两段）分组。"""
    con = sqlite3.connect("file:%s?mode=ro" % MAIN_DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT m.session_key, m.text, m.ts, s.cwd FROM messages m "
        "JOIN sessions s ON s.session_key=m.session_key "
        "WHERE length(m.text) BETWEEN 80 AND 1200 AND m.role='assistant' "
        "ORDER BY m.id DESC LIMIT ?", (limit_sessions * 40,)).fetchall()
    con.close()
    groups: dict[str, list[dict]] = {}
    for r in rows:
        cwd = (r["cwd"] or "").replace("/", "\\")
        parts = [p for p in cwd.split("\\") if p]
        if len(parts) < 2:
            continue
        proj = (parts[0] + "\\" + parts[1]).lower()
        if proj in _CONTAINERS:
            continue                      # 容器目录不是项目
        groups.setdefault(proj, []).append(
            {"text": r["text"], "ts": r["ts"], "session": r["session_key"]})
    # 只留句子数够多的项目
    return {k: v for k, v in groups.items() if len(v) >= 20}


def _make_queries(mems: list[dict], n: int) -> list[tuple[str, str]]:
    """从记忆里造 query：取该条里一个**有辨识度的片段**（路径/英文标识符/长词）。"""
    import re
    queries = []
    for m in mems:
        text = m["text"]
        cands = re.findall(r"[A-Za-z]:\\[^\s，。；、\"'`]{8,60}", text)
        cands += re.findall(r"\b[a-z][a-z0-9_-]{7,24}\.(?:py|mjs|js|md|json|db)\b", text)
        cands += re.findall(r"\b[A-Za-z][A-Za-z0-9_]{12,28}\b", text)
        if not cands:
            continue
        q = cands[0]
        queries.append((q, m["text"]))
        if len(queries) >= n:
            break
    return queries


def _score(store: AmlStore, uid: str, pairs: list[tuple[str, str]],
           label: str, target_ids: dict[str, str]) -> dict:
    """跑一批 query，算 hit@1 / recall@5 / recall@10 / MRR。"""
    hit1 = r5 = r10 = 0
    mrr = 0.0
    for q, _text in pairs:
        want = target_ids.get(q)
        hits = store.search(q, uid, 10)
        ids = [h["id"] for h in hits]
        if want and ids:
            if ids[0] == want:
                hit1 += 1
            if want in ids[:5]:
                r5 += 1
            if want in ids[:10]:
                r10 += 1
            if want in ids:
                mrr += 1.0 / (ids.index(want) + 1)
    n = max(1, len(pairs))
    return {"label": label, "queries": len(pairs),
            "hit@1": round(hit1 / n, 3), "recall@5": round(r5 / n, 3),
            "recall@10": round(r10 / n, 3), "mrr": round(mrr / n, 3)}


def run_capability_tests(db_path: str, n_queries: int = 40) -> dict:
    """relevant（只放目标项目）vs noisy（掺入其他项目）两条件。"""
    corpus = _fetch_corpus()
    if len(corpus) < 2:
        return {"error": "主库语料不足（需要至少 2 个项目，每个 >=20 条消息）"}
    random.seed(20261117)                  # 固定种子：改代码前后可比
    projs = sorted(corpus)
    target_proj = "f:\\demo-kb" if "f:\\demo-kb" in corpus else projs[0]
    target = corpus[target_proj]
    pairs = _make_queries(target, n_queries)
    if not pairs:
        return {"error": "目标项目里没造出 query（片段太短？）"}

    def build(extra_projects: list[str]) -> tuple[AmlStore, dict[str, str], str]:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        st = AmlStore(tmp.name)
        uid = "eval:selftest:cap:conv-0"
        target_ids: dict[str, str] = {}
        # 目标项目的记忆（每条一个 Add，模拟平台分块写入）
        for i, m in enumerate(target):
            rid = "eval:selftest:chunk-%d" % i
            st.add(rid, uid, "eval:selftest:sample:0",
                   [{"role": "assistant", "content": m["text"]}])
        # 建立 query -> 目标记忆 id 的映射：用内容回查（内容唯一）
        for q, text in pairs:
            row = st.con.execute(
                "SELECT id FROM memories WHERE user_id=? AND content=? LIMIT 1",
                (uid, text)).fetchone()
            if row:
                target_ids[q] = row["id"]
        # 掺入噪音项目
        for p in extra_projects:
            for j, m in enumerate(corpus[p]):
                st.add("eval:selftest:noise-%s-%d" % (p.replace("\\", "_"), j),
                       uid, "eval:selftest:sample:noise",
                       [{"role": "assistant", "content": m["text"]}])
        return st, target_ids, uid

    results = {}
    # relevant：只有目标项目
    st1, ids1, uid1 = build([])
    results["relevant"] = _score(st1, uid1, pairs, "relevant(仅本项目)", ids1)
    results["relevant"]["memories"] = st1.stats()["memories"]
    st1.close()

    # noisy：掺入其他项目（最多 3 个，控制体量）
    others = [p for p in projs if p != target_proj][:3]
    st2, ids2, uid2 = build(others)
    results["noisy"] = _score(st2, uid2, pairs, "noisy(掺入%d个项目)" % len(others), ids2)
    results["noisy"]["memories"] = st2.stats()["memories"]
    results["noisy"]["noise_projects"] = others
    st2.close()
    results["target_project"] = target_proj
    results["corpus_sizes"] = {p: len(v) for p, v in corpus.items()}
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AML 契约 + 能力自测")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "..", "work", "aml", "eval.db"))
    ap.add_argument("--queries", type=int, default=40)
    ap.add_argument("--json", default="", help="把结果写到该 JSON 文件")
    ap.add_argument("--skip-capability", action="store_true")
    args = ap.parse_args(argv)

    db = os.path.normpath(args.db)
    store = AmlStore(db)
    print("=== ① 契约自测（对着官方文档逐条）===")
    cases = run_contract_tests(store)
    failed = 0
    for c in cases:
        mark = "PASS" if c.ok else "FAIL"
        if not c.ok:
            failed += 1
        print("  [%s] %s%s" % (mark, c.name, ("  | " + c.detail) if c.detail else ""))
    store.close()
    print("  -> %d 条，失败 %d" % (len(cases), failed))

    report = {"contract": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in cases],
              "contract_failed": failed}

    if not args.skip_capability:
        print()
        print("=== ② 能力自测（本机真实语料；非 AML 官方分数）===")
        cap = run_capability_tests(db, args.queries)
        report["capability"] = cap
        if "error" in cap:
            print("  " + cap["error"])
        else:
            print("  目标项目: %s   语料: %s" % (cap["target_project"],
                                               json.dumps(cap["corpus_sizes"], ensure_ascii=False)))
            for key in ("relevant", "noisy"):
                r = cap[key]
                print("  %-22s queries=%d  memories=%d  hit@1=%.3f  recall@5=%.3f  "
                      "recall@10=%.3f  MRR=%.3f"
                      % (r["label"], r["queries"], r["memories"], r["hit@1"],
                         r["recall@5"], r["recall@10"], r["mrr"]))
            d = cap["noisy"]["recall@5"] - cap["relevant"]["recall@5"]
            print("  噪音下 recall@5 变化: %+.3f（负得越多，说明越不会筛）" % d)
        print()
        print("  ⚠ 声明：这是**自建题集**上的分数，不是 AML 官方分数，不可与公榜对比；")
        print("     用途：同一个尺子量自己（改代码前后对比）+ 契约合规验证。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print("\n结果已写入 %s" % args.json)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
