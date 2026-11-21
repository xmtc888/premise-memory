"""注入块生成：把 42 条判定压成「可粘贴进会话开头的几行环境事实」。

## 为什么必须压缩

40 条 🟡 里一个目录（如 `F:\\demo-wiki`）就占了十几条（不同文件路径）。
按条目逐条注入 = 40 行 ≈ 800+ token，而**真正的信息只有 3 条规则**（旧目录→新目录）。
压缩后约 6 行，信息量不减。

## 注入块的三条纪律

1. **只放可执行的事实**（旧目录→新目录、失效路径），不放观点；
2. **必须带后继的正确路径**（按最近使用排序，实测按字母序会把 `f:\\demo-wiki2` 当前者，误导）；
3. **必须带 check_at**（存在性是检验时刻的函数，注入时可能已变）。
"""
from __future__ import annotations

import collections
import os
import re
import sqlite3
import time

from premises import (DEAD_NO_SUCCESSOR, DEAD_WITH_SUCCESSOR, check_conclusions,
                      project_dir_of)

#: 注入块的稳定前缀（不含 check_at，便于下游按前缀识别/替换）
MARKER = "<!-- agent-memory:auto-generated"

#: 注入块多久算过期（天）。超过就在文件头自报"日更没在跑"，并在 `inject --check` / doctor 里 WARN。
#: 取 **3** 而不是 7：`refresh.ps1` 是**每日**任务，正常情况下 age 应 <1 天；
#: 连续 3 天没更新就说明任务断了（留一点周末/休假的余量）。
#: 依据（2026-11-09 实测）：某个接入本技能的项目的 context.md 停在 11-03，5.9 天没人发现，
#: 而这类事实（目录改名/失效路径）一旦过期会**直接误导 agent 按旧路径工作**
#: —— 本仓实测：沿旧路径要 16 次工具调用，读它对只要 2 次。
STALE_AFTER_DAYS = 3

#: 从文件头读 check_at 的正则（格式由 `build_injection` 写出）
_CHECK_AT_RE = re.compile(r"check_at=([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})")


def freshness(out_path: str, stale_days: int = None, now: str = "") -> dict:
    """检查某个注入块文件是否过期。

    返回 `{exists, check_at, age_days, stale, reason}`。
    `check_at` 是**检验时刻**（不是文件 mtime）：文件可能被复制、被 git 检出，mtime 会骗人。
    """
    if stale_days is None:
        stale_days = STALE_AFTER_DAYS
    res = {"exists": False, "check_at": "", "age_days": None,
           "stale": True, "reason": "", "path": out_path}
    if not out_path or not os.path.exists(out_path):
        res["reason"] = "文件不存在（还没生成过）"
        return res
    res["exists"] = True
    try:
        with open(out_path, encoding="utf-8", errors="replace") as fh:
            # 读多一点（2026-11-17 踩坑）：MARKER 必须在文件头附近，但 caller 可能
            # 在它前面拼了别的块（procedure / 卡片）。只读 400 字符时，一块 300+ 字符的
            # procedure 块就能把 check_at 挤出窗口，`inject --check` 会误报
            # "文件头没有 check_at"。读 4000 字符仍是**有界**读，不影响成本。
            head = fh.read(4000)
    except OSError as exc:
        res["reason"] = "读不了：%s" % exc
        return res
    m = _CHECK_AT_RE.search(head)
    if not m:
        res["reason"] = "文件头没有 check_at（不是 agent-memory 生成的？）"
        return res
    res["check_at"] = m.group(1)
    try:
        days = (_parse_dt(now) - _parse_dt(res["check_at"])).total_seconds() / 86400.0
    except ValueError:
        res["reason"] = "check_at 无法解析：%s" % res["check_at"]
        return res
    res["age_days"] = round(days, 1)
    res["stale"] = days > stale_days
    res["reason"] = ("已过期 %.1f 天（阈值 %d 天）—— 日更任务可能没在跑" % (days, stale_days)
                     if res["stale"] else "新鲜（%.1f 天）" % days)
    return res


def _parse_dt(s: str):
    import datetime
    if not s:
        return datetime.datetime.now()
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")

#: 容器目录：不是项目，出现在规则表里是噪音（实测 `c:\users → f:\demo-base` 这种荒谬规则）
_CONTAINERS = {"c:\\users", "c:\\program files", "c:\\programdata", "c:\\windows"}


