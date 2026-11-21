"""procedure 挖掘（只读）：从 tool_calls 里提炼"可复用的操作步骤"。

## 为什么不抄 Memorax 的做法

Memorax 的 "Procedure Memory" 是**学习式**（模型学什么时候想起来，靠奖励/权重，
承认「遗忘与权重变化难以观测」）。本项目是**零模型**，所以做成规则式：
每一步都能回答「为什么挖掘出来这条」和「这条现在还成不成立」（前提判定见 premises.py）。

## 两类原料（2026-11-17 实测）

| 路线 | 信号 | 实测产量 |
|---|---|---|
| A `repair` | 失败 -> 5 条内同程序重试成功 | 2,005 个（85% 的失败都有） |
| B `ritual` | 同目录跨 >=3 会话重复的归一化命令 | 202 个 |

A 是"怎么修"的知识（`python -c` 失败 -> `.\\venv\\Scripts\\python.exe -c` 成功）；
B 是"怎么做"的仪式（`$env:PYTHONIOENCODING='utf-8'` 前缀、`cd <项目>`）。

## 归一化纪律

命令归一化必须**保留能区分写法的部分**、抹掉每次不同的参数：
路径 -> `<P>`、引号内容 -> `<Q>`、数字 -> `<N>`。否则
`python -c "x"` 与 `python -c "y"` 会被当成两条不同的步骤，
而 `python -c` 与 `.\\venv\\Scripts\\python.exe -c` 必须保持**不同**（这正是要学的东西）。

## 只读

本模块只 SELECT，不写任何表、不生成文件。产物由 CLI 决定要不要落盘。
"""
from __future__ import annotations

import collections
import os
import re
import sqlite3
import time

from diagnoses import stable_id      # 单向依赖：diagnoses 只依赖 paths，不成环

#: 命令首 token 的处理：`.\\venv\\Scripts\\python.exe` 与 `python` 必须区分，
#: 所以这里只做大小写/扩展名归一，**不做等价合并**（等价合并会丢掉"该用哪个解释器"这个关键知识）
_EXE_SUFFIX = re.compile(r"\.exe\b", re.I)

#: 归一化时抹掉的"每次不同"的部分
_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"']*")
_REL_PATH_RE = re.compile(r"(?<![\w:])/(?:[\w./-]{3,})")
_SQUOTE_RE = re.compile(r"'[^']*'")
_DQUOTE_RE = re.compile(r'"[^"]*"')
_NUM_RE = re.compile(r"\b\d+\b")
#: 多个空白压成一个（命令常带换行）
_WS_RE = re.compile(r"\s+")

#: 命令主干的截断长度：procedure 里出现超长内联脚本没有复用价值
MAX_CMD_LEN = 600


def normalize_command(cmd: str) -> str:
    """把一条命令归一成"形态"：抹掉参数值，保留写法差异。

    例：
      `.\\venv\\Scripts\\python.exe -c "import x"` -> `.\\venv\\scripts\\python -c <q>`
      `python -c "import x"`                     -> `python -c <q>`
      两者**不同**，这正是要学的东西。
    """
    s = (cmd or "").strip()
    if not s:
        return ""
    s = _WS_RE.sub(" ", s)
    # `2>&1` 里的数字是重定向语法，不是参数值：先占位再还原，
    # 否则会被 _NUM_RE 抹成 `N>&N`（既不可读，也让幂等类型命令聚类不准）
    s = s.replace("2>&1", "@@r@@")
    s = _PATH_RE.sub("<p>", s)
    s = _REL_PATH_RE.sub("<p>", s)
    s = _SQUOTE_RE.sub("<q>", s)
    s = _DQUOTE_RE.sub("<q>", s)
    s = _NUM_RE.sub("<n>", s)
    s = _EXE_SUFFIX.sub("", s)
    s = s.lower().strip()
    s = s.replace("@@r@@", "2>&1")
    return s[:MAX_CMD_LEN]


