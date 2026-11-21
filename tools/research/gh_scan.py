#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GitHub 同类项目「快扫」：发现 → 采数 → 事实抽取 → 与我方对比 → 跨期 delta。

## 为什么有它

2026-11-17 的生态调研和 2026-11-19 的两篇公众号对比，都是**手工**做的：
curl 逐个抓 JSON、人眼比 README、手写表。三次下来有两条教训值得固化：

1. **指标可以机械化**（stars/活跃度/客户端覆盖/关键词命中），人只该做"判定"；
2. **关键词命中 ≠ 事实**：MemoraX 的 README 里能 grep 到 training 相关词，
   但一手文档根本没有训练证据 —— 所以本脚本把所有关键词命中一律标 `(需核实)`，
   不许直接当结论（这是那次误判留下的规矩）。

## 两种用法

```powershell
# A. 发现模式：按查询词捞一批同类项目
python tools/research/gh_scan.py --query "topic:agent-memory" --query "agent memory in:name,description" --limit 30

# B. 点名模式：已知竞品，直接采集（公众号/评测里看到的项目）
python tools/research/gh_scan.py --repo memorax-ai/memorax-code --repo topmindspace/dsh-super-kanban

# 跨期 delta：给上次的快照，算 stars 增量
python tools/research/gh_scan.py --query "topic:agent-memory" --prev docs/research/repos/2026-11-17.json
```

产出（默认 `docs/research/repos/<日期>.json` + 同名 `.md`）：
- JSON 是**快照**，进库，供下次 `--prev` 算增速（跨会话可查，别只放 work/）；
- MD 是**给人看的表**：指标、活跃度、客户端交集、事实命中、需核实项。

## 纪律（脚本里已落实）

- README 抓取走 `raw.githubusercontent.com`，**不占 GitHub API 配额**（只有 search 占）；
- 无 token 也能跑：默认只发 1 次 search（10 次/分钟限制内），点名的 repo 走 `/repos` 逐个取；
- 有 token 时读 `GITHUB_TOKEN`（可选，5000/h），没有就按匿名跑；
- 事实命中一律带 `(需核实)` 标记 + 原文片段，**脚本不下结论**，判定留给人。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ═══════════ 我方能力清单（对比基线）═══════════
#: 本仓覆盖的 agent 来源（与 README 的六个采集器一致）。改这里等于改"重叠度"口径。
MY_CLIENTS = ["DSH", "Codex", "Claude Code", "Hermes", "WorkBuddy", "CodeBuddy"]
#: 我方能力关键词（用于"它有没有同类能力"的粗筛，全部输出为"需核实"）
MY_CAPS = {
    "前提失效": [r"stale", r"premise", r"invalidat", r"supersed", r"过期", r"失效"],
    "跨agent一份库": [r"across agents?", r"multi-?agent", r"shared memory layer", r"跨\s*agent"],
    "零模型": [r"no llm", r"zero-?llm", r"without (?:an? )?(?:model|llm)"],
    "本机存储": [r"local-?first", r"offline", r"sqlite", r"on-?device", r"本机"],
}

#: README 事实抽取规则。**全部输出都标"需核实"**——这是 2026-11-19 的教训：
#: MemoraX README 能 grep 到 training 词，但一手文档没有训练证据。
FACTS = {
    "客户端": [r"DeepSeek[\s-]*Harness", r"\bDSH\b", r"\bCodex\b", r"Claude Code",
              r"\bWorkBuddy\b", r"CodeBuddy", r"OpenCode", r"\bCursor\b", r"\bTrae\b",
              r"\bQoder\b", r"\bHermes\b"],
    "云/账号": [r"\bcloud\b", r"hosted", r"\baccount\b", r"\bsign[ -]?in\b", r"guest mode", r"云端", r"账号"],
    "训练/奖励": [r"\btrain(?:ing|ed)?\b", r"\breward\b", r"\bdataset\b", r"fine-?tun", r"训练", r"奖励"],
    "LLM/摘要": [r"\bLLM\b", r"summar(?:y|ies|ize)", r"embedding", r"vector", r"向量"],
    "前提失效": [r"\bstale\b", r"premise", r"invalidat", r"supersed", r"过期", r"失效"],
    "评测/榜单": [r"benchmark", r"\bAML\b", r"leaderboard", r"\beval\b", r"榜单"],
    "遥测/上传": [r"telemetry", r"upload", r"sync(?:ed|ing)? to", r"上传"],
}