def _collapse(items, live_dirs: dict[str, list[str]], entity: str):
    """把条目按「旧目录 → 新目录」合并成规则。"""
    rules = collections.defaultdict(lambda: {"n": 0, "paths": [], "resolved": 0,
                                             "unresolved": []})
    live = live_dirs.get(entity) or []
    for it in items:
        for p in it["paths"]:
            old = project_dir_of(p)
            # `c:\users` 这类容器不是项目（实测被当成"已迁移"混进规则表，纯噪音）
            if not old or old in _CONTAINERS:
                continue
            r = rules[old]
            r["n"] += 1
            if p not in r["paths"]:
                r["paths"].append(p)
            # 该路径能否在后继目录里解析出来；**整目录引用（rest 为空）不算解析成功**
            rest = p[len(old):].lstrip("\\")
            if rest and any(os.path.exists(os.path.join(d, rest)) for d in live):
                r["resolved"] += 1
            elif rest:
                if rest not in r["unresolved"]:
                    r["unresolved"].append(rest)

    # 每条规则单独选后继：**谁能真正解析出这些文件，谁就是后继**。
    # 踩过两次坑：① 按字母序选到 f:\demo-wiki2；② 按"最近使用"选到 f:\demo-kb
    # （那是跑采集的这个会话的 cwd，天然最新）。只有"有证据"的才对。
    for old, r in rules.items():
        best, best_score = (live[0] if live else ""), -1
        for d in live:
            score = 0
            for p in r["paths"]:
                rest = p[len(old):].lstrip("\\")
                if rest and os.path.exists(os.path.join(d, rest)):
                    score += 1
            if score > best_score:
                best, best_score = d, score
        r["dir"] = old
        # 一条都解析不出来时不要瞎指：留空，渲染时写"未定位到具体位置"
        r["succ"] = best if best_score > 0 else ""
        r["evidence"] = best_score
    return rules


def _render_dead(items) -> list[str]:
    """「已确认失效」清单：只报**现在真的不存在**的路径。

    ⚠️ 一条结论只要有一个路径没了就会被标 dead，同一句里的**其它路径可能还活着**。
    实测（2026-11-04，平台包干净目录实测）把 `C:\\Users\\xmtx\\.codex` 折成
    `c:\\users` 写进"勿照做"——而 `C:\\Users` 明明存在，这是主动误导。
    所以：路径本身还存在 → 不提；折出来的项目目录是容器或还存在 → 也不提。
    """
    seen = set()
    out = []
    for it in items:
        for p in it["paths"]:
            if os.path.exists(p):
                continue
            d = project_dir_of(p) or p
            if d in _CONTAINERS or os.path.exists(d) or d in seen:
                continue
            seen.add(d)
            out.append("- `%s` 已不存在（%s 的结论基于它，需重新确认）" % (d, it["when"]))
    return out


