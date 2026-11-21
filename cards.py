# -*- coding: utf-8 -*-
"""结论卡（assertion cards）：把会话压成**可复用的断言**，而不是只留原始消息。

## 为什么要有它

`search` 只能返回消息片段。一条决策往往横跨几十条消息、两万 token，于是理解成本
全落在查询期——这和"传统 RAG 只切块不编译"是同一个病（见 docs/design.md 的定位）。
`docs/` 里的结论文档证明了"编译过的东西"检索起来有效得多，但 `docs/` 属于**某个项目**，
换台机器、换个项目就没了。

卡片补的就是这一层：**独立于任何项目目录**，存在本机索引库旁（`index/cards.jsonl`），
复制文件即可迁移。

## 一张卡长什么样

    {
      "id": "c-1a2b3c4d",                 # 内容派生，重复 add 不会重复入库
      "claim": "一句话可证伪断言",          # 必填
      "topic": "检索与排序",                # 可选，用于分组
      "status": "alive",                   # 必填：alive/stale/dead/unverified
      "scope": "适用语料/版本/条件",         # 可选但强烈建议（否则结论会被过度泛化）
      "evidence": [                        # 必填，至少 1 条
        {"agent": "dsh", "date": "2026-11-06",
         "quote": "原文摘录（≤200 字，必填）",
         "session": "dsh:session-…",       # 可选：本机可回查
         "message": "…"}                   # 可选
      ],
      "supersedes": ["c-…"],               # 本卡推翻了哪些
      "superseded_by": "c-…",              # 被谁推翻
      "created_at": "2026-11-08T…", "updated_at": "…"
    }

## 三条硬规矩

1. **证据必须带 `date` + `agent` + `quote`**：`session` 只在本机有效，换机后回查不了原文，
   所以摘录本身就是证据的**下限**。没有摘录的卡片等于不可核验，直接判为非法。
2. **状态是卡片的一部分**：`alive / stale / dead / unverified` 四态，对应
   🟢 仍成立 / 🟡 前提已变 / 🔴 已被推翻 / ⚪ 未验证。被推翻的卡**不删**，
   用 `supersede()` 连起来——"过去这么认为、现在不成立"本身就是最有价值的经验。
3. **文件坏了不许让检索变砖**：读失败 ⇒ 当成空表并返回 warnings，`search` 照常工作。

## 人工确认账本（2026-11-19）

注入是每轮成本，"同一张卡天天推同一句话"就是噪音。`cards ack <id>` 在
`cards-ack.jsonl` 里记下**这一张卡、在这个内容指纹上、被谁确认过**，
注入侧（`inject --cards`）默认跳过仍有效的确认。

三条语义，都不需要人手动清账：

- **内容指纹变了就自动重现**：`status` 被巡检改成 stale、证据增删、claim 改写 ⇒ 重新推；
- **超过 TTL（默认 30 天）自动重现**：避免"确认一次吃一辈子"；
- **账本跟随卡片文件同目录**：多库与测试天然隔离，不会污染真实 `index/`。

## 不做的事

- 不调用任何 LLM（总结由写卡的人/agent 负责——它上下文里有全部证据）；
- 不做向量检索（与主项目路线一致）；
- 不自动从会话里抽结论（规则抽取的产物我们已经在 `timeline` 里见过，噪音率高）。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time

#: 允许的状态值 → 显示用标签（不用 emoji：Windows GBK 控制台会炸）
STATUS = {
    "alive": "[alive]",
    "stale": "[stale]",
    "dead": "[dead]",
    "unverified": "[unverified]",
}
ALIASES = {
    "alive": "alive", "ok": "alive", "成立": "alive", "仍成立": "alive",
    "stale": "stale", "前提已变": "stale",
    "dead": "dead", "推翻": "dead", "已被推翻": "dead",
    "unverified": "unverified", "未验证": "unverified",
}
QUOTE_MAX = 200
CLAIM_MAX = 300
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def cards_file() -> str:
    try:
        from paths import CARDS_FILE
        return CARDS_FILE
    except Exception:                                   # noqa: BLE001
        return ""


# ═══════════ 读写 ═══════════
#: 审计日志的滚动阈值（行数）与触发大小（字节）
_AUDIT_MAX_LINES = 5000
_AUDIT_MAX_BYTES = 2 * 1024 * 1024


def audit_file() -> str:
    try:
        from paths import CARDS_AUDIT_FILE
        return CARDS_AUDIT_FILE
    except Exception:                                  # noqa: BLE001
        return ""


def dedupe_key(card: dict) -> str:
    """去重键：**规范化 claim** 的 sha8（去标点空白、小写）。

    为什么不用 `id`：`id` = sha8(claim+"||"+topic) —— 同一句断言挂到两个 topic 下会得到两个 id，
    而那正是最需要被发现的"重复登记"。用 claim 单独算键，才能把它们认成同一条。
    """
    return _sha8_text(_norm_claim(card.get("claim") or ""))


def _sha8_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


def find_duplicate_claims(cards: list[dict]) -> list[dict]:
    """找出"同一断言、多个 id"的重复组（只报告，不合并）。

    典型来源：同一结论先按 topic A 记一次，后来换 topic B 又记一次；或从两台机器导入。
    """
    groups: dict[str, list[dict]] = {}
    for c in cards:
        groups.setdefault(dedupe_key(c), []).append(c)
    out = []
    for key, items in groups.items():
        ids = {c.get("id") for c in items}
        if len(ids) > 1:
            out.append({"dedupe_key": key,
                        "cards": [{"id": c.get("id"), "topic": c.get("topic"),
                                   "status": c.get("status"),
                                   "claim": c.get("claim")} for c in items]})
    return out


def _sha8(obj) -> str:
    """卡片内容指纹（前 8 位）。用规范化 JSON 算，保证同一内容永远同哈希。"""
    try:
        blob = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = repr(obj)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def audit_by() -> str:
    """谁改的：优先 AGENT_NAME（多 agent 协作时由调用方注入），否则用户名。"""
    return (os.environ.get("AGENT_NAME") or os.environ.get("USERNAME")
            or os.environ.get("USER") or "unknown")


def append_audit(op: str, card_id: str = "", before=None, after=None,
                 note: str = "", by: str = "", path: str = "") -> bool:
    """追加一条审计记录。**失败绝不抛异常**（审计是副业，写卡才是业务）。

    返回是否写成功，仅用于诊断。
    """
    p = path or audit_file()
    if not p:
        return False
    rec = {"at": _now(), "op": op, "card_id": card_id or "",
           "by": by or audit_by(),
           "before": _sha8(before) if before is not None else "",
           "after": _sha8(after) if after is not None else "",
           "note": (note or "")[:200]}
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with io.open(p, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        _trim_audit(p)
        return True
    except OSError:
        return False


def _trim_audit(p: str) -> None:
    """文件过大时只留最后 `_AUDIT_MAX_LINES` 行（append-only 也要有界）。"""
    try:
        if os.path.getsize(p) <= _AUDIT_MAX_BYTES:
            return
        with io.open(p, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        keep = lines[-_AUDIT_MAX_LINES:]
        tmp = p + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(keep) + "\n")
        os.replace(tmp, p)
    except OSError:
        pass


def read_audit(card_id: str = "", limit: int = 50, path: str = "") -> list[dict]:
    """读审计记录（**新 → 旧**）。坏行跳过；不存在返回空表。"""
    p = path or audit_file()
    if not p or not os.path.exists(p):
        return []
    out = []
    try:
        with io.open(p, encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if card_id and rec.get("card_id") != card_id:
                    continue
                out.append(rec)
    except OSError:
        return []
    out.reverse()
    return out[:limit] if limit else out


def load(path: str = "") -> tuple[list[dict], list[str]]:
    """读全部卡片，返回 `(cards, warnings)`。文件不存在 ⇒ `([], [])`（不是错误）。"""
    p = path or cards_file()
    cards, warns = [], []
    if not p or not os.path.exists(p):
        return cards, warns
    try:
        # `utf-8-sig`：Windows 上记事本/PowerShell 重定向写出的文件常带 BOM，
        # 用 `utf-8` 读会把**第一行**判成坏 JSON（BOM 混进首个字符）⇒ 静默丢一张卡。
        with io.open(p, encoding="utf-8-sig") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    warns.append("第 %d 行不是合法 JSON（已跳过）：%s" % (n, exc.msg))
                    continue
                if not isinstance(obj, dict):
                    warns.append("第 %d 行不是对象（已跳过）" % n)
                    continue
                cards.append(obj)
    except (OSError, UnicodeDecodeError) as exc:
        # 读不了就当空表：卡片层坏了不许拖垮检索（见模块 docstring 规矩 3）。
        # **UnicodeDecodeError 必须一起接**（2026-11-07 GLM 复核发现）：cards.jsonl 被人用记事本
        # ANSI / PowerShell 5.1 的 `>`/`Out-File`（默认非 UTF-8）改过一次，整层就 traceback，
        # cards 的 list/search/stats/validate/import/export 全部不可用 —— 那正是这条规矩要防的。
        warns.append("卡片文件读不了（当作空表）：%s: %s" % (type(exc).__name__, exc))
        return [], warns
    return cards, warns


MAX_BACKUPS = 5


def _rotate_backup(p: str) -> str:
    """写前快照：把现有文件按时间戳复制一份，只留最近 `MAX_BACKUPS` 份。

    原子写（tmp + replace）只能防"写一半断电"，防不了**逻辑损坏**：
    一次错误的 merge / 一次手滑的 `replace` 之后，原文件就没了。
    借鉴 TencentDB Agent Memory 的 BackupManager 思路（2026-11-09 调研）：
    **动之前先留一份，且恢复本身不许覆盖真正的错误信息。**
    """
    if not os.path.exists(p):
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = "%s.%s.bak" % (p, stamp)
    n = 1
    while os.path.exists(dst):                      # 同一秒内多次写
        n += 1
        dst = "%s.%s-%d.bak" % (p, stamp, n)
    try:
        with open(p, "rb") as src, open(dst, "wb") as out:
            out.write(src.read())
    except OSError:
        return ""                                   # 快照失败不许挡住写入
    olds = sorted(backups(p))
    for old in olds[:-MAX_BACKUPS]:
        try:
            os.remove(old)
        except OSError:
            pass
    return dst


def backups(path: str = "") -> list[str]:
    """列出该卡片文件的快照（旧 → 新）。"""
    p = path or cards_file()
    if not p:
        return []
    d = os.path.dirname(p) or "."
    base = os.path.basename(p) + "."
    try:
        names = [n for n in os.listdir(d) if n.startswith(base) and n.endswith(".bak")]
    except OSError:
        return []
    return [os.path.join(d, n) for n in sorted(names)]


def restore(src: str = "", path: str = "") -> tuple[bool, str]:
    """从快照恢复。`src` 为空则取最新一份。**恢复前先给当前文件留快照**，所以可反悔。"""
    p = path or cards_file()
    baks = backups(p)
    if not baks:
        return False, "没有可用的快照"
    if not src:
        src = baks[-1]
    if not os.path.exists(src):
        return False, "快照不存在：%s" % src
    rows, warns = load(src)                         # load 是 fail-soft：读不了 ⇒ 空表 + warning
    # 正因为 fail-soft，"空表"有两种含义：真的空，或者根本读不了。
    # 恢复必须区分这两者 —— 否则一份坏快照就能把卡表静默清空（真错误被警告吞掉）。
    hard = [w for w in warns if "读不了" in w]
    if hard:
        return False, "快照读不了（未改动当前文件）：%s" % hard[0]
    try:
        size = os.path.getsize(src)
    except OSError:
        size = 0
    if not rows and size > 0:
        return False, "快照里没有可解析的卡（未改动当前文件，%d 字节）" % size
    # save() 自己会在写前留一份当前文件 ⇒ 恢复本身也可反悔，不必在这里再拍一次
    try:
        save(rows, p)
    except Exception as exc:                        # noqa: BLE001
        return False, "写回失败：%s: %s" % (type(exc).__name__, exc)
    append_audit("restore", "", note="从 %s 恢复 %d 张" % (os.path.basename(src), len(rows)))
    return True, "已从 %s 恢复 %d 张卡" % (os.path.basename(src), len(rows))


def save(cards: list[dict], path: str = "") -> str:
    """原子写 + 写前快照：先 `<file>.tmp` 再 `os.replace`；旧文件留一份 `.bak`。

    返回写入路径（挂 `.tmp` 失败时的异常照常抛出，由调用方决定语义）。
    """
    p = path or cards_file()
    if not p:
        raise RuntimeError("没有可写的卡片文件路径（paths.CARDS_FILE 不可用）")
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    _rotate_backup(p)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for c in cards:
            fh.write(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, p)
    return p


# ═══════════ 校验 ═══════════
def normalize_status(raw: str) -> str | None:
    return ALIASES.get((raw or "").strip().lower())


def validate(card: dict) -> list[str]:
    """返回问题列表；空列表 = 合法。"""
    errs = []
    claim = (card.get("claim") or "").strip()
    if not claim:
        errs.append("缺 claim（一句话断言）")
    elif len(claim) > CLAIM_MAX:
        errs.append("claim 超过 %d 字（拆成多张卡）" % CLAIM_MAX)
    st = normalize_status(card.get("status") or "")
    if not st:
        errs.append("status 非法：%r（alive/stale/dead/unverified，或中文别名）"
                    % (card.get("status"),))
    ev = card.get("evidence")
    if not isinstance(ev, list) or not ev:
        errs.append("缺 evidence（至少 1 条：agent + date + quote）")
    else:
        for i, e in enumerate(ev, 1):
            if not isinstance(e, dict):
                errs.append("evidence[%d] 不是对象" % i)
                continue
            if not (e.get("agent") or "").strip():
                errs.append("evidence[%d] 缺 agent" % i)
            date = (e.get("date") or "").strip()
            if not _DATE_RE.match(date):
                errs.append("evidence[%d] 日期格式应为 YYYY-MM-DD，实际 %r" % (i, date))
            q = (e.get("quote") or "").strip()
            if not q:
                errs.append("evidence[%d] 缺 quote（原文摘录是跨机可迁移的证据下限）" % i)
            elif len(q) > QUOTE_MAX:
                errs.append("evidence[%d] quote 超过 %d 字" % (i, QUOTE_MAX))
            # msg = 本机 messages.id，用来把证据**精确锚回原文**（等价于 TencentDB Agent Memory
            # 的 node_id 下钻）。可选：跨机时它没有意义，但本机能让 `cards open` 打出全文。
            if "msg" in e and e["msg"] not in (None, ""):
                try:
                    int(e["msg"])
                except (TypeError, ValueError):
                    errs.append("evidence[%d] msg 必须是整数（messages.id），实际 %r" % (i, e["msg"]))
    # 机器状态类卡的声明式检查（2026-11-18）：允许缺省，写了就必须是已知类型
    vf = card.get("verify")
    if vf is not None:
        if not isinstance(vf, list) or not vf:
            errs.append("verify 必须是非空列表（如 [{\"kind\":\"dir_exists\",\"path\":\"C:\\\\x\"}]）")
        else:
            for i, chk in enumerate(vf, 1):
                if not isinstance(chk, dict):
                    errs.append("verify[%d] 不是对象" % i)
                    continue
                kind = chk.get("kind")
                if kind not in VERIFY_KINDS:
                    errs.append("verify[%d] 未知检查类型 %r（可选：%s）"
                                % (i, kind, "/".join(sorted(VERIFY_KINDS))))
                    continue
                field = VERIFY_KINDS[kind][0]
                if not str(chk.get(field) or "").strip():
                    errs.append("verify[%d] %s 缺 %s" % (i, kind, field))
    if card.get("superseded_by") and card.get("supersedes"):
        errs.append("同一张卡既 supersedes 又 superseded_by（链条会成环，请拆开）")
    # 作用域：允许缺省（旧卡 = machine），但**写了就必须是合法值**
    sc = card.get("scopes")
    if sc is not None:
        if not isinstance(sc, list):
            errs.append("scopes 必须是列表（如 [\"machine\", \"project:F:/x\"]）")
        elif not sc:
            # 显式空列表 ≠ 缺省：缺省（旧卡）视为 machine，而空列表意味着"不属于任何作用域"，
            # 结果是在任何 --scope-filter 下都查不到 —— 这是静默消失，必须报错。
            errs.append("scopes 不能是空列表（至少要 machine 或 project:<路径>）")
        else:
            for s in sc:
                if not isinstance(s, str) or not s.strip():
                    errs.append("scopes 里有空值/非字符串：%r" % (s,))
                elif s != SCOPE_MACHINE and not s.lower().startswith(SCOPE_PROJECT):
                    errs.append("非法 scope %r（只认 machine 或 project:<路径>）" % (s,))
                elif s.lower().startswith(SCOPE_PROJECT) and \
                        norm_path(s[len(SCOPE_PROJECT):]).replace(":", "") == "":
                    errs.append("project scope 缺路径：%r" % (s,))
    return errs


#: 作用域（2026-11-10）：一条结论属于"这台机器通用"还是"某个项目"。
#: 为什么需要它：原来的 `scope` 是**自由文本**（"agent-memory >=1.5.3；seed 与 last_search_via"），
#: 机器无法据此过滤 —— 于是"按当前 cwd 注入相关结论""只导出一个项目的卡"都做不了。
#: 借鉴 DSH 市场里 `dsh-engram` 的 dual-scope（用户级 + 工作区级）与
#: `yolk_vat-y/dsh-project-memory` 的 per-workspace 存储。
SCOPE_MACHINE = "machine"
SCOPE_PROJECT = "project:"

#: 机器状态类卡的**声明式**验证（2026-11-18）。
#: 为什么需要：像「codex 命令经 PATH 指向便携版」这种卡，证据不是"谁说过什么"，
#: 而是**当时的机器状态**——用聊天消息锚定它，等于把"可复核"降级成"有人说过"。
#: 所以给这类卡一个活体检查通道：每天跑一次，过了就是"现在仍然成立"。
#: ⚠️ 只允许这几种**纯声明**检查，**不执行任意命令**（卡文件是本机数据，但执行任意
#: 命令等于给它开了后门；要跑命令的场景应该在 tools/ 里写脚本，而不是塞进卡片）。
VERIFY_KINDS = {
    "file_exists": ("path",),       # 文件存在
    "dir_exists": ("path",),        # 目录存在
    "path_env_contains": ("value",),  # PATH 里含有该片段（大小写不敏感）
}


def parse_verify_spec(spec: str) -> dict:
    """把 CLI 的 `kind:value` 解析成检查对象（值为空则报错）。"""
    kind, _, value = (spec or "").partition(":")
    kind = kind.strip()
    value = value.strip()
    if kind not in VERIFY_KINDS:
        raise ValueError("未知检查类型 %r（可选：%s）"
                         % (kind, "/".join(sorted(VERIFY_KINDS))))
    if not value:
        raise ValueError("检查 %r 缺值（写法：%s:<路径或片段>）" % (kind, kind))
    return {"kind": kind, VERIFY_KINDS[kind][0]: value}


def run_verify(card: dict) -> list[dict]:
    """跑一张卡的声明式检查，返回逐条结果（不抛异常：检查失败要**报出来**，不是崩）。"""
    out = []
    for chk in card.get("verify") or []:
        kind = chk.get("kind")
        field = VERIFY_KINDS.get(kind, ("",))[0]
        target = str(chk.get(field) or "")
        ok, detail = False, ""
        try:
            if kind == "file_exists":
                ok = os.path.isfile(target)
                detail = "文件存在" if ok else "文件不存在"
            elif kind == "dir_exists":
                ok = os.path.isdir(target)
                detail = "目录存在" if ok else "目录不存在"
            elif kind == "path_env_contains":
                env = os.environ.get("PATH", "")
                ok = target.lower() in env.lower()
                detail = "PATH 含该片段" if ok else "PATH 不含该片段"
            else:
                detail = "未知检查类型 %r" % (kind,)
        except OSError as exc:
            detail = "检查失败：%s" % exc
        out.append({"kind": kind, "target": target, "ok": ok, "detail": detail})
    return out


def verify_machine_cards(cards: list[dict]) -> dict:
    """对所有带 `verify` 的卡跑活体检查，返回汇总。"""
    items, passed, failed, unchecked = [], 0, 0, 0
    for c in cards:
        checks = run_verify(c)
        if not checks:
            unchecked += 1
            continue
        all_ok = all(x["ok"] for x in checks)
        passed += 1 if all_ok else 0
        failed += 0 if all_ok else 1
        items.append({"id": c.get("id") or "", "claim": c.get("claim") or "",
                      "ok": all_ok, "checks": checks})
    return {"cards": len(cards), "with_checks": passed + failed, "passed": passed,
            "failed": failed, "unchecked": unchecked, "items": items}



def norm_path(p: str) -> str:
    """路径归一化：统一斜杠、去尾斜杠、小写（Windows 不区分大小写）。"""
    s = (p or "").strip().replace("\\", "/")
    while s.endswith("/") and len(s) > 2:
        s = s[:-1]
    return s.lower()


def parse_scopes(scopes) -> list[str]:
    """把 `"machine,project:F:\\demo-base"` / list / None 解析成规范列表。

    规则：`machine` 保留；`project:<path>` 归一化路径；其它值**原样保留交给 validate 报错**
    （不静默丢弃 —— 拼错的 scope 会悄悄让卡片"消失"在最常见的过滤里）。
    """
    if scopes is None:
        return []
    if isinstance(scopes, str):
        items = [x.strip() for x in scopes.split(",") if x.strip()]
    else:
        items = [str(x).strip() for x in scopes if str(x).strip()]
    out = []
    for it in items:
        low = it.lower()
        if low == SCOPE_MACHINE:
            out.append(SCOPE_MACHINE)
        elif low.startswith(SCOPE_PROJECT):
            out.append(SCOPE_PROJECT + norm_path(it[len(SCOPE_PROJECT):]))
        else:
            out.append(it)                     # 非法值原样保留，由 validate 报
    # 去重保序
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def card_scopes(card: dict) -> list[str]:
    """读一张卡的作用域；**旧卡没有 scopes 字段** ⇒ 视为 `["machine"]`（向后兼容）。"""
    raw = card.get("scopes")
    if raw is None:
        return [SCOPE_MACHINE]
    return parse_scopes(raw)


def scope_matches(card: dict, want: str) -> bool:
    """`want` 为 `machine` 或 `project:<path>`；项目作用域按**路径前缀**匹配。"""
    want = parse_scopes(want)
    if not want:
        return True
    b = want[0].lower()
    for s in card_scopes(card):
        s_low = s.lower()
        if s_low == b:
            return True
        if b.startswith(SCOPE_PROJECT) and s_low.startswith(SCOPE_PROJECT):
            wp, sp = b[len(SCOPE_PROJECT):], s_low[len(SCOPE_PROJECT):]
            if sp == wp or sp.startswith(wp + "/") or wp.startswith(sp + "/"):
                return True
    return False


def _card_id(claim: str, topic: str = "") -> str:
    h = hashlib.sha256(("%s||%s" % (claim.strip(), (topic or "").strip())).encode("utf-8"))
    return "c-" + h.hexdigest()[:8]


#: 卡片里能承载"可验证前提"的字段（巡检时只看这些）
_AUDIT_FIELDS = ("claim", "scope")


#: 否定/反转标记（判定"同义反向"用）。长标记在前，避免"不要"被"不"先吃掉。
_NEG_MARKERS = ("不应该", "不需要", "不用", "不要", "不会", "不能", "不得", "不必", "不需",
                "无需", "禁止", "避免", "严禁", "并非", "不是", "没有", "别", "不", "没", "无",
                "勿", "免")
#: 与否定标记配对的"肯定情态"（比较内容时必须一起剥，否则"要X"与"不要X"不对等）
_POS_MARKERS = ("需要", "必须", "应该", "应当", "允许", "推荐", "可以", "要", "需", "应")
#: 相似度比较时忽略的标点/空白
_STRIP_RE = re.compile(r"[\s\u3000，。、；：！？,.;:!?（）()\[\]【】「」“”\"'`~—\-]+")


def _norm_claim(s: str) -> str:
    """归一化：去标点空白、小写。"""
    return _STRIP_RE.sub("", (s or "").lower())


def _strip_neg(s: str) -> str:
    """剥掉否定标记**与配对的肯定情态**，只留"被否定/被肯定"的那部分内容。

    只剥否定会不对称：`要机器检` 剥完是 `要机器检`，`不要机器检` 剥完是 `机器检`，
    相似度只有 0.62，刚好卡在阈值边缘。两侧都剥情态后两者都是 `机器检`，相似度 1.0。
    """
    out = _norm_claim(s)
    for m in _NEG_MARKERS:
        out = out.replace(m, "")
    for m in _POS_MARKERS:
        out = out.replace(m, "")
    return out


def _bigrams(s: str) -> set:
    """汉字/字母 2-gram 集合（中文无词边界，2-gram 是最稳的近似）。"""
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def has_negation(s: str) -> bool:
    """claim 里是否出现否定标记（出现 ≥1 次即视为否定句）。"""
    n = _norm_claim(s)
    return any(m in n for m in _NEG_MARKERS)


def claim_similarity(a: str, b: str) -> float:
    """剥离否定后的 2-gram Jaccard 相似度（0~1）。"""
    x, y = _bigrams(_strip_neg(a)), _bigrams(_strip_neg(b))
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


# ═══════════ 人工确认账本（2026-11-19）═══════════
#: 确认一次的有效期（天）。语义：**内容没变就不再推给你**，变了 / 过期了自动恢复推送。
#: 为什么不是永久：环境会漂移，一季没复核过的结论不该继续免打扰。
ACK_TTL_DAYS = 30


def ack_file() -> str:
    """确认账本的默认位置（`index/cards-ack.jsonl`，与 cards.jsonl 并列）。"""
    try:
        from paths import CARDS_ACK_FILE
        return CARDS_ACK_FILE
    except Exception:                                   # noqa: BLE001
        return ""


def _ack_path(cards_path: str = "", ack_path: str = "") -> str:
    """账本路径：显式 > 卡片文件同目录 > paths 默认。

    为什么"跟随卡片文件"：多个卡片库（测试、多项目副本）各自有账本，否则测试会读写
    真实 index 目录、跑一次就污染本机数据。
    """
    if ack_path:
        return ack_path
    if cards_path:
        return os.path.join(os.path.dirname(os.path.abspath(cards_path)), "cards-ack.jsonl")
    return ack_file()


def card_sig(card: dict) -> str:
    """卡片内容指纹：只取"会改变注入价值"的部分，**不含时间戳**。

    为什么不用 `updated_at`：同内容重写（有人 import 一遍）不该让确认失效；
    而 `status` 被巡检改成 stale **必须**失效 —— 那正是该重新推给人的时刻。
    """
    payload = {
        "claim": _norm_claim(card.get("claim") or ""),
        "status": normalize_status(card.get("status") or "") or "",
        "topic": (card.get("topic") or "").strip().lower(),
        "scopes": sorted(s.strip().lower() for s in card_scopes(card)),
        "evidence": [[(e.get("agent") or "").strip(), (e.get("date") or "").strip(),
                      (e.get("quote") or "").strip()]
                     for e in (card.get("evidence") or []) if isinstance(e, dict)],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _ack_ts(entry: dict) -> float:
    """账本时间戳 → epoch 秒；格式坏了返回 0（= 立即过期，宁可按未确认处理）。"""
    try:
        return time.mktime(time.strptime((entry.get("ack_at") or "").strip()[:19],
                                        "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return 0.0


def load_acks(ack_path: str = "", cards_path: str = "") -> tuple[dict, list[str]]:
    """读账本 → ({id: entry}, warnings)。坏行跳过计数：账本坏了**不许**让注入变砖。"""
    p = _ack_path(cards_path, ack_path)
    if not p or not os.path.exists(p):
        return {}, []
    out, warns = {}, []
    try:
        with io.open(p, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    warns.append("账本第 %d 行不是合法 JSON，已跳过" % i)
                    continue
                cid = (o.get("id") or "").strip()
                if not cid:
                    warns.append("账本第 %d 行缺 id，已跳过" % i)
                    continue
                out[cid] = o
    except OSError as exc:
        return {}, ["账本读失败（按空账本继续）：%s" % exc]
    return out, warns


def save_acks(registry: dict, ack_path: str = "", cards_path: str = "") -> str:
    """原子写账本（临时文件 + os.replace），与 `save()` 同一套做法。"""
    p = _ack_path(cards_path, ack_path)
    if not p:
        return ""
    d = os.path.dirname(os.path.abspath(p))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for cid in sorted(registry):
            f.write(json.dumps(registry[cid], ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, p)
    return p


def ack(card_id: str, by: str = "human", cards_path: str = "",
        ack_path: str = "") -> tuple[bool, str]:
    """确认一张卡：记下「当前内容指纹 + 时间 + 谁」。重复确认是幂等的。"""
    rows, _ = load(cards_path)
    card = next((c for c in rows if c.get("id") == card_id), None)
    if not card:
        return False, "找不到卡片 %r（用 cards list 看 id）" % card_id
    if not _ack_path(cards_path, ack_path):
        return False, "账本路径不可用（index/ 没初始化？跑一次 init）"
    reg, _warns = load_acks(ack_path, cards_path)
    reg[card_id] = {
        "id": card_id,
        "sig": card_sig(card),
        "ack_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "by": (by or "human").strip()[:32],
    }
    save_acks(reg, ack_path, cards_path)
    return True, "已确认 %s：内容不变期间不再注入；status/证据一变就自动重现" % card_id


def unack(card_id: str, cards_path: str = "", ack_path: str = "") -> tuple[bool, str]:
    """撤销确认（下轮注入会重新出现）。"""
    reg, _warns = load_acks(ack_path, cards_path)
    if (card_id or "").strip() not in reg:
        return False, "账本里没有 %r" % card_id
    reg.pop(card_id.strip())
    save_acks(reg, ack_path, cards_path)
    return True, "已撤销确认 %s（下轮注入会重新出现）" % card_id


def acks(cards_path: str = "", ack_path: str = "", ttl_days: int = 0) -> list[dict]:
    """列出账本并标出每条的现状态。

    state：`valid` 仍免打扰 ｜ `changed` 卡片内容已变（会自动重现）｜
    `expired` 超过 TTL ｜ `missing` 卡片已不存在。
    """
    reg, _warns = load_acks(ack_path, cards_path)
    rows, _w2 = load(cards_path)
    by_id = {c.get("id"): c for c in rows}
    ttl = ttl_days or ACK_TTL_DAYS
    now = time.time()
    out = []
    for cid, e in reg.items():
        card = by_id.get(cid)
        if card is None:
            st = "missing"
        elif card_sig(card) != (e.get("sig") or ""):
            st = "changed"
        elif now - _ack_ts(e) >= ttl * 86400:
            st = "expired"
        else:
            st = "valid"
        item = dict(e)
        item.update({"state": st, "claim": (card or {}).get("claim") or ""})
        out.append(item)
    return sorted(out, key=lambda x: (x.get("ack_at") or "", x.get("id") or ""), reverse=True)


def acked_ids(cards: list[dict], ttl_days: int = 0, cards_path: str = "",
              ack_path: str = "") -> set:
    """这批卡里"应该跳过注入"的 id 集合 —— 注入侧唯一入口。

    两条失效路径都在这里判掉：**内容指纹变了** 或 **超过 TTL** ⇒ 不再跳过。
    """
    reg, _warns = load_acks(ack_path, cards_path)
    if not reg:
        return set()
    ttl = ttl_days or ACK_TTL_DAYS
    now = time.time()
    out = set()
    for c in cards:
        cid = c.get("id")
        e = reg.get(cid or "")
        if not e:
            continue
        if card_sig(c) != (e.get("sig") or ""):
            continue
        if now - _ack_ts(e) >= ttl * 86400:
            continue
        out.add(cid)
    return out


def prune_acks(cards_path: str = "", ack_path: str = "",
               ttl_days: int = 0) -> tuple[int, int]:
    """清掉"已经没有意义"的账本条目（卡不在 / 内容已变 / 过期）→ (清掉, 剩几条)。"""
    reg, _warns = load_acks(ack_path, cards_path)
    if not reg:
        return 0, 0
    live = {x["id"]: x["state"] for x in acks(cards_path, ack_path, ttl_days)}
    newreg = {cid: e for cid, e in reg.items() if live.get(cid) == "valid"}
    removed = len(reg) - len(newreg)
    if removed:
        save_acks(newreg, ack_path, cards_path)
    return removed, len(newreg)


def detect_conflicts(path: str = "", topic: str = "", min_sim: float = 0.6) -> list[dict]:
    """检测疑似矛盾的卡片配对（**只报告，不改任何东西**）。

    ## 为什么需要

    `supersedes` 链一直是**人工**维护的。两张卡说相反的话，系统不发现、人也没注意，
    它们就会同时"成立"。市场里 `memory-eternal` 讲"自研去重"、`dsh-mneme` 讲
    "autoDream consolidation" —— 共识是**同一主题的多条记忆需要合并/裁决**。
    但我们**不做**自动合并（那需要 LLM，且实测规则抽取噪音高），只把可疑配对摆出来让人裁决。

    ## 判据（三条同时满足才报，宁少勿多）

      1. 同 `topic`、都是 `alive`/`unverified`；
      2. 未建立 `supersedes`/`superseded_by` 关系（已知矛盾不必再报）；
      3. 剥掉否定词后 2-gram 相似度 ≥ `min_sim`，且两条的**否定极性不同**。
    """
    rows, warns = load(path)
    if topic:
        rows = [c for c in rows if (c.get("topic") or "") == topic]
    live = [c for c in rows if (c.get("status") or "unverified") in ("alive", "unverified")]
    out = []
    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            a, b = live[i], live[j]
            if (a.get("topic") or "") != (b.get("topic") or ""):
                continue
            aid, bid = a.get("id"), b.get("id")
            if (aid in (b.get("supersedes") or [])) or (bid in (a.get("supersedes") or [])):
                continue
            if a.get("superseded_by") == bid or b.get("superseded_by") == aid:
                continue
            sim = claim_similarity(a.get("claim") or "", b.get("claim") or "")
            if sim < min_sim:
                continue
            na, nb = has_negation(a.get("claim") or ""), has_negation(b.get("claim") or "")
            if na == nb:
                continue                       # 极性相同的相似句不是矛盾，是重复
            out.append({
                "topic": a.get("topic") or "",
                "a": {"id": aid, "claim": a.get("claim"), "neg": na, "status": a.get("status")},
                "b": {"id": bid, "claim": b.get("claim"), "neg": nb, "status": b.get("status")},
                "similarity": round(sim, 3),
            })
    out.sort(key=lambda x: -x["similarity"])
    return out


def audit_cards(analyze, path: str = "", statuses=("alive", "unverified"),
                apply: bool = False) -> dict:
    """前提巡检：让"过期"自己浮出来（2026-11-10，改进清单第 4 条）。

    `analyze(card) -> list[{path, verdict, note}]` 由调用方注入（CLI 用 premises 的
    `extract_paths` + `is_project_asset` + `judge_path` 拼）—— **cards 层不依赖 premises**，
    换一个判据源（比如"版本号是否还匹配"）也能复用这个函数。

    ## 为什么需要它

    卡片的 `status` 一直靠**人**改。而"结论里提到的路径现在还在不在"完全可以让机器判
    （`premises` 早就有三路判定：活 / 项目已迁移 / 真失效）。市场里 `stratagate-dsh`
    把"旧的会褪色成摘要"当卖点、`dsh-memoir` 讲 lifecycle —— 共识是**记忆需要生命周期**。

    ## 安全边界（比功能本身重要）

    - 命中失效前提 ⇒ 只置 `stale`（**不置 dead、不删除**）：路径没了不等于结论错了；
    - 改状态必须留痕：`status_history` 追加 `{at, from, to, reason}`，可人工回滚；
    - `apply=False` 时**只报告不改**（默认），让人先看一眼；
    - 已被推翻（dead）的卡不参与巡检。
    """
    rows, warns = load(path)
    report = {"cards": len(rows), "scanned": 0, "no_paths": 0,
              "candidates": [], "applied": 0, "warnings": warns}
    now = _now()
    changed = False
    for c in rows:
        st = c.get("status") or "unverified"
        if st not in statuses:
            continue
        report["scanned"] += 1
        findings = analyze(c) or []
        if not findings:
            report["no_paths"] += 1
            continue
        dead = [f for f in findings
                if f.get("verdict") in ("dead_with_successor", "dead_no_successor")]
        if not dead:
            continue
        reason = "前提已失效：" + "；".join(
            "%s（%s）" % (f.get("path"), f.get("verdict")) for f in dead[:3])
        report["candidates"].append({
            "id": c.get("id"), "claim": c.get("claim"),
            "from": st, "reason": reason,
            "findings": findings,
        })
        if not apply:
            continue
        # 改状态 + 留痕（可回滚）
        hist = c.get("status_history") or []
        hist.append({"at": now, "from": st, "to": "stale", "reason": reason})
        c["status_history"] = hist
        c["status"] = "stale"
        c["stale_reason"] = reason
        c["updated_at"] = now
        changed = True
        report["applied"] += 1
        append_audit("audit_stale", c.get("id", ""), after=c, note=reason)
    if apply and changed:
        save(rows, path)
    return report


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ═══════════ 证据核验（2026-11-10）═══════════
#: 摘录里的省略标记：允许作者跳过大段原文，但**每段**都得在原文里找到
_ELISION = re.compile(r"(?:…+|⋯+|\.{3,})")
#: 单段最短长度：太短的片段（"的"、"了"）容易偶然命中，不构成证据
_MIN_SEG = 4


def _norm_for_match(s: str) -> str:
    """归一化后再比：去掉所有空白 + 忽略大小写。

    原文里的换行/缩进与摘录里的一行文字，在"是否同一句话"上是等价的；
    中文没有词边界，所以不能按空格切词比较，只能整段子串比对。
    """
    return re.sub(r"\s+", "", s or "").lower()


def verify_evidence(fetch_text, card: dict, min_seg: int = _MIN_SEG) -> dict:
    """核验一张卡的全部证据是否能**回到原文**（不是格式校验，是事实校验）。

    `fetch_text(msg_id) -> str | None`：由调用方注入（CLI 传 store.message_by_id 的包装，
    找不到返回 None）。这样 cards.py 不依赖 store，保持分层。

    为什么要它：`validate()` 只保证"quote 非空且不太长"，**从不检查它是否真出自原文**；
    而"可溯源"正是这套卡片的核心承诺 —— 没有这一步，承诺是空的。
    借鉴 DSH 市场里 `dsh-memento`「auditable」/`dsh-engram`「evidence」的共识：
    记忆的可信度来自证据可回查，不来自算法。
    """
    out = {"total": 0, "verified": [], "mismatch": [], "missing_msg": [],
           "no_anchor": [], "verifiable_ratio": 0.0}
    for i, e in enumerate(card.get("evidence") or [], 1):
        if not isinstance(e, dict):
            continue
        out["total"] += 1
        quote = (e.get("quote") or "").strip()
        msg = e.get("msg")
        if msg in (None, ""):
            out["no_anchor"].append(i)
            continue
        try:
            text = fetch_text(int(msg))
        except (TypeError, ValueError):
            out["no_anchor"].append(i)
            continue
        if text is None:
            out["missing_msg"].append(i)
            continue
        hay = _norm_for_match(text)
        # 允许用省略号跳过原文，但每一段都得找到
        segs = [s for s in _ELISION.split(quote) if len(_norm_for_match(s)) >= min_seg]
        if not segs:
            out["no_anchor"].append(i)      # 全是省略号/太短 ⇒ 无法核验
            continue
        bad = [s for s in segs if _norm_for_match(s) not in hay]
        if bad:
            out["mismatch"].append((i, "原文里找不到：%r" % bad[0][:40]))
        else:
            out["verified"].append(i)
    checked = len(out["verified"]) + len(out["mismatch"]) + len(out["missing_msg"])
    out["verifiable_ratio"] = (len(out["verified"]) / checked) if checked else 0.0
    return out


def verify_cards(fetch_text, path: str = "") -> dict:
    """整库证据核验：汇总 + 有问题的卡明细。"""
    rows, warns = load(path)
    bad, total_ev = [], 0
    ver = mis = miss = noanchor = 0
    for c in rows:
        r = verify_evidence(fetch_text, c)
        total_ev += r["total"]
        ver += len(r["verified"])
        mis += len(r["mismatch"])
        miss += len(r["missing_msg"])
        noanchor += len(r["no_anchor"])
        if r["mismatch"] or r["missing_msg"]:
            bad.append({"id": c.get("id"), "claim": c.get("claim"), "detail": r})
    checked = ver + mis + miss
    return {"cards": len(rows), "evidence": total_ev, "verified": ver,
            "mismatch": mis, "missing_msg": miss, "no_anchor": noanchor,
            "verifiable_ratio": (ver / checked) if checked else 0.0,
            "bad_cards": bad, "warnings": warns}


def build(claim: str, evidence: list[dict], status: str = "alive", topic: str = "",
          scope: str = "", supersedes: list[str] | None = None,
          scopes=None) -> dict:
    """构造一张卡（不落盘）。

    `status` 不认识时**原样保留**（不悄悄退化成 alive）——不然一个拼错的 `ded` 会被当成
    "仍成立"，而它本该是"已被推翻"。非法值由 `validate()` 报出来。
    """
    raw = (status or "").strip()
    st = normalize_status(raw) if raw else "alive"
    card = {
        "id": _card_id(claim, topic),
        "claim": claim.strip(),
        "topic": (topic or "").strip(),
        "status": st or raw,
        "scope": (scope or "").strip(),
        # 结构化作用域（见 SCOPE_MACHINE / SCOPE_PROJECT）。
        # 不传 ⇒ 默认 machine（"这台机器通用"是绝大多数结论的情况）；
        # 显式空列表是**错误**（见 validate），因为那意味着"任何 scope 过滤都查不到"。
        "scopes": parse_scopes(scopes) if scopes is not None else [SCOPE_MACHINE],
        "evidence": [dict(e) for e in (evidence or [])],
        "supersedes": list(supersedes or []),
        "superseded_by": "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    return card


# ═══════════ 增 / 改 ═══════════
def upsert(card: dict, path: str = "") -> tuple[str, str]:
    """写入或更新一张卡，返回 `(状态, 说明)`；状态 ∈ added / updated / invalid / exists。"""
    errs = validate(card)
    if errs:
        return "invalid", "；".join(errs)
    cards, warns = load(path)
    cid = card.get("id") or _card_id(card.get("claim", ""), card.get("topic", ""))
    card["id"] = cid
    # 自洽防呆（2026-11-18 实测踩到）：同一 claim 重新 add 时，调用方可能顺手把「旧 id」
    # 传成 --supersedes-card，而 upsert 命中同一张卡 ⇒ 卡片"推翻它自己"，
    # 渲染成 `⇢ 推翻了 c-xxxx`（就是它自己），关系图变成自环。这里剔掉自己。
    if card.get("supersedes"):
        card["supersedes"] = [x for x in card["supersedes"] if x and x != cid]
    for i, old in enumerate(cards):
        if old.get("id") == cid:
            if json.dumps(old, sort_keys=True, ensure_ascii=False) == \
                    json.dumps(card, sort_keys=True, ensure_ascii=False):
                return "exists", "已存在且内容一致"
            before_snapshot = dict(old)
            card["created_at"] = old.get("created_at") or card.get("created_at")
            card["superseded_by"] = old.get("superseded_by") or card.get("superseded_by", "")
            card["updated_at"] = _now()
            cards[i] = card
            save(cards, path)
            append_audit("update", cid, before=before_snapshot, after=card)
            return "updated", "已更新 %s" % cid
    cards.append(card)
    save(cards, path)
    append_audit("add", cid, after=card)
    return "added", "已新增 %s%s" % (cid, ("（另有 %d 条读警告）" % len(warns)) if warns else "")


def supersede(old_id: str, new_id: str, path: str = "") -> tuple[bool, str]:
    """把 `old_id` 标成被 `new_id` 推翻：旧卡 dead + superseded_by，新卡记 supersedes。"""
    cards, _ = load(path)
    by_id = {c.get("id"): c for c in cards}
    if old_id not in by_id:
        return False, "找不到旧卡 %s" % old_id
    if new_id not in by_id:
        return False, "找不到新卡 %s" % new_id
    if old_id == new_id:
        return False, "不能自我推翻"
    old, new = by_id[old_id], by_id[new_id]
    # 审计要记「改前」快照：必须在改字段**之前**复制，否则 before==after，追溯就没意义了
    old_before = dict(old)
    new_before = dict(new)
    old["status"] = "dead"
    old["superseded_by"] = new_id
    old["updated_at"] = _now()
    sup = new.get("supersedes") or []
    if old_id not in sup:
        sup.append(old_id)
    new["supersedes"] = sup
    new["updated_at"] = _now()
    save(cards, path)
    append_audit("supersede", old_id, before=old_before, after=old,
                 note="被 %s 推翻" % new_id)
    append_audit("superseded_by", new_id, before=new_before, after=new,
                 note="推翻了 %s" % old_id)
    return True, "%s 已被 %s 推翻" % (old_id, new_id)


# ═══════════ 查 ═══════════
def search(text: str, path: str = "", status: str = "", topic: str = "",
           scope: str = "") -> list[dict]:
    """在 claim / topic / scope / quote 上做**子串**匹配（大小写不敏感）。

    不用分词：卡片是短文本，且中文分词对"别名/旧名"这类问题本就不适用
    （见 docs/reviews 里 `search` 别名补检那一课）。
    """
    cards, _ = load(path)
    want_st = normalize_status(status) if status else ""
    out = []
    low = (text or "").strip().lower()
    for c in cards:
        if want_st and c.get("status") != want_st:
            continue
        if topic and (c.get("topic") or "") != topic:
            continue
        if scope and not scope_matches(c, scope):
            continue
        if low:
            hay = " ".join([c.get("claim") or "", c.get("topic") or "", c.get("scope") or ""] +
                           [(e.get("quote") or "") for e in c.get("evidence") or []])
            if low not in hay.lower():
                continue
        out.append(c)
    return out


def stats(path: str = "") -> dict:
    cards, warns = load(path)
    by = {k: 0 for k in STATUS}
    topics = {}
    for c in cards:
        st = c.get("status") or "unverified"
        by[st] = by.get(st, 0) + 1
        t = c.get("topic") or "(未分类)"
        topics[t] = topics.get(t, 0) + 1
    return {"total": len(cards), "by_status": by, "topics": topics,
            "warnings": warns, "file": path or cards_file()}


def render(card: dict) -> str:
    """单卡的人类可读形式（CLI 用）。"""
    st = card.get("status") or "unverified"
    lines = ["%s %s" % (STATUS.get(st, "[?]"), card.get("claim", ""))]
    meta = []
    if card.get("topic"):
        meta.append("topic=%s" % card["topic"])
    sc = card_scopes(card)
    if sc:
        meta.append("scopes=%s" % ",".join(sc))
    if card.get("scope"):
        meta.append("适用范围=%s" % card["scope"])
    if card.get("id"):
        meta.append("id=%s" % card["id"])
    if meta:
        lines.append("      " + " ｜ ".join(meta))
    if card.get("superseded_by"):
        lines.append("      ⇢ 已被 %s 推翻" % card["superseded_by"])
    if card.get("supersedes"):
        lines.append("      ⇢ 推翻了 %s" % "、".join(card["supersedes"]))
    for e in card.get("evidence") or []:
        loc = e.get("session") or ""
        anchor = ("  #%s" % e["msg"]) if e.get("msg") not in (None, "") else ""
        lines.append("      · [%s/%s]%s%s %s" % (e.get("agent", "?"), e.get("date", "?"),
                                                 (" " + loc) if loc else "", anchor,
                                                 e.get("quote", "")))
    return "\n".join(lines)


#: distill 输出的状态标签（与 render 保持一致，避免两套措辞）
_DISTILL_ORDER = ("alive", "unverified", "stale", "dead")


def distill(topic: str, dst: str = "", path: str = "") -> dict:
    """把某个 topic 的卡汇总成一份 **SOP 草稿**（零 LLM，纯模板）。

    ## 为什么做这个（改进清单第 8 条）

    同 topic 下攒了 N 张卡之后，人只能 `cards list --topic` 一条条看，看不出"这件事到底怎么做的"。
    市场里 `@a9i5k4/dsh-auto-memory` 讲「skill crystallization」、`@weibaohui/hermes-loop` 讲
    「把有价值的经历提炼成可复用的技能」—— 但那些都要 LLM。我们只做**确定性重排**：
    同样的输入永远产出同样的文本，不引入任何模型调用。

    ## 结构（固定，便于人扫读与后续 diff）

      # <topic> — 操作要点（草稿）
      ## 一、前提（适用条件）
      ## 二、结论（按 仍成立 → 未验证 → 前提已变 排序）
      ## 三、证据
      ## 四、历史：被推翻的做法（勿照做）

    ## 两条纪律

      1. **必须人工审阅后才落 docs/**（草稿不是结论）—— 函数只写文件，不提交、不判定；
      2. 证据分两类如实统计：带 `msg` 锚点（可回查）/ 无锚点（跨机不可回查）。
    """
    rows, warns = load(path)
    cards = [c for c in rows if (c.get("topic") or "") == topic]
    if not cards:
        return {"topic": topic, "cards": 0, "written": "", "warnings": warns,
                "reason": "该 topic 下没有卡片"}

    cards.sort(key=lambda c: (_DISTILL_ORDER.index(c.get("status") or "unverified")
                              if (c.get("status") or "unverified") in _DISTILL_ORDER else 9,
                              c.get("updated_at") or ""))

    dead = [c for c in cards if (c.get("status") or "") == "dead"]
    live = [c for c in cards if (c.get("status") or "") in ("alive", "unverified")]
    stale = [c for c in cards if (c.get("status") or "") == "stale"]
    anchored = sum(1 for c in cards for e in (c.get("evidence") or [])
                   if e.get("msg") not in (None, ""))
    total_ev = sum(len(c.get("evidence") or []) for c in cards)

    out = ["# %s — 操作要点（草稿，待人工审阅）" % topic, ""]
    out.append("> 由 `agent-memory cards distill` 生成，**不是结论文档**：零 LLM 的确定性重排，"
               "审阅后请自行删掉这句再落 `docs/`。")
    out.append("> 卡片 %d 张（仍成立 %d ／ 未验证 %d ／ 前提已变 %d ／ 被推翻 %d）｜ "
               "证据 %d 条（带锚点 %d）"
               % (len(cards), len([c for c in live if (c.get("status") or "") == "alive"]),
                  len([c for c in live if (c.get("status") or "") == "unverified"]),
                  len(stale), len(dead), total_ev, anchored))
    if total_ev and anchored < total_ev:
        out.append("> ⚠ 有 %d 条证据无 `msg` 锚点（跨机卡片不可回查），引用时需谨慎。"
                   % (total_ev - anchored))
    out.append("")

    def _status_tag(c):
        st = c.get("status") or "unverified"
        return {"alive": "仍成立", "unverified": "未验证",
                "stale": "前提已变", "dead": "已被推翻"}.get(st, st)

    # 一、前提
    out.append("## 一、前提（适用条件）")
    out.append("")
    # 前提只取"还活着"的卡：dead 卡的 scope 是**当时**的适用条件，已被推翻，不能当现在的前提
    prereq = [(c, c.get("scope") or "") for c in cards
              if (c.get("scope") or "").strip()
              and (c.get("status") or "unverified") in ("alive", "unverified", "stale")]
    if prereq:
        for c, scope in prereq:
            out.append("- %s（来自 `%s`，%s）" % (scope, c.get("id"), _status_tag(c)))
    else:
        out.append("- （卡片都没写 `scope`，说明适用条件缺失——补上再发布）")
    out.append("")

    # 二、结论
    out.append("## 二、结论")
    out.append("")
    if live:
        for c in live:
            out.append("- **[%s]** %s" % (_status_tag(c), (c.get("claim") or "").strip()))
            if c.get("scopes"):
                out.append("  - 作用域：%s" % ",".join(card_scopes(c)))
    else:
        out.append("- （没有仍成立或未验证的结论）")
    out.append("")
    if stale:
        out.append("### 前提已变（用前先核实）")
        out.append("")
        for c in stale:
            out.append("- %s" % (c.get("claim") or "").strip())
            if c.get("stale_reason"):
                out.append("  - %s" % c["stale_reason"])
        out.append("")

    # 三、证据
    out.append("## 三、证据")
    out.append("")
    for c in cards:
        evs = c.get("evidence") or []
        if not evs:
            continue
        out.append("### %s" % (c.get("claim") or "")[:60])
        for e in evs:
            anchor = ("#%s" % e["msg"]) if e.get("msg") not in (None, "") else "（无锚点）"
            out.append("- [%s %s]%s %s" % (e.get("agent", "?"), e.get("date", "?"),
                                           anchor, (e.get("quote") or "").strip()))
        out.append("")

    # 四、历史
    out.append("## 四、历史：被推翻的做法（勿照做）")
    out.append("")
    if dead:
        for c in dead:
            out.append("- ~~%s~~" % (c.get("claim") or "").strip())
            if c.get("superseded_by"):
                out.append("  - 被 `%s` 推翻" % c["superseded_by"])
            if c.get("supersedes"):
                out.append("  - 它推翻了 %s" % "、".join("`%s`" % x for x in c["supersedes"]))
    else:
        out.append("- （暂无被推翻的记录）")
    out.append("")

    text = "\n".join(out).rstrip("\n") + "\n"
    if dst:
        d = os.path.dirname(os.path.abspath(dst))
        if d:
            os.makedirs(d, exist_ok=True)
        with io.open(dst, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return {"topic": topic, "cards": len(cards), "written": dst or "", "text": text,
            "evidence": total_ev, "anchored": anchored,
            "by_status": {s: sum(1 for c in cards if (c.get("status") or "unverified") == s)
                          for s in _DISTILL_ORDER},
            "warnings": warns}


def export_md(dst: str, path: str = "") -> tuple[int, list[str]]:
    """导出为**人类可读** Markdown，**固定三段**：被推翻 → 仍成立 → 其余。

    为什么固定三段（2026-11-10，改进清单第 7 条）：
      · `cards.jsonl` 是机器格式，人要看得跑 `cards list`；Markdown 落到 `docs/` 后
        能被我自己的 docs 层直接检索（卡片层与 docs 层就此打通）；
      · **被推翻的卡必须置顶**：它是"别再这么干"的经验，混在正文里最容易被人跳过
        （借鉴 `dsh-gokifu-compact` 的「failure memory survives compaction」）；
      · 同一 topic 内部按状态分组，而不是按时间——读的人先看"哪些不能信"。
    """
    rows, warns = load(path)
    d = os.path.dirname(os.path.abspath(dst))
    if d:
        os.makedirs(d, exist_ok=True)

    dead = [c for c in rows if (c.get("status") or "") == "dead"]
    alive = [c for c in rows if (c.get("status") or "") in ("alive", "unverified")]
    other = [c for c in rows if (c.get("status") or "") == "stale"]
    dups = find_duplicate_claims(rows)

    out = ["# 结论卡索引（由 `agent-memory cards export --md` 生成）", ""]
    out.append("> 共 %d 张卡 ｜ 被推翻 %d ／ 仍成立(含未验证) %d ／ 前提已变 %d"
               % (len(rows), len(dead), len(alive), len(other)))
    out.append("> 本文件可被 `search --docs-first` 检索；来源：`index/cards.jsonl`。")
    if dups:
        out.append("> ⚠ 检测到 %d 组「同一断言、多个 id」的重复登记，见文末。" % len(dups))
    out.append("")

    def _render(group, header, note):
        if not group:
            return
        out.append("## %s" % header)
        if note:
            out.append("")
            out.append("> %s" % note)
        out.append("")
        by_topic: dict[str, list[dict]] = {}
        for c in group:
            by_topic.setdefault(c.get("topic") or "(未分类)", []).append(c)
        for topic in sorted(by_topic):
            out.append("### %s" % topic)
            out.append("")
            for c in by_topic[topic]:
                st = c.get("status") or "unverified"
                out.append("- **%s**  \n  `status=%s`%s"
                           % ((c.get("claim") or "").strip(), st,
                              (" ｜ 适用范围：%s" % c["scope"]) if c.get("scope") else ""))
                if c.get("superseded_by"):
                    out.append("  - 已被 `%s` 推翻" % c["superseded_by"])
                if c.get("stale_reason"):
                    out.append("  - 前提已失效：%s" % c["stale_reason"])
                for e in c.get("evidence") or []:
                    anchor = ("#%s" % e["msg"]) if e.get("msg") not in (None, "") else ""
                    out.append("  - [%s %s]%s %s"
                               % (e.get("agent", "?"), e.get("date", "?"), anchor,
                                  (e.get("quote") or "").strip()))
            out.append("")

    _render(dead, "一、已被推翻（勿再采信）",
            "下面是**过去这么认为、后来被推翻**的结论。它们比成立的结论更值钱——"
            "照着做会重犯同一个错误。")
    _render(alive, "二、仍成立 / 未验证", "")
    _render(other, "三、前提已变（用前先核实）",
            "前提（路径/环境）已失效，结论未必错，但**不能直接照用**。")

    if dups:
        out.append("## 附：重复登记（同一断言、多个 id）")
        out.append("")
        for g in dups:
            ids = "、".join("`%s`(%s)" % (c["id"], c.get("topic") or "无topic")
                           for c in g["cards"])
            out.append("- %s ｜ %s" % ((g["cards"][0].get("claim") or "")[:60], ids))
        out.append("")
        out.append("> 裁决方式：留一张、其余 `cards supersede` 或删除；系统不会自动合并。")
        out.append("")

    with io.open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out).rstrip("\n") + "\n")
    return len(rows), warns


# ═══════════ 迁移 ═══════════
_EXPORT_RANK = {"dead": 0, "stale": 1, "alive": 2, "unverified": 3}


def export_to(dst: str, path: str = "") -> tuple[int, list[str]]:
    """导出为 JSONL。返回 `(条数, warnings)`。

    两条约定（2026-11-10，改进清单第 7 条）：
      · **dead 卡置顶**：`supersede` 链是最有价值的经验，迁移/重排后必须显眼地留在最前面
        （借鉴 `dsh-gokifu-compact` 的「failure memory survives compaction」）；
      · 每张卡带 `dedupe_key`（规范化 claim 的 sha8）：导入方可据此发现"同一断言多 id"的重复。
        只写进**导出副本**，不改库里的卡（导出是只读操作）。
    """
    cards, warns = load(path)
    ordered = sorted(cards, key=lambda c: (_EXPORT_RANK.get(c.get("status") or "unverified", 9),
                                           c.get("updated_at") or ""))
    d = os.path.dirname(os.path.abspath(dst))
    if d:
        os.makedirs(d, exist_ok=True)
    with io.open(dst, "w", encoding="utf-8", newline="\n") as fh:
        for c in ordered:
            row = dict(c)
            row["dedupe_key"] = dedupe_key(c)
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(cards), warns


def import_from(src: str, mode: str = "merge", path: str = "") -> tuple[dict, list[str]]:
    """从 JSONL 导入。

    `mode=merge`（默认）：按 id 去重合并，**同 id 保留较新的 `updated_at`**；
    `mode=replace`：整表替换（危险，调用方须显式确认）。
    导入的卡片若带 `session`，那只在原机器可回查——这里不动它，但会在 warnings 里提示。
    """
    incoming, warns = load(src)
    if mode == "replace":
        save(incoming, path)
        dups = find_duplicate_claims(incoming)
        if dups:
            warns.append("导入后有 %d 组「同一断言、多个 id」的重复登记（`cards list` 可查，"
                         "裁决请用 supersede；系统不自动合并）" % len(dups))
        return {"mode": "replace", "total": len(incoming), "added": len(incoming),
                "updated": 0, "skipped": 0, "duplicate_groups": len(dups)}, warns
    cur, w2 = load(path)
    warns += w2
    by_id = {c.get("id"): c for c in cur}
    added = updated = skipped = 0
    portable = 0
    for c in incoming:
        cid = c.get("id")
        if not cid:
            skipped += 1
            continue
        if any(e.get("session") for e in c.get("evidence") or []):
            portable += 1
        old = by_id.get(cid)
        if old is None:
            cur.append(c)
            by_id[cid] = c
            added += 1
        elif (c.get("updated_at") or "") > (old.get("updated_at") or ""):
            cur = [c if x.get("id") == cid else x for x in cur]
            by_id[cid] = c
            updated += 1
        else:
            skipped += 1
    save(cur, path)
    if portable:
        warns.append("有 %d 张卡带 session 字段：换机后无法回查原文，只能靠 quote 取证" % portable)
    # 重复登记检测（改进清单第 7 条）：同一句断言被挂到不同 topic 下会得到不同 id，
    # 只按 id 去重的合并会**同时留下两张** —— 必须报出来让人裁决，不能自动合并。
    dups = find_duplicate_claims(cur)
    if dups:
        warns.append("库内有 %d 组「同一断言、多个 id」的重复登记（只报告，不自动合并）"
                     % len(dups))
    return {"mode": "merge", "total": len(cur), "added": added, "updated": updated,
            "skipped": skipped, "duplicate_groups": len(dups)}, warns