def command_head(cmd: str) -> str:
    """命令首 token（程序名），用于判断"是不是同一个程序"。

    `.\\venv\\Scripts\\python.exe` 与 `python` 的头不同 —— 这是**故意**的：
    重试路线里"换了程序"本身可能就是要学的知识。
    """
    s = (cmd or "").strip()
    if not s:
        return ""
    # 取第一段（遇到 | ; 截断）
    seg = re.split(r"[;|]", s, 1)[0].strip()
    if not seg:
        return ""
    tok = seg.split()[0]
    return _EXE_SUFFIX.sub("", tok).lower()


def _rows_to_sessions(con: sqlite3.Connection) -> dict[str, list[tuple]]:
    """把 tool_calls 按会话聚成有序列表：(seq, command, ok, exit_code)。"""
    out: dict[str, list[tuple]] = collections.defaultdict(list)
    for r in con.execute(
        "SELECT session_key, seq, command, ok, exit_code FROM tool_calls "
        "ORDER BY session_key, seq"
    ):
        out[r["session_key"]].append((r["seq"], r["command"] or "", r["ok"], r["exit_code"]))
    return out


def _session_cwds(con: sqlite3.Connection) -> dict[str, str]:
    """会话 → cwd（小写）。修复对也要带目录归属，否则无法按实体注入。"""
    return {r["session_key"]: (r["cwd"] or "").lower()
            for r in con.execute("SELECT session_key, cwd FROM sessions")}


def _token_overlap(a: str, b: str) -> float:
    """两个归一化形态的 token 重合率（Jaccard）。

    ⚠️ 注意：它比较的是**掩码后**的形态，对 `powershell -command "<脚本>"` 这类
    包装式命令会失效（任何脚本都掩成 `<q>`，形态相同）——所以真正的修复闸门用
    `_similar_enough`（字符级，保留引号内容），本函数只作辅助与测试用途。
    """
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def _similarity_form(cmd: str) -> str:
    """相似度用的形态：**只抹路径与数字，保留引号里的内容**。

    为什么保留引号内容（2026-11-17 多 agent 数据实测）：`powershell -NoProfile
    -Command "<任意脚本>"` 的掩码形态完全一样，于是"失败后随便跑个成功的 powershell"
    都会被配成修复。保留引号内容后，不同脚本的字符相似度会掉下来。
    """
    s = _WS_RE.sub(" ", (cmd or "").strip()).lower()
    s = _PATH_RE.sub("<p>", s)
    s = _NUM_RE.sub("<n>", s)
    return s[:MAX_CMD_LEN]


def _similarity(a: str, b: str) -> float:
    """两条原始命令的字符相似度（difflib 比值；用保留引号内容的形态）。"""
    import difflib
    fa, fb = _similarity_form(a), _similarity_form(b)
    if not fa or not fb:
        return 0.0
    return difflib.SequenceMatcher(None, fa, fb).ratio()


def _similar_enough(a: str, b: str, threshold: float = 0.85) -> bool:
    """两条**原始命令**是否"只差一小段"（= 真修复的形态）。

    用字符级相似度（difflib 比值）而不是 token 集合：
    真修复是"在原来那条命令上改一小处"（`-First 20` → `-Last 5`），
    比值高；而换了脚本/换了目标则是两条不同的命令，比值低。

    额外放行"在命令尾部追加一小段"这种最常见的修复形态
    （`| tail -3`、`2>&1`、`--apply`）——短命令时比值算出来会低于阈值，
    但语义上确实是修复，所以按**前缀关系 + 追加长度**单独放行。
    """
    fa, fb = _similarity_form(a), _similarity_form(b)
    if not fa or not fb or fa == fb:
        return False                       # 空或完全一样：不是"改正"
    if _similarity(a, b) >= threshold:
        return True
    short, long_ = sorted((fa, fb), key=len)
    extra = len(long_) - len(short)
    if extra <= 30 and long_.startswith(short):
        return True                        # 尾部追加（| tail -N / 2>&1 / --apply）
    if extra <= 15 and short in long_:
        return True                        # 中间插入一小段
    return False