def build_injection(db_path: str, entity: str, max_chars: int = 1400,
                    stale_days: int = None) -> dict:
    """生成注入块。返回 {text, stats, rules}。"""
    if stale_days is None:
        stale_days = STALE_AFTER_DAYS
    r = check_conclusions(db_path, entity)
    items = r["items"]
    live = r["live_dirs"]
    check_at = r["check_at"]
    succ = (live.get(entity) or [""])[0]

    migrate = [x for x in items if x["verdict"] == DEAD_WITH_SUCCESSOR]
    dead = [x for x in items if x["verdict"] == DEAD_NO_SUCCESSOR]

    lines = ["%s  check_at=%s  stale_after_days=%d -->"
             % (MARKER, check_at, stale_days),
             "## 环境事实（agent-memory 自动生成，勿手改；check_at=%s）" % check_at,
             "",
             "> 今天距 check_at > %d 天 ⇒ 日更没在跑，先跑 refresh.ps1 再照此工作"
             "（过期事实比没有事实更危险）。" % stale_days,
             ""]

    rules = _collapse(migrate, live, entity)
    if rules:
        lines.append("### 目录已改名（历史结论里的旧路径按下表换算）")
        for old, info in sorted(rules.items(), key=lambda kv: -kv[1]["n"]):
            if info.get("succ"):
                hint = ("相对路径多数不变" if info["resolved"] >= info["n"] / 2
                        else "同名文件可能也改名，需确认")
                lines.append("- `%s` → `%s`（%d 条历史结论引用；%s）"
                             % (old, info["succ"], info["n"], hint))
            else:
                # 没有证据说明迁到哪：只报"旧目录没了 + 本项目现存哪些目录"，不瞎指。
                # ⚠️ 措辞要分两种（2026-11-17 实测踩到）：**目录本身还在**、只是里面的
                # 文件不在原位时，写"已不存在"就是主动误导（实测 `f:\agent-memory`
                # 目录明明存在，却被渲染成"已不存在"）。
                tail = ("现存目录：%s，具体位置需确认"
                        % "、".join("`%s`" % d for d in (live.get(entity) or [])[:3]))
                if os.path.isdir(old):
                    lines.append("- `%s` 目录还在，文件已不在原位（%d 条引用）；%s"
                                 % (old, info["n"], tail))
                else:
                    lines.append("- `%s` 已不存在（%d 条引用）；%s"
                                 % (old, info["n"], tail))
            if info["unresolved"]:
                hmm = info["unresolved"][:2]
                lines.append("  - 在新位置没找到：%s" % ", ".join("`%s`" % x for x in hmm))
        lines.append("")

    entries = _render_dead(dead)
    if entries:
        lines.append("### 已确认失效（勿照做）")
        lines.extend(entries)
        lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    # 超预算时优先砍"新位置没找到"的细节行，保留规则行
    if len(text) > max_chars:
        keep = [ln for ln in lines if not ln.startswith("  - 在新位置没找到")]
        text = "\n".join(keep).rstrip() + "\n"

    return {"text": text, "check_at": check_at,
            "stats": {"migrate_items": len(migrate), "dead_items": len(dead),
                      "rules": len(rules), "chars": len(text),
                      "est_tokens": round(len(text) / 1.6)},
            "rules": {k: v["n"] for k, v in rules.items()}}


#: 注入时卡片状态优先级（越小越先）。dead 默认不注入（它是"勿照做"的告警，见 --include-dead）
_STATUS_RANK = {"alive": 0, "unverified": 1, "stale": 2, "dead": 3}
_STATUS_LABEL = {"alive": "成立", "unverified": "未验证", "stale": "前提已变", "dead": "已被推翻"}