UA = "agent-memory-gh-scan/1.0 (+local research tool)"
_API = "https://api.github.com"


# ═══════════ 抓取：urllib 优先，失败回退本机 curl（Windows schannel 实测需要）═══════════
def fetch(url: str, timeout: int = 25) -> str:
    """取文本。urllib 的 SSL 在部分 Windows 环境会挂在吊销检查上，故回退 curl。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        out = subprocess.run(
            ["curl.exe", "-sSL", "--ssl-no-revoke", "-A", UA, "-H", "Accept: application/vnd.github+json", url],
            capture_output=True, timeout=timeout + 10)
        if out.returncode != 0:
            raise RuntimeError("fetch 失败：%s（curl=%s）" % (url, out.returncode))
        return out.stdout.decode("utf-8", errors="replace")


def api(path: str, token: str = "") -> dict:
    url = _API + path
    if token:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Authorization": "Bearer " + token,
                                                  "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception:                               # noqa: BLE001
            pass
    return json.loads(fetch(url))


def readme_of(full_name: str) -> str:
    """README 走 raw，不占 API 配额；HEAD 免去猜分支名。取不到就返回空串（不报错）。"""
    for name in ("README.md", "README.zh.md", "readme.md", "README.rst"):
        try:
            return fetch("https://raw.githubusercontent.com/%s/HEAD/%s" % (full_name, name))
        except Exception:                               # noqa: BLE001
            continue
    return ""


# ═══════════ 事实抽取 ═══════════
def hits(text: str, patterns) -> list[dict]:
    """每种模式取首个**非否定**命中；只有全是否定句时才返回否定命中并标记。

    为什么必须做否定检测（实测踩到）：MemoraX 的 README 有
    "It does not upload the complete retained client trace artifact" —— 裸 grep `upload`
    会把这句话算成"它会上传"，方向正好相反。机械抽取的边界就在这里：能定位句子，
    不能替你判定语义，所以否定句要显式标出来，而不是当真。
    """
    out = []
    for pat in patterns:
        cands = []
        for m in re.finditer(pat, text, re.IGNORECASE):
            if len(cands) >= 5:
                break
            lo, hi = max(0, m.start() - 40), min(len(text), m.end() + 60)
            snippet = re.sub(r"\s+", " ", text[lo:hi]).strip()
            pre = text[max(0, m.start() - 30):m.start()].lower()
            negated = bool(re.search(
                r"\b(?:not|no|never|without|doesn'?t|does not|cannot|don'?t|isn'?t)\b", pre))
            cands.append({"pattern": pat, "snippet": snippet[:140], "negated": negated})
        if not cands:
            continue
        out.append(next((c for c in cands if not c["negated"]), cands[0]))
    return out


def scan_repo(item: dict, token: str = "", with_readme: bool = True) -> dict:
    full = item["full_name"]
    lic = (item.get("license") or {}).get("spdx_id") or ""
    rec = {
        "full_name": full,
        "url": item.get("html_url"),
        "desc": (item.get("description") or "")[:200],
        "stars": item.get("stargazers_count", 0),
        "forks": item.get("forks_count", 0),
        "open_issues": item.get("open_issues_count", 0),
        "created": (item.get("created_at") or "")[:10],
        "pushed": (item.get("pushed_at") or "")[:10],
        "license": lic,
        "archived": bool(item.get("archived")),
        "topics": (item.get("topics") or [])[:10],
        "language": item.get("language") or "",
    }
    if with_readme:
        text = readme_of(full)
        rec["readme_bytes"] = len(text)
        rec["facts"] = {k: hits(text, v) for k, v in FACTS.items()}
        # 客户端交集（机械可算，最有用的一列）
        found = []
        for c in MY_CLIENTS:
            pat = {"DSH": r"DeepSeek[\s-]*Harness|\bDSH\b", "Claude Code": r"Claude Code",
                   "Codex": r"\bCodex\b", "Hermes": r"\bHermes\b", "WorkBuddy": r"\bWorkBuddy\b",
                   "CodeBuddy": r"CodeBuddy"}[c]
            if re.search(pat, text, re.IGNORECASE):
                found.append(c)
        rec["overlap_clients"] = found
        rec["caps"] = {k: bool(hits(text, v)) for k, v in MY_CAPS.items()}
    return rec


def dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        fn = (it.get("full_name") or "").lower()
        if not fn or fn in seen:
            continue
        seen.add(fn)
        out.append(it)
    return out


# ═══════════ 输出 ═══════════
def _num(n) -> str:
    """千分位，不用 2.2k：对比表里 2164 与 2200 的差要看得出来。"""
    return "{:,}".format(int(n or 0))


def render_md(scan: dict, prev: dict | None) -> str:
    prev_items = {i["full_name"].lower(): i for i in (prev or {}).get("items", [])}
    rows = []
    for it in scan["items"]:
        old = prev_items.get(it["full_name"].lower())
        delta = ""
        if old is not None:
            d = it["stars"] - old.get("stars", 0)
            delta = ("+%d" % d) if d else "0"
        ov = it.get("overlap_clients") or []
        facts = it.get("facts") or {}

        def mark(key):
            """命中即标「需核实」；否定句单独标，不许当成事实。"""
            fs = facts.get(key) or []
            if not fs:
                return ""
            return "否定句?" if all(f.get("negated") for f in fs) else "命中"

        cloud = mark("云/账号")
        train = mark("训练/奖励")
        stale = mark("前提失效")
        caps = it.get("caps") or {}
        rows.append((it, delta, ",".join(ov) or "—", cloud, train, stale, len(ov)))

    rows.sort(key=lambda r: (-r[6], -r[0]["stars"]))
    lines = [
        "# GitHub 同类项目快扫 — %s" % scan["generated_at"],
        "",
        "> 口径：指标为 GitHub API 快照；**事实列只表示「README 里能 grep 到」，"
        "一律需人工核实**（2026-11-19 教训：MemoraX README 能命中 training 词，一手文档无训练证据；"
        "`does not upload` 这类否定句也标为「否定句?」）。",
        "> 我方基线：6 个来源 %s。" % " / ".join(MY_CLIENTS),
        "",
    ]
    if scan.get("queries"):
        lines += ["> 查询：" + " ｜ ".join("`%s`" % q for q in scan["queries"]), ""]
    lines += [
        "| 项目 | stars | Δ | pushed | 许可 | 客户端交集 | 云/账号 | 训练叙 | 前提失效 | 归档 |",
        "|---|--:|--:|---|---|---|---|---|---|---|",
    ]
    for it, delta, ov, cloud, train, stale, _n in rows:
        lines.append("| [%s](%s) | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            it["full_name"], it["url"], _num(it["stars"]), delta or "—", it["pushed"],
            it["license"] or "—", ov, cloud, train, stale, "是" if it["archived"] else ""))
    lines.append("")
    lines.append("## 能力粗筛（关键词命中，全部需人工核实）")
    lines.append("")
    lines.append("| 项目 | " + " | ".join(MY_CAPS) + " |")
    lines.append("|---|" + "---|" * len(MY_CAPS))
    for it, *_ in rows:
        caps = it.get("caps") or {}
        lines.append("| %s | %s |" % (it["full_name"], " | ".join(
            "命中" if caps.get(k) else "" for k in MY_CAPS)))
    lines.append("")
    lines.append("## 需核实清单（命中原文片段 — 先读句子再判定）")
    lines.append("")
    for it, *_ in rows:
        facts = it.get("facts") or {}
        flagged = [k for k in ("训练/奖励", "云/账号", "遥测/上传", "前提失效") if facts.get(k)]
        if not flagged:
            continue
        lines.append("### %s" % it["full_name"])
        for k in flagged:
            f = facts[k][0]
            tag = "**(否定句，方向可能相反)** " if f.get("negated") else ""
            lines.append("- **%s** %s命中 `%s`：…%s…" % (k, tag, f["pattern"], f["snippet"]))
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="GitHub 同类项目快扫")
    ap.add_argument("--query", action="append", default=[],
                    help="搜索查询（可重复），如 'topic:agent-memory'")
    ap.add_argument("--repo", action="append", default=[],
                    help="点名仓库 owner/name（可重复）；已知竞品走这条，省一次 search")
    ap.add_argument("--limit", type=int, default=30, help="每个查询取多少条（默认 30，上限 100）")
    ap.add_argument("--out", default="", help="输出前缀（默认 docs/research/repos/<UTC 日期>）")
    ap.add_argument("--prev", default="", help="上次的快照 JSON，用于算 stars 增量")
    ap.add_argument("--no-readme", action="store_true", help="只取指标，不抓 README（更快，无事实列）")
    ap.add_argument("--only-overlap", type=int, default=0,
                    help="只保留与我方客户端交集 >= N 的项目（0=全留）")
    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    items: list[dict] = []
    if args.repo:
        for full in args.repo:
            try:
                items.append(api("/repos/" + full.strip(), token))
            except Exception as exc:                    # noqa: BLE001
                print("skip %s: %s" % (full, exc), file=sys.stderr)
    for q in args.query:
        try:
            d = api("/search/repositories?" + urllib.parse.urlencode(
                {"q": q, "sort": "stars", "order": "desc", "per_page": min(100, args.limit)}), token)
        except Exception as exc:                        # noqa: BLE001
            print("search 失败 %r: %s（无 token 时限 10 次/分钟）" % (q, exc), file=sys.stderr)
            continue
        items += d.get("items") or []
    items = dedupe(items)
    if not items:
        print("没有拿到任何仓库：检查查询词，或换个时间再试（匿名 API 限流）。", file=sys.stderr)
        return 2

    scans = [scan_repo(it, token, with_readme=not args.no_readme) for it in items[:200]]
    if args.only_overlap > 0:
        scans = [s for s in scans if len(s.get("overlap_clients") or []) >= args.only_overlap]

    prev = None
    if args.prev and os.path.exists(args.prev):
        prev = json.load(open(args.prev, encoding="utf-8"))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = args.out or os.path.join("docs", "research", "repos", stamp)
    os.makedirs(os.path.dirname(os.path.abspath(prefix)), exist_ok=True)
    scan = {"type": "gh-scan", "generated_at": datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"), "queries": args.query,
            "repos": args.repo, "baseline_clients": MY_CLIENTS, "items": scans}
    with open(prefix + ".json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(scan, f, ensure_ascii=False, indent=1, sort_keys=True)
    md = render_md(scan, prev)
    with open(prefix + ".md", "w", encoding="utf-8", newline="\n") as f:
        f.write(md)

    print("仓库 %d 个 → %s.json / %s.md" % (len(scans), prefix, prefix))
    for s in sorted(scans, key=lambda x: -len(x.get("overlap_clients") or []))[:8]:
        print("  %-42s stars=%-7s 交集=%d %s" % (
            s["full_name"], _num(s["stars"]), len(s.get("overlap_clients") or []),
            ",".join(s.get("overlap_clients") or [])))
    print("\n下一步（人工）：打开 .md，把「需核实」项逐条对一手材料核；判定写进"
          " docs/<日期>_对比<项目>.md，A/B/C 分级照 docs/2026-11-17_生态调研 的口径。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