def mine_repairs(con: sqlite3.Connection, window: int = 5,
                 min_sessions: int = 1, min_overlap: float = 0.5) -> list[dict]:
    """路线 A：失败 -> 同程序改正 -> 成功（且两条命令足够相似 = 真修复）。

    返回按"出现次数"降序的候选；每条带：
      bad/good（归一化后的形态对，用于聚类）
      sample_bad/sample_good（一条真实样例，给人工看）
      sessions（出现过的会话数）、raw_hits（总出现次数）、overlap（形态相似度）
    """
    sessions = _rows_to_sessions(con)
    cwds = _session_cwds(con)
    groups: dict[tuple, dict] = {}

    for skey, seq in sessions.items():
        for i, (seq_no, cmd, ok, code) in enumerate(seq):
            if ok != 0:
                continue
            head = command_head(cmd)
            if not head:
                continue
            for seq2, cmd2, ok2, code2 in seq[i + 1:i + 1 + window]:
                if ok2 != 1 or command_head(cmd2) != head:
                    continue
                bad, good = normalize_command(cmd), normalize_command(cmd2)
                if not bad or bad == good:
                    continue
                if not _similar_enough(cmd, cmd2):
                    continue          # 换了脚本/换了目标，不是修复
                sim = _similarity(cmd, cmd2)
                ov = _token_overlap(bad, good)
                key = (bad, good)
                g = groups.get(key)
                if g is None:
                    g = groups[key] = {
                        "shape_bad": bad, "shape_good": good,
                        "sample_bad": cmd[:400], "sample_good": cmd2[:400],
                        "sessions": set(), "raw_hits": 0,
                        "exit_codes": set(), "sim": sim, "overlap": ov,
                        "cwds": collections.Counter(),
                    }
                g["sessions"].add(skey)
                g["raw_hits"] += 1
                g["cwds"][cwds.get(skey, "")] += 1
                if code is not None:
                    g["exit_codes"].add(code)
                break          # 每个失败只配一次（最近的改正）

    out = []
    for g in groups.values():
        if len(g["sessions"]) < min_sessions:
            continue
        out.append({
            "kind": "repair",
            "id": stable_id(g["shape_bad"], g["shape_good"]),
            "bad": g["shape_bad"],
            "good": g["shape_good"],
            "sample_bad": g["sample_bad"],
            "sample_good": g["sample_good"],
            "sessions": len(g["sessions"]),
            "raw_hits": g["raw_hits"],
            "exit_codes": sorted(g["exit_codes"]),
            "overlap": round(g["overlap"], 2),
            "sim": round(g["sim"], 2),
            "cwd": (g["cwds"].most_common(1)[0][0] if g["cwds"] else ""),
        })
    # 排序按 raw_hits（同一会话内重复踩同一个坑也是高价值），
    # 不按 sessions：实测 1,776 组里只有 3 组跨 >=2 会话 —— 修复知识基本是
    # "这一次碰到的问题"，价值在**当次可查**，不在跨会话复用。不要用 sessions
    # 排序把它压到最后（那等于把 99.8% 的产出扔掉）。
    out.sort(key=lambda x: (-x["raw_hits"], -x["sessions"]))
    return out