def build_cards_block(cards_path: str = "", topic: str = "", scope: str = "",
                      max_cards: int = 5, max_chars: int = 900,
                      include_dead: bool = False, skip_acked: bool = True,
                      ack_ttl_days: int = 0, ack_path: str = "",
                      include_ids: bool = True) -> dict:
    """从结论卡里挑出要注入的条目（2026-11-10，改进清单第 3 条）。

    ## 为什么要有"预算"

    注入是**每轮成本**：卡片越攒越多，无脑全塞会把上下文挤爆，而且噪声会盖掉信号。
    DSH 市场里做注入的插件（`dsh-memoir`「bounded Hot Memory」、`dsh-memento`「Bounded …」）
    都把"有界"写在定位里 —— 这不是优化，是正确性。

    ## 优先序（都可在输出里核对）

      1. `status`：alive > unverified > stale >(默认排除) dead
      2. 证据条数多的优先（可核验性高的先上）
      3. `updated_at` 新的优先

    注意：这里**没有**按"最近验证时间"排序 —— 因为 `cards validate --verify` 目前只报告、
    不回写 `verified_at`（见改进清单的后续项）。不假装有这个字段。

    ## 已确认的卡不再推（2026-11-19）

    `cards ack <id>` 记下的确认，在**内容指纹未变且未过 TTL** 时跳过注入
    （`skip_acked=False` 无视账本）。跳过数必须报出来（`acked_skipped`）——
    "静默少推"和"静默丢条目"一样不可接受。

    ## 行内带 id（2026-11-19，为「记忆影响披露」提供可机械核验的锚点）

    每条卡的行尾带 `c-xxxxxxxx`：模型引用时能精确写出是哪条，`cards influence`
    也能在 assistant 回复里机械匹配到 —— 披露如果只是"请自觉说明"，就只是口号。

    ## 两种预算都要报

      `max_cards`：条数上限；`max_chars`：字符上限。
      **截断必须说出来**（"另有 N 条未列出"），静默丢条目正是这套系统一直在防的事。
    """
    import cards as C

    rows, warns = C.load(cards_path)
    if topic:
        rows = [c for c in rows if (c.get("topic") or "") == topic]
    if scope:
        rows = [c for c in rows if C.scope_matches(c, scope)]
    if not include_dead:
        rows = [c for c in rows if (c.get("status") or "unverified") != "dead"]

    acked_skipped = 0
    ttl = ack_ttl_days or C.ACK_TTL_DAYS
    if skip_acked:
        try:
            skip = C.acked_ids(rows, ttl_days=ttl, cards_path=cards_path, ack_path=ack_path)
        except Exception:                               # noqa: BLE001
            skip = set()                                # 账本坏了按"没确认"处理，宁可多推
        if skip:
            before = len(rows)
            rows = [c for c in rows if c.get("id") not in skip]
            acked_skipped = before - len(rows)

    def rank(c):
        st = c.get("status") or "unverified"
        return (_STATUS_RANK.get(st, 9), -len(c.get("evidence") or []), c.get("updated_at") or "")

    rows = sorted(rows, key=lambda c: (rank(c)[0], rank(c)[1],
                                       _neg_ts(rank(c)[2])))
    total = len(rows)
    picked, used = [], 0
    bound_by = ""
    for c in rows:
        if len(picked) >= max_cards:
            bound_by = "cards"
            break
        st = c.get("status") or "unverified"
        line = "- [%s] %s" % (_STATUS_LABEL.get(st, st), (c.get("claim") or "").strip())
        scope_note = ",".join(C.card_scopes(c))
        cid = (c.get("id") or "") if include_ids else ""
        notes = []
        if scope_note:
            notes.append(scope_note)
            notes.append("证据 %d 条" % len(c.get("evidence") or []))
        if cid:
            notes.append(cid)               # 行尾 id：披露时能精确引用，且可被 cards influence 机械核验
        if notes:
            line += "（%s）" % "；".join(notes)
        if used + len(line) > max_chars:
            bound_by = "chars"          # 说清是哪条预算卡住的，否则用户会去调错参数
            break
        picked.append(line)
        used += len(line)

    return {
        "text": "\n".join(picked),
        "picked": len(picked),
        "candidates": total,
        "truncated": max(0, total - len(picked)),
        "bound_by": bound_by if len(picked) < total else "",
        "chars": used,
        "max_cards": max_cards,
        "max_chars": max_chars,
        "topic": topic, "scope": scope, "include_dead": include_dead,
        "acked_skipped": acked_skipped, "ack_ttl_days": ttl, "skip_acked": skip_acked,
        "include_ids": include_ids,
        "warnings": warns,
    }


def _neg_ts(s: str):
    """把时间戳转成"越小越新"的排序键（字母序取反即可，格式固定 YYYY-MM-DDTHH:MM:SS）。"""
    return "".join(chr(255 - ord(ch)) if ord(ch) < 255 else ch for ch in (s or ""))


def render_cards_block(block: dict) -> str:
    """把 build_cards_block 的结果渲染成注入文本（含标题与截断说明）。"""
    head = "### 相关结论（agent-memory 卡片；按 status→证据数→更新时间排序）"
    parts = [head, ""]
    if block["text"]:
        parts.append(block["text"])
    else:
        parts.append("（没有符合条件的卡片）")
    if block["truncated"]:
        hint = {"cards": "调大 `--max-cards` 或收窄 `--topic`",
                "chars": "调大 `--card-chars` 或收窄 `--topic`"}.get(
                    block.get("bound_by"), "收窄 `--topic`")
        parts.append("")
        parts.append("> 另有 **%d** 条未列出（候选 %d 条；卡住的是%s）：%s。"
                     % (block["truncated"], block["candidates"],
                        "**条数上限 %d 条**" % block["max_cards"]
                        if block.get("bound_by") == "cards"
                        else "**字符上限 %d 字符**" % block["max_chars"],
                        hint))
    if block.get("acked_skipped"):
        parts.append("")
        parts.append("> 另有 **%d** 条已确认（内容未变，%d 天内免打扰）："
                     "`cards ack --show` 查看，`--include-acked` 忽略。"
                     % (block["acked_skipped"], block.get("ack_ttl_days") or 0))
    return "\n".join(parts)


def main(argv=None):
    import sys
    argv = argv or sys.argv[1:]
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index", "sessions.db")
    entity = argv[0] if argv else "demo-kb"
    out = build_injection(db, entity)
    print(out["text"])
    print("--- 统计: %s" % out["stats"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