def mine_rituals(con: sqlite3.Connection, min_sessions: int = 3) -> list[dict]:
    """路线 B：同目录跨 >=N 会话重复的归一化命令（"每次都要做的仪式"）。

    为什么按**目录**分组而不是全局：不同项目的仪式不同（一个项目要
    `cd <项目A>`、另一个要 `cd <项目B>`），全局聚类会把它们混成一条。
    """
    groups: dict[tuple, dict] = {}
    rows = con.execute(
        "SELECT t.session_key, t.command, s.cwd FROM tool_calls t "
        "JOIN sessions s ON s.session_key = t.session_key"
    )
    per_session: dict[tuple, set] = collections.defaultdict(set)
    samples: dict[tuple, str] = {}
    for skey, cmd, cwd in rows:
        shape = normalize_command(cmd)
        if not shape or len(shape) < 4:
            continue
        key = ((cwd or "").lower(), shape)
        per_session[key].add(skey)
        if key not in samples or len(cmd) < len(samples[key]):
            samples[key] = cmd[:400]          # 留最短的那条当样例（最像"仪式"）

    for key, sess in per_session.items():
        if len(sess) < min_sessions:
            continue
        if not _has_substance(key[1]):
            continue
        groups[key] = {
            "kind": "ritual",
            "shape": key[1],
            "cwd": key[0],
            "sample": samples[key],
            "sessions": len(sess),
        }
    out = sorted(groups.values(), key=lambda g: -g["sessions"])
    return out


#: 归一化后"有信息量"的判据：抹掉 `<p>/<q>/<n>` 占位符后，还得剩下足够多的
#: 真实单词。反例（实测）：`python -c <q>` / `& <p> -c <q>` —— 抹完只剩
#: `python -c`，任何内联脚本都长这样，不是可执行步骤，进列表只会稀释信噪比。
_PLACEHOLDER_RE = re.compile(r"<[pqn]>")

#: 能说明"在做具体事"的动作词：出现其一才算可执行步骤
#: （不是只有解释器 + 内联表达式）
_ACTION_WORDS = (
    "pytest", "-m ", "tools/", "scripts/", "select-object", "select-string",
    "format-table", "get-childitem", "git ", "npm ", "pnpm ", "node ",
    "python ", "set-location", "cd <p>", "$env:", "[console]", "pip ",
    "docker", "ollama", "curl", "tar ", "compile", "build", "test",
)


def _has_substance(shape: str) -> bool:
    """归一化形态是否是一条"可执行步骤"（而非内联表达式壳）。"""
    rest = _PLACEHOLDER_RE.sub(" ", shape)
    words = [w for w in rest.split() if len(w) >= 2]
    if len(words) < 2:
        return False
    low = shape
    # 纯内联表达式壳：解释器 + -c/-e + 占位符，没有任何脚本名/子命令 -> 丢掉
    if re.fullmatch(r".*(python|node|py)\s+-[ce]\s*<q>.*", low) and not any(
            k in low for k in ("-m ", "tools/", "scripts/", ".py", ".mjs", ".js")):
        return False
    return any(k in low for k in _ACTION_WORDS)


def summarize(con: sqlite3.Connection) -> dict:
    """挖掘面的规模概览（doctor / CLI 头部用）。"""
    q = con.execute
    total = q("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    fails = q("SELECT COUNT(*) FROM tool_calls WHERE ok=0").fetchone()[0]
    nulls = q("SELECT COUNT(*) FROM tool_calls WHERE ok IS NULL").fetchone()[0]
    oks = q("SELECT COUNT(*) FROM tool_calls WHERE ok=1").fetchone()[0]
    sess = q("SELECT COUNT(DISTINCT session_key) FROM tool_calls").fetchone()[0]
    return {"calls": total, "ok": oks, "failed": fails, "no_result": nulls,
            "sessions": sess}


# ── 第 3 步：给 procedure 挂前提判定 ─────────────────────────────────────
def _paths_in(text: str) -> list[str]:
    """抽出文本里的绝对路径（复用 premises 的抽取器，含中文标点截断规则）。"""
    import premises
    return premises.extract_paths(text or "")


#: 判定结果的短标签（basis 用；与 premises 的四个常量一一对应）
_BASIS_LABEL = {
    "alive": "存在",
    "dead_with_successor": "已迁移",
    "dead_no_successor": "已不存在",
    "unverified": "不可判",
}

#: 证书的失效条件（Many Steps into One 的"事实一变就重启证书"在本仓的落法）
RECHECK_HINT = "使用前复核路径存在性；路径消失或项目迁移即作废"


def _basis_text(verdicts: list[tuple[str, str, str]]) -> str:
    """把逐条判定压成"凭什么"的一句话（认证依据）。"""
    if not verdicts:
        return "无可验证路径前提"
    counts = collections.Counter(v for _p, v, _n in verdicts)
    order = ("alive", "dead_with_successor", "dead_no_successor", "unverified")
    parts = ["%d 条%s" % (counts[v], _BASIS_LABEL[v]) for v in order if counts.get(v)]
    return "；".join(parts) or "无可验证路径前提"


def _cert(verdict: str, note: str, verdicts: list[tuple[str, str, str]]) -> dict:
    """构造一条**认证结果**（证书）。

    为什么要认证而不是计数（Many Steps into One, arXiv 2610.17544 的结论）：
    成功/失败计数是糟糕的变更检测器（陈旧答案多 7~13.5 倍）；安全做法是
    **对照活事实做认证、事实一变就重启**。这里的证书 = 结论 + 检验时刻 +
    依据 + 失效条件，四件套都显式给出，下游（注入块/CLI）才能复核。
    """
    return {
        "verdict": verdict,
        "note": note,
        "paths": verdicts,
        "certified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "basis": _basis_text(verdicts),
        "recheck": RECHECK_HINT,
    }


def judge(item: dict, live_dirs: dict, dir_to_entity: dict) -> dict:
    """判定一条 procedure 的前提现在还成不成立，并给出**认证四件套**。

    判据（与 premises 的三路语义一致）：
      · 命令里引用的**解释器/脚本路径**还存在 -> alive
      · 不存在但所属项目仍有活目录 -> migrated（带注：项目已迁移至 X）
      · 不存在且项目无活目录 -> dead（这条步骤照做会失败）
      · 抽不到路径（纯 shell 内建）-> unverified

    返回值除 verdict/note/paths 外，还带：
      certified_at  本次检验时刻（存在性是检验时刻的函数，必须显式）
      basis         依据摘要（"2 条存在；1 条已迁移"）
      recheck       失效条件（路径消失或项目迁移即作废）

    为什么要它：procedure 是从历史命令里挖出来的，历史命令的前提会变
    （venv 删了、项目改名、脚本搬走）。不判定就把"照做会报错"的步骤
    原样给 agent，正好违背本项目"过期事实比没有事实更危险"的立场。
    """
    import premises
    cwd = item.get("cwd") or ""
    samples = [item.get("sample") or "", item.get("sample_bad") or "",
               item.get("sample_good") or ""]
    verdicts: list[tuple[str, str, str]] = []      # (path, verdict, note)

    # cwd 是这个 procedure 的隐含前提（"在哪个目录做这件事"）
    if cwd:
        v, note = premises.judge_path(cwd, live_dirs, dir_to_entity)
        verdicts.append((cwd, v, note or "工作目录"))

    for text in samples:
        for p in _paths_in(text):
            v, note = premises.judge_path(p, live_dirs, dir_to_entity)
            verdicts.append((p, v, note))

    if not verdicts:
        return _cert(premises.UNVERIFIED, "无可验证路径前提", [])

    # 整条结论：任一 dead 即 dead（照做会失败）；否则有 migrated 即 migrated
    kinds = {v for _p, v, _n in verdicts}
    if premises.DEAD_NO_SUCCESSOR in kinds:
        bad = [p for p, v, _ in verdicts if v == premises.DEAD_NO_SUCCESSOR]
        return _cert(premises.DEAD_NO_SUCCESSOR,
                     "路径已不存在：%s" % ", ".join(bad[:2]), verdicts)
    if premises.DEAD_WITH_SUCCESSOR in kinds:
        note = next(n for _p, v, n in verdicts if v == premises.DEAD_WITH_SUCCESSOR)
        return _cert(premises.DEAD_WITH_SUCCESSOR, note, verdicts)
    if premises.ALIVE in kinds:
        return _cert(premises.ALIVE, "", verdicts)
    return _cert(premises.UNVERIFIED, "路径均属非项目资产，不判", verdicts)


# ── 注入块（把 procedure 接进 context.md）──────────────────────────────
#: 渲染时把归一化占位符换成人能读的形式（占位符在 CLI 里是给聚类用的，注入块要可读）
_SHAPE_CLEAN = (("<p>", "…"), ("<q>", '"…"'), ("<n>", "N"))

#: 注入块里每条的形态上限（超出就不是"步骤"，是长脚本，注入只会撑爆上下文）
_INJECT_SHAPE_MAX = 80


def _clean_shape(shape: str) -> str:
    s = shape
    for a, b in _SHAPE_CLEAN:
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > _INJECT_SHAPE_MAX:
        s = s[:_INJECT_SHAPE_MAX].rstrip() + "…"
    return s


def _clip(text: str, limit: int) -> str:
    """单行截断（诊断原文常带换行，注入块里必须压成一行）。"""
    s = re.sub(r"\s+", " ", (text or "")).strip()
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _owned_factory(entity: str, live: dict, d2e: dict):
    """构造"某 cwd 是否属于该实体"的判定函数（含已搬家旧目录，三层归属）。

    三层（2026-11-17 实测补的第三层）：只按 `d2e` 精确匹配会漏掉**已消失的旧目录**
    （`f:\\dp` 目录现在不存在，但它历史上有 269 条消息）。所以：
      ① live dirs（现存的）
      ② dir_entity_map 里映射到该实体的（含旧名）
      ③ **judge 判为 migrated 的 cwd**：它的后继目录里若有属于该实体的，
         说明这就是"同一项目搬家前"的 procedure（迁移语义与 premises 一致）
    """
    import premises

    dirs = {d.lower() for d in (live.get(entity) or [])}
    dirs |= {d.lower() for d, e in d2e.items() if e == entity}
    entity_live = {d.lower() for d in (live.get(entity) or [])}

    def _owned(cwd: str) -> bool:
        c = (cwd or "").lower()
        if not c:
            return False
        if c in dirs:
            return True
        pdir = premises.project_dir_of(c)
        if pdir and not os.path.exists(c):
            succ = (live.get(entity) or [])
            if succ and any(os.path.basename(s).lower() == os.path.basename(pdir) or
                            pdir.replace("\\", "/").split("/")[-1].lower() ==
                            s.replace("\\", "/").split("/")[-1].lower()
                            for s in succ):
                return True
            # 名称对不上时：只有该实体唯一现存目录、且 cwd 项目确实曾属该实体才放行
            if len(entity_live) == 1 and d2e.get(pdir) == entity:
                return True
        return False

    return _owned


def pick_from_lists(entity: str, rituals: list[dict], repairs: list[dict],
                    live: dict, d2e: dict, max_items: int = 3,
                    max_repairs: int = 1) -> dict:
    """从**已挖好的** rituals/repairs 里挑出该实体的注入项（毫秒级，供缓存路径用）。

    与 `select_for_entity` 的区别只在于"不现挖"：筛选/置信/前提/配额的规则完全一致，
    所以两条路径的结果必须一致（测试钉住这一点）。
    """
    import premises

    _owned = _owned_factory(entity, live, d2e)
    rituals = [r for r in rituals if _owned(r.get("cwd") or "")]
    repairs = [r for r in repairs if _owned(r.get("cwd") or "")]

    import diagnoses
    diagnosed = {rid: rec for rid, rec in diagnoses.load()["items"].items()}

    def _conf_ok(r: dict) -> bool:
        """修复对能否进注入块：有重复证据，或**人类已诊断**。

        依据（2026-11-17 实测）：包装式命令（`powershell -Command "<脚本>"`）换脚本时
        形态前缀相同、相似度很高，只能靠"跨会话重复"这类证据压住假阳性；
        而人类诊断是更强的证据（WASP-Mem：一轮人类诊断 > 四轮自判 7.0~10.9%）。
        """
        if r.get("id") in diagnosed:
            return True
        return ((r.get("sessions") or 0) >= 2
                or ((r.get("raw_hits") or 0) >= 3 and (r.get("sim") or 0) >= 0.95))

    # 先全量筛（置信度 + 前提），再按配额取 —— 否则"未列出"与"被过滤"会重复计数
    eligible: list[dict] = []
    dead_skipped = conf_skipped = 0
    for kind, rows in (("ritual", rituals), ("repair", repairs)):
        for r in rows:
            if kind == "repair" and not _conf_ok(r):
                conf_skipped += 1          # 置信不足：只给 CLI 浏览
                continue
            v = judge(r, live, d2e)
            if v["verdict"] == premises.DEAD_NO_SUCCESSOR:
                dead_skipped += 1          # 照做会失败：不进注入
                continue
            item = dict(r)
            item["judge"] = v
            if kind == "repair" and item.get("id") in diagnosed:
                item["diagnosis"] = diagnosed[item["id"]]
            eligible.append(item)

    n_ritual = max(0, max_items - max_repairs)
    picked = ([i for i in eligible if i["kind"] == "ritual"][:n_ritual]
              + [i for i in eligible if i["kind"] == "repair"][:max_repairs])
    # 配额没填满就补仪式：max_items 小（如 1）且修复对不足时，别让预算空着
    if len(picked) < max_items:
        used = {id(i) for i in picked}
        for i in eligible:
            if len(picked) >= max_items:
                break
            if id(i) not in used:
                picked.append(i)

    return {"entity": entity, "items": picked,
            "candidates": len(rituals) + len(repairs), "eligible": len(eligible),
            "dead_skipped": dead_skipped, "conf_skipped": conf_skipped,
            "dirs": sorted({d.lower() for d in (live.get(entity) or [])})}


def select_for_entity(db_path: str, entity: str, max_items: int = 3,
                      max_repairs: int = 1) -> dict:
    """挑出属于某实体的 procedure（**只留前提没失效的**）—— 现挖版（CLI/日更用）。

    前提过滤：🔴 dead_no_successor 的一律不进注入块（照做会失败的步骤
    不能递给 agent），只报数量；🟡 migrated 会进，但**必须带迁移注**。

    ⚠️ 现挖一次约 12 秒（实测）：MCP 工具**不要**走这条路，走 `pick_cached()`
    （缓存版，毫秒级）。
    """
    import premises

    live = premises.live_dirs_by_entity(db_path)
    d2e = premises.dir_entity_map(db_path)

    con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    try:
        rituals = mine_rituals(con, min_sessions=3)
        repairs = mine_repairs(con)
    finally:
        con.close()

    return pick_from_lists(entity, rituals, repairs, live, d2e, max_items, max_repairs)


def pick_cached(db_path: str, entity: str, max_items: int = 3,
                max_repairs: int = 1, ttl_hours: float = 24.0,
                allow_build: bool = True) -> dict:
    """缓存版：毫秒级，供 MCP 工具用。

    返回额外带 `cache_note`（"缓存命中/现算/陈旧"）——调用方必须把它转述出去，
    不能把陈旧数据当新鲜的用。
    """
    import premises
    import proc_cache

    live = premises.live_dirs_by_entity(db_path)
    d2e = premises.dir_entity_map(db_path)

    con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    try:
        doc, note = proc_cache.ensure(con, db_path, ttl_hours, allow_build)
    finally:
        con.close()

    blk = pick_from_lists(entity, doc.get("rituals") or [], doc.get("repairs") or [],
                          live, d2e, max_items, max_repairs)
    blk["cache_note"] = note
    blk["cache_built_at"] = doc.get("built_at") or ""
    return blk


def _cert_mark(v: dict) -> str:
    """注入块里的认证标记（短：省字符，但信息完整）。

    形如 `（✓11-17 认证：2 条前提存在）`；迁移/未验证带对应的劝退或告警。
    设计依据：Many Steps 论文要求"结论必须能回答凭什么"，注入块是 agent 唯一
    会读的地方，所以依据必须进块，而不是只留在 CLI。
    """
    if not v:
        return ""
    verdict = v.get("verdict") or ""
    day = (v.get("certified_at") or "")[5:10]        # MM-DD（省字符）
    basis = v.get("basis") or ""
    if verdict == "dead_with_successor":
        return "（⚠ 项目已迁移，先换目录；%s 校：%s）" % (day or "?", basis)
    if verdict == "unverified":
        return "（前提未验证%s）" % (("：" + basis) if basis else "")
    if verdict == "alive":
        return "（✓%s 认证：%s）" % (day or "?", basis)
    return ""


def render_block(blk: dict, max_chars: int = 500) -> str:
    """把 select_for_entity 的结果渲染成注入块（有界，且明说截断）。"""
    head = "### 本项目常用操作（agent-memory procedure；前提已校验）"
    if (not blk["items"] and not blk["dead_skipped"]
            and not blk.get("conf_skipped")):
        # 本项目确实没有可注入的 procedure：**不写空块**。
        # 注入是每轮成本，写明"没有"只是噪音；要查全量有 `am procedure`。
        return ""
    parts = [head, ""]
    if not blk["items"]:
        # 不能在这里直接 return：下面还要报「有多少条因前提失效被跳过」——
        # 全部失效时只说"没有可用的"会让人以为这个项目没有 procedure 可挖。
        parts.append("（本项目没有可直接照做的 procedure）")

    used = 0
    shown = 0
    bound_by = ""
    for it in blk["items"]:
        v = it.get("judge") or {}
        mark = _cert_mark(v)
        if it["kind"] == "ritual":
            line = "- 常用：`%s`（%d 会话）%s" % (
                _clean_shape(it.get("shape") or ""), it.get("sessions") or 0, mark)
        else:
            dg = it.get("diagnosis") or {}
            if dg:
                # 人类诊断过：优先展示人话诊断（它比形态对更好懂，且是更强的证据）
                day = (dg.get("diagnosed_at") or "")[5:10]
                line = "- 修法：`%s` 失败 ⇒ `%s`（人已诊断 %s：%s）" % (
                    _clean_shape(it.get("bad") or ""),
                    _clean_shape(it.get("good") or ""), day or "?",
                    _clip(dg.get("diagnosis") or "", 60))
            else:
                line = "- 修法：`%s` 失败 ⇒ `%s`（%d 次）%s" % (
                    _clean_shape(it.get("bad") or ""),
                    _clean_shape(it.get("good") or ""), it.get("raw_hits") or 0, mark)
        if used + len(line) > max_chars:
            bound_by = "chars"
            break
        parts.append(line)
        used += len(line)
        shown += 1

    hidden = max(0, blk.get("eligible", len(blk["items"])) - shown)
    if hidden > 0 or blk["dead_skipped"] or blk.get("conf_skipped"):
        bits = []
        if hidden > 0:
            bits.append("另有 **%d** 条未列出（%s）"
                        % (hidden, "**字符上限 %d**" % max_chars if bound_by == "chars"
                           else "条数上限"))
        if blk.get("conf_skipped"):
            bits.append("%d 条修复对置信度不足未注入（`am procedure --kind repair` 浏览）"
                        % blk["conf_skipped"])
        if blk["dead_skipped"]:
            bits.append("%d 条前提已失效未注入（照做会失败）" % blk["dead_skipped"])
        parts.append("")
        parts.append("> " + "；".join(bits) + "。全量用 `am procedure --judge`。")
    return "\n".join(parts)
