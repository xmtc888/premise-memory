"""会话索引：SQLite + FTS5（含 trigram 索引给中文用）。

设计借鉴（实测 Hermes `session_search` 后确认有效的部分）：
  · **索引用 0 次 LLM**：纯 FTS5 MATCH，毫秒级；
  · **中文用 trigram 索引**：两个索引分工是**实测**出来的（2026-11-04 内存 FTS5 验证）——
    `unicode61` **不切分 CJK**：一段连续中文是**一个 token**，所以任何中文子串都查不到
    （`招标`/`保证金` 全部 0 命中，连 3 字词也不中）；`trigram` 按 3 字滑窗建索引，
    3 字及以上的中文短语能命中，但 **1~2 字的中文词（`质保`/`预算`）在 trigram 上也查不到**——
    这条由 `search()` 的 LIKE 回退兜底（`last_search_via == "like"`，调用方要报给用户）；
  · **内容有界**：单条正文入库前已截断 20000 字符（见 collectors/base.clean_text）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Iterable

from collectors.base import CollectResult, Record, SessionMeta

#: 数据 schema 版本（存在 SQLite 的 `PRAGMA user_version`）。
#: 为什么要它（借鉴 Helm 的 version vs appVersion 分离）：包版本（SKILL.md 的 version）
#: 与**数据**版本是两件事 —— 升级包不等于数据已迁移，反过来也一样。doctor 会把两者
#: 都报出来；未来的迁移按这个号做阶梯（0 = 2026-11-04 之前的库，结构已由 _migrate 补齐；
#: 2 = 2026-11-17 新增 tool_calls 表，procedure 原料落库）。
SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
    session_key   TEXT PRIMARY KEY,
    agent         TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    title         TEXT,
    cwd           TEXT,
    started_at    TEXT,
    ended_at      TEXT,
    model         TEXT,
    provider      TEXT,
    parent_session TEXT,
    origin        TEXT,
    source_path   TEXT,
    peer          TEXT,                 -- 工作区身份：由 cwd 的 git origin 派生（见 peers.py）
    message_count INTEGER,
    tool_call_count INTEGER,
    indexed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    agent       TEXT NOT NULL,
    role        TEXT NOT NULL,
    ts          TEXT,
    seq         INTEGER,
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_key);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id', tokenize='unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_trgm USING fts5(
    text, content='messages', content_rowid='id', tokenize='trigram'
);

-- 话题归并结果（第二阶段 A）：一个会话可有多个实体与意图
CREATE TABLE IF NOT EXISTS session_topics (
    session_key TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- 'entity' | 'intent'
    label       TEXT NOT NULL,
    hits        INTEGER DEFAULT 0,      -- 命中次数，用于排序/权重
    PRIMARY KEY (session_key, kind, label)
);
CREATE INDEX IF NOT EXISTS idx_topics_label ON session_topics(kind, label);

-- 工具调用碰过的项目目录（第二阶段 A 的归属信号；只存目录名与次数，不存工具输出）
CREATE TABLE IF NOT EXISTS session_paths (
    session_key TEXT NOT NULL,
    dir         TEXT NOT NULL,
    hits        INTEGER DEFAULT 0,
    PRIMARY KEY (session_key, dir)
);
CREATE INDEX IF NOT EXISTS idx_paths_dir ON session_paths(dir);

-- 工具调用本体（2026-11-17，procedure 原料）：只落 pwsh/命令类工具，
-- 不存输出正文（那是 messages 的噪音大头）。ok 由 error 键与
-- message 里的 "[exit code: N]" 双通道判定（探针 A 实测两者都有）。
CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    seq         INTEGER NOT NULL,       -- 会话内序号（稳定排序用）
    ts          TEXT,
    tool        TEXT NOT NULL,          -- 'pwsh'（后续可扩）
    command     TEXT NOT NULL,          -- 命令本体（截断 2000 字符）
    ok          INTEGER,                -- 1 成功 / 0 失败 / NULL 无结果
    exit_code   INTEGER                 -- pwsh 退出码；error 键路径为 NULL
);
CREATE INDEX IF NOT EXISTS idx_toolcalls_session ON tool_calls(session_key);
CREATE INDEX IF NOT EXISTS idx_toolcalls_ok ON tool_calls(ok);
"""


class QueryError(ValueError):
    """查询本身有问题（FTS5 语法错误），**必须**与"没命中"区分开。

    2026-11-04 glm-5.3 复核指出：原来 `except sqlite3.OperationalError: return []` 把
    `C++`、未闭合引号这类语法错误静默变成"零命中"，用户会以为"历史上没聊过"。
    """

    def __init__(self, query: str, detail: str):
        self.query = query
        self.detail = detail
        super().__init__(
            "查询语法错误（不是没有结果）：%r ｜ 原始错误：%s ｜ 提示：FTS5 里 "
            "`+ - * ^ : \" ( )` 是语法字符，搜它们请加引号或换词" % (query, detail))


#: 是否含中日韩字符（决定要不要走 LIKE 回退）
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

#: 这些字符出现在查询里通常是"我想搜这个词"，不是语法意图（`agent-memory`、`user:query`）
_RELAXABLE_CHARS = re.compile(r"[-:.]")
#: 这些是真·语法字符：出现就不做宽松重试，直接报错（`C++`、`(a OR b)`、`"未闭合`）
_HARD_SYNTAX = re.compile(r'[+\*^()"]')


#: 瞬时故障（不是查询写错）：这些 OperationalError **不允许**被"宽松重试"掩盖
_TRANSIENT_ERR = ("locked", "disk i/o", "unable to open database", "out of memory")


def _relax_query(query: str) -> str | None:
    """把含 `- : .` 的普通词包成 FTS5 短语，避免被当成语法运算符。

    实测（2026-11-04）：`search "agent-memory"` 会报 `no such column: reminder` ——
    因为 `-` 在 FTS5 里是运算符。这类词在本机语料里极常见（`demo-base`、`qwen3.5:9b`），
    必须能搜。返回 None = 不做宽松重试（含真·语法字符，应报错）。
    """
    parts = query.split()
    if not parts:
        return None
    out = []
    for part in parts:
        if _HARD_SYNTAX.search(part):
            return None
        out.append('"%s"' % part.replace('"', '""') if _RELAXABLE_CHARS.search(part) else part)
    return " ".join(out)


def _like_escape(s: str) -> str:
    """LIKE 模式里的通配符转义（配合 SQL 里的 `ESCAPE '\\'`）。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Store:
    def __init__(self, path: str, read_only: bool = False):
        """打开索引库。

        `read_only=True`（2026-11-17，MCP server 用）：以 SQLite `mode=ro` 打开，
        **不建目录、不 executescript(SCHEMA)、不 _migrate**。
        为什么必须分开（glm-5.3 复核 C14，已核实）：`__init__` 原本会建表并写
        `PRAGMA user_version`，在只读连接上启动即抛 "attempt to write a readonly database"。
        只读模式还把"误写"变成**响亮失败**——这是"这个 server 不会写库"的运行时证明。
        """
        self.path = path
        self.read_only = read_only
        if read_only:
            if not os.path.exists(path):
                raise FileNotFoundError("索引库不存在：%s（先跑 ingest）" % path)
            # Windows 路径要转正斜杠再拼 URI（与 premises.py 的既有写法一致）
            self.con = sqlite3.connect(
                "file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=15.0)
            self.con.row_factory = sqlite3.Row
            # schema 版本对账：ro 不能迁移，版本不一致就明确报错（别让旧代码读新库/新代码读旧库）
            cur = self.con.execute("PRAGMA user_version").fetchone()[0]
            if cur != SCHEMA_VERSION:
                raise RuntimeError(
                    "索引库 schema v%d 与代码 v%d 不一致：先跑一次 `am ingest`（或 am doctor 看提示）"
                    % (cur, SCHEMA_VERSION))
        else:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            self.con = sqlite3.connect(path)
            self.con.row_factory = sqlite3.Row
            self.con.executescript(SCHEMA)
            self._migrate()
        #: 最近一次 search() 实际走的路：fts5 / trigram / like（调用方要把它报给用户，
        #: 不能把「回退 LIKE」伪装成索引命中）
        self.last_search_via = ""
        #: 查询里含 `- : .` 时是否做了"包成短语"的宽松重试（CLI 要如实告知）
        self.last_query_relaxed = False
        #: 别名补检的说明（2026-11-06）：非空表示"主查询没打满，按 entities.json 换了同义写法
        #: 又补了几条"。CLI 必须打印它 —— 用户要知道结果里混进了别的词。
        self.last_alias_note = ""
        #: 某条别名补检抛了 QueryError（语法字符等）时的原因；单条失败不影响整次检索
        self.last_alias_error = ""
        #: 会话级补检的说明（2026-11-09）：非空表示"多词查询在单条消息内凑不齐，
        #: 已按会话交集补了几条"。CLI 必须打印 —— 这些是**跨消息**命中，不说明就是误导。
        self.last_cross_note = ""
        #: 子串命中缺口说明（2026-11-17 用户实测暴露）：FTS5 按 **token** 匹配，
        #: 而"质保"这种 2 字中文在语料里既可能独立成 token、也可能出现在"质保期"里。
        #: FTS 命中 94 条、LIKE 子串命中 325 条 —— 不说明就会把 94 当成"总量"。
        self.last_substring_note = ""

    def _migrate(self) -> None:
        """老库补列：`peer` 是 2026-11-04 新增的（git 派生的工作区身份）。

        `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列，所以必须显式 ALTER；
        幂等：只在该列缺失时执行。
        """
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(sessions)")}
        if "peer" not in cols:
            self.con.execute("ALTER TABLE sessions ADD COLUMN peer TEXT")
        # 索引放在这里（而不是 SCHEMA 里）：老库的 sessions 表还没有 peer 列，
        # executescript 阶段建索引会直接 `no such column: peer` 挂掉。
        self.con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_peer ON sessions(peer)")
        # 数据 schema 版本：库比代码新 → 直接拒绝（新结构旧代码读会给出错答案，比崩溃更糟）
        cur = self.con.execute("PRAGMA user_version").fetchone()[0]
        if cur > SCHEMA_VERSION:
            raise RuntimeError(
                "索引库 schema 版本 %d 高于本代码支持的 %d：请升级 agent-memory 包（别用旧代码读新库）"
                % (cur, SCHEMA_VERSION))
        if cur < SCHEMA_VERSION:
            self.con.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        self.con.commit()

    def schema_version(self) -> tuple[int, int]:
        """返回 (库里的版本, 本代码支持的版本)；供 doctor 与测试使用。"""
        cur = self.con.execute("PRAGMA user_version").fetchone()[0]
        return cur, SCHEMA_VERSION

    # ── 写入 ────────────────────────────────────────────────────────────
    def upsert_session(self, cr: CollectResult, peer: str = "") -> bool:
        """写入一个会话；已存在则跳过（幂等）。返回是否新写入。

        `peer` 由 `ingest()` 按 cwd 的 git origin 派生后传入（见 peers.py）；
        老调用方不传就是空串（= 无归属），不影响其它字段。
        """
        m: SessionMeta = cr.meta
        from collectors.base import session_key as _sk
        key = _sk(m.agent, m.session_id)
        exists = self.con.execute(
            "SELECT message_count FROM sessions WHERE session_key=?", (key,)).fetchone()
        if exists:
            # 静默跳过 ≠ 内容相同：CN/AI 两套日志里实测有 22 个同名 session（大小相同、
            # 字节不同）。message_count 不同就说明**不是同一份**，必须让人看见
            # （2026-11-04 glm-5.3 第 2 轮复核：原实现连这个都不区分）。
            old = exists["message_count"] or 0
            if old != len(cr.records):
                print("  [重复 key] %s 已存在（%d 条）但本次是 %d 条，保留先入库的那份"
                      % (key, old, len(cr.records)))
            return False
        self.con.execute(
            "INSERT INTO sessions (session_key,agent,session_id,title,cwd,started_at,"
            "ended_at,model,provider,parent_session,origin,source_path,peer,"
            "message_count,tool_call_count,indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, m.agent, m.session_id, m.title, m.cwd, m.started_at, m.ended_at,
             m.model, m.provider, m.parent_session, m.origin, m.source_path, peer or "",
             m.message_count, m.tool_call_count, _now()))
        for r in cr.records:
            cur = self.con.execute(
                "INSERT INTO messages (session_key,agent,role,ts,seq,text) VALUES (?,?,?,?,?,?)",
                (key, m.agent, r.role, r.ts, r.seq, r.text))
            self.con.execute("INSERT INTO messages_fts (rowid,text) VALUES (?,?)",
                             (cur.lastrowid, r.text))
            self.con.execute("INSERT INTO messages_trgm (rowid,text) VALUES (?,?)",
                             (cur.lastrowid, r.text))
        # 命令类工具调用（procedure 原料，2026-11-17）：挂在 session 级 upsert 里，
        # 与会话同生共死（已存在则跳过 ⇒ 天然幂等，不会重复落）。
        for tc in getattr(cr, "tool_calls", None) or []:
            self.con.execute(
                "INSERT INTO tool_calls (session_key,seq,ts,tool,command,ok,exit_code) "
                "VALUES (?,?,?,?,?,?,?)",
                (key, tc.seq, tc.ts, tc.tool, (tc.command or "")[:2000], tc.ok, tc.exit_code))
        self.con.commit()
        return True

    # ── 查询 ────────────────────────────────────────────────────────────
    _SEARCH_COLS = """m.id, m.session_key, m.agent, m.role, m.ts,
               substr(m.text, 1, 400) AS snippet,
               s.title, s.cwd, s.started_at, s.peer"""

    _SEARCH_SQL = """
        SELECT {cols}
        FROM {t} f
        JOIN messages m ON m.id = f.rowid
        JOIN sessions s ON s.session_key = m.session_key
        WHERE {t} MATCH ? {peer}
        ORDER BY rank
        LIMIT ?
        """

    def message_by_id(self, msg_id: int | str) -> dict | None:
        """按 `messages.id` 取**一条消息的全文**（`search` 只回 400 字截断）。

        用途：结论卡的证据里存 `msg` 锚点后，`cards open` 靠它把证据精确锚回原文 ——
        等价于 TencentDB Agent Memory 的 `node_id` 下钻（2026-11-09 调研）。
        找不到（跨机、库里被重建过）返回 `None`，由调用方如实说明，不许伪造。
        """
        try:
            mid = int(msg_id)
        except (TypeError, ValueError):
            return None
        row = self.con.execute(
            "SELECT m.id, m.session_key, m.agent, m.role, m.ts, m.seq, m.text, "
            "       s.title, s.cwd, s.started_at "
            "FROM messages m LEFT JOIN sessions s ON s.session_key = m.session_key "
            "WHERE m.id = ?", (mid,)).fetchone()
        if not row:
            return None
        keys = ("id", "session_key", "agent", "role", "ts", "seq", "text",
                "title", "cwd", "started_at")
        return dict(zip(keys, row))

    def id_mentions(self, ids, role: str = "assistant", since: str = "",
                    limit_per_id: int = 200, strict: bool = True) -> dict:
        """统计一组卡片 id 被**披露**的次数（记忆影响披露的机械核验，2026-11-19）。

        ## 为什么要有它

        「披露」如果只是靠模型自觉，就只是一句口号。卡片 id 是固定格式 `c-<sha8>`，
        `cards influence` 用它回答"注入过的卡到底被用了几次"。

        ## strict：只认**披露格式**，不认"顺带提到"（实测踩到的假阳性）

        披露要求是"回答开头用一行写「受 agent-memory 卡片 c-xxxxxxxx 影响」"。
        第一版按"全文任意出现 id"统计 —— 结果**讨论这套机制本身的回复**也被算成引用，
        实测出现 3 次假阳性（2026-11-19 重启后首次跑 `cards influence`）。
        所以默认 `strict=True`：只匹配**行首披露句式**；`strict=False` 保留任意提及口径，
        用于排查"到底在哪提过"，不是"被用了几次"。

        ## 为什么逐 id 走 LIKE 而不是 FTS

        id 里带 `-`，FTS5 会把它当语法字符；而单条 LIKE 只要 5~10 ms，几十张卡够用。
        strict 模式先用 `LIKE '%agent-memory 卡片%'` 粗筛、再用正则精确抽取行首 id，
        避免把整表读进 Python 再筛。

        返回：`{id: {"count", "sessions": {session_key: n}, "last_ts", "last_session"}}`；
        查不到就是 0，不抛。
        """
        rx = re.compile(
            r"^[ \t>*\-]*(?:本次)?[ \t]*受[ \t]*agent[\s-]*memory[ \t]*卡片[ \t]*(c-[0-9a-f]{8})",
            re.MULTILINE | re.IGNORECASE)
        out = {}
        ids = [str(x).strip() for x in (ids or []) if str(x).strip()]
        sql = ("SELECT m.session_key, m.ts, m.text, s.title FROM messages m "
               "LEFT JOIN sessions s ON s.session_key = m.session_key "
               "WHERE m.role = ? AND m.text LIKE ?" +
               (" AND m.ts >= ?" if since else "") +
               " ORDER BY m.ts DESC LIMIT ?")
        for cid in ids:
            args = [role, "%agent-memory 卡片%" if strict else ("%" + cid + "%")]
            if since:
                args.append(since)
            args.append(limit_per_id)
            rows = self.con.execute(sql, args).fetchall()
            if strict:
                want = cid.lower()
                rows = [r for r in rows
                        if want in {m.group(1).lower() for m in rx.finditer(r["text"] or "")}]
            sessions = {}
            last_ts = last_session = ""
            for r in rows:
                sk = r["session_key"]
                sessions[sk] = sessions.get(sk, 0) + 1
                if not last_ts:
                    last_ts = r["ts"] or ""
                    last_session = r["title"] or sk
            out[cid] = {"count": len(rows), "sessions": sessions,
                        "last_ts": last_ts, "last_session": last_session,
                        "truncated": len(rows) >= limit_per_id}
        return out

    def search(self, query: str, limit: int = 10, use_trigram: bool = False,
               like_fallback: bool = True, peer: str = "", alias_expand: bool = True,
               cross_message: bool = False, explain_substring: bool = False) -> list[dict]:
        """FTS5 检索；中文短词自动回退 LIKE 全表扫描。

        两条实测边界（2026-11-04，内存 FTS5 验证）：
          · `unicode61` 不切分 CJK ⇒ 默认索引**查不到任何中文子串**（含 3 字以上）；
          · `trigram` 只索引 3 字滑窗 ⇒ **1~2 字的中文词**（`质保`/`预算`）命不中。
        所以：FTS 零命中且查询含中文时回退 `LIKE '%词%'`（34k 条约十几 ms），
        并把 `last_search_via` 标成 `"like"`；空查询仍返回 `[]`（不算错误）；
        语法错误抛 `QueryError`，**不再**静默返回空列表。

        `peer` 非空时只检索该工作区身份下的会话（子串匹配，便于只写仓库名）。

        `alias_expand`（2026-11-06）：主查询命中**不足 limit** 时，按 `index/entities.json`
        的别名表把查询里出现的实体名换成同义写法再补一轮（结果按 `m.id` 去重追加在后面）。
        这解决的是"数据没丢、查询侧不认旧名"：`demo-kb` 在库里从没出现过，
        历史会话写的是 `demo-wiki`；实测 `search 'demo-kb'` 原来只 2 条逐字命中。
        主查询**不被改写**，别名只在命中不足时补，且 `last_alias_note` 会如实回报。

        ⚠️ 补检对标志的污染（2026-11-07 GLM 复核发现 1）：补检内部会再调 `_search_once`，
        而那条路径会写 `last_query_relaxed` / `last_search_via` —— 于是用户一句纯中文查询
        也会被告知「查询含 FTS5 语法字符」，或被告知「已回退 LIKE 全表扫描」，**都是假话**。
        现在补检前后**快照并恢复**这两个标志；补检自身的信息只经由 `last_alias_note` 汇报。
        """
        self.last_search_via = "trigram" if use_trigram else "fts5"
        self.last_query_relaxed = False
        self.last_alias_note = ""
        self.last_alias_error = ""
        self.last_cross_note = ""
        self.last_cross_hint = ""
        self.last_substring_note = ""
        if not query or not query.strip():
            return []
        rows = self._search_once(query, limit, use_trigram, like_fallback, peer)
        # 子串缺口计数要全表 LIKE 扫（46k 消息约 190ms），默认不算：
        # CLI 交互默认不付这个成本，agent（MCP）与 `--explain` 才来要这个"如实数"。
        if explain_substring:
            self.last_substring_note = self._substring_gap_note(query, len(rows), peer)

        # ── 跨消息补检（2026-11-09）：**默认关闭**，只在用户显式要求时开 ──
        # 实测：12 个双词查询里 10 个在"同一条消息内 AND"下 0 命中，而同一话题常常是
        # "一条问 + 一条答"合起来说全的（514 条相邻窗口 vs 82 条消息级命中）。
        # 但两个**通用词**（`超时` `回落`）的邻近共现仍会召回大量弱相关消息，
        # 所以默认不掺进结果：精确 AND 返回 0 是**诚实的结果**，塞弱相关条目才是误导。
        # 想跨消息找，加 `--cross-message`，结果会逐条标 `terms_matched` 与窗口跨度。
        toks = [t for t in query.split() if t]
        if cross_message and len(toks) >= 2 and len(rows) < limit:
            via_main, relaxed_main = self.last_search_via, self.last_query_relaxed
            extra = self._search_session_level(toks, limit - len(rows), peer)
            self.last_search_via = via_main
            self.last_query_relaxed = relaxed_main
            if extra:
                seen = {r["id"] for r in rows}
                added = [r for r in extra if r["id"] not in seen]
                if added:
                    rows = rows + added
                    self.last_cross_note = (
                        "跨消息补检：+%d 条（同一会话相邻 ≤%d 条消息内共现；"
                        "非单条消息命中）" % (len(added), self.MULTITERM_WINDOW))
            elif not rows:
                self.last_cross_note = "跨消息补检也无命中（相邻 ≤%d 条消息内没有共现）" \
                    % self.MULTITERM_WINDOW
        elif not cross_message and not rows and len(toks) >= 2:
            # 不静默：告诉用户"精确匹配就是 0"，并给出可选的下一步
            self.last_cross_hint = ("这些词没有在同一条消息里共现；"
                                    "若要按「同一会话相邻消息」再找一次，加 --cross-message")

        if not alias_expand or len(rows) >= limit:
            return rows
        # 主查询的"如实回报"必须保住：补检走的 _search_once 会改这两个标志
        via_main, relaxed_main = self.last_search_via, self.last_query_relaxed
        merged = self._search_alias_topup(query, rows, limit, use_trigram, like_fallback, peer)
        self.last_search_via = via_main
        self.last_query_relaxed = relaxed_main
        return merged

    def _search_once(self, query: str, limit: int, use_trigram: bool,
                     like_fallback: bool, peer: str = "") -> list[dict]:
        """单次检索（原 `search` 的全部逻辑，原样保留）。"""
        rows: list[dict] = []
        peer_sql = "AND s.peer LIKE ? ESCAPE '\\'" if peer else ""
        table = "messages_trgm" if use_trigram else "messages_fts"
        sql = self._SEARCH_SQL.format(cols=self._SEARCH_COLS, t=table, peer=peer_sql)

        def run(q):
            params = (q, "%" + _like_escape(peer) + "%", limit) if peer else (q, limit)
            return [dict(r) for r in self.con.execute(sql, params).fetchall()]

        try:
            rows = run(query)
        except sqlite3.OperationalError as exc:
            # 瞬时故障（锁/IO/内存）不许被重试掩盖 —— 要让调用方看见真故障
            if any(k in str(exc).lower() for k in _TRANSIENT_ERR):
                raise QueryError(query, str(exc)) from exc
            # 常见词的 `- : .` 会被 FTS5 当运算符（`agent-memory` → no such column 或 syntax error），
            # 先尝试"包成短语"再重试；真·语法字符（`C++`）仍按错误报出。
            relaxed = _relax_query(query)
            if not relaxed or relaxed == query:
                raise QueryError(query, str(exc)) from exc
            try:
                rows = run(relaxed)
                self.last_query_relaxed = True
            except sqlite3.OperationalError:
                raise QueryError(query, str(exc)) from exc
        if rows or not (like_fallback and _CJK_RE.search(query)):
            if not rows:
                return rows
        if rows:
            return rows
        # 中文多词：整句 LIKE 兜底多半无效（`%超时 回落%` 要求两词挨着），
        # 先按**词交集**做真正的 AND（2026-11-09 实测：`超时 回落` 0 → 17 条）
        toks = [t for t in query.split() if t]
        if len(toks) >= 2:
            and_rows = self._search_all_terms(toks, limit, peer=peer)
            if and_rows:
                self.last_search_via = "like-and"
                return and_rows
        hits = self._search_like(query, limit, peer=peer)
        if hits:
            self.last_search_via = "like"
        return hits

    def _substring_gap_note(self, query: str, hit_count: int, peer: str = "") -> str:
        """短中文词：FTS 按 token 命中，会漏掉"包含它作为子串"的消息，必须如实报数。

        2026-11-17 用户实测暴露：`质保` 走 FTS 命中 94 条，LIKE 子串命中 **325** 条
        （"质保期"/"质保金"里也含这两字）。不说明的话，调用方会把 94 当成"总量"——
        这正是本项目一直在防的"静默不完整"。

        只在**短中文查询**（≤3 字）且**已命中**时算一次 COUNT（实测 ~10 ms），
        避免给所有查询加无谓的全表扫描。
        """
        if not query or not _CJK_RE.search(query):
            return ""
        if len(query.strip()) > 3:
            return ""                       # 4 字以上走 trigram，子串差异小，不额外扫
        if hit_count <= 0:
            return ""                       # 零命中已由 LIKE 回退处理，不重复说
        try:
            if peer:
                sql = ("SELECT COUNT(*) FROM messages m JOIN sessions s "
                       "ON s.session_key = m.session_key "
                       "WHERE m.text LIKE ? ESCAPE '\\' AND s.peer LIKE ? ESCAPE '\\'")
                params = ("%" + _like_escape(query) + "%",
                          "%" + _like_escape(peer) + "%")
            else:
                # 不 join（实测 join 版 ~190ms → 纯 messages 版 ~20ms）：
                # 这一步只是"报个数"，不该让检索慢一个量级
                sql = "SELECT COUNT(*) FROM messages WHERE text LIKE ? ESCAPE '\\'"
                params = ("%" + _like_escape(query) + "%",)
            total = self.con.execute(sql, params).fetchone()[0]
        except sqlite3.Error:
            return ""                       # 计数失败不影响检索本身
        if total <= hit_count:
            return ""
        return ("另有 %d 条**子串命中**未包含（FTS 按 token 匹配：`%s` 独立成词才算；"
                "含在 `%s期`/`%s金` 这类词里的不算）。要子串级全量，用 `am search` 加长词"
                "或直接查库" % (total - hit_count, query, query, query))

    def _search_alias_topup(self, query: str, rows: list[dict], limit: int,
                            use_trigram: bool, like_fallback: bool, peer: str = "") -> list[dict]:
        """按别名表补一轮（只在主查询命中不足时跑）。"""
        try:
            import aliases
            groups = aliases.expansion(query)
        except Exception:  # noqa: BLE001
            return rows                     # 别名层坏掉不许影响主检索
        if not groups:
            return rows
        seen = {r.get("id") for r in rows}
        notes, extra = [], []
        for canon, names in groups:
            if len(rows) + len(extra) >= limit:
                break
            added_here = 0
            for name in names:
                if name.lower() in (query or "").lower():
                    continue                # 已经在主查询里，不必重复
                try:
                    got = self._search_once(name, limit, use_trigram, like_fallback, peer)
                except QueryError as exc:    # 单个别名语法错误不该弄挂整次检索
                    self.last_alias_error = str(exc)
                    continue
                for r in got:
                    if r.get("id") in seen:
                        continue
                    seen.add(r.get("id"))
                    extra.append(r)
                    added_here += 1
                    if len(rows) + len(extra) >= limit:
                        break
                if len(rows) + len(extra) >= limit:
                    break
            if added_here:
                notes.append("`%s` +%d 条" % (canon, added_here))
        if extra:
            # 后缀（同义写法来自哪儿）由调用方补，避免 CLI 与库层各写一遍导致重复
            self.last_alias_note = "别名补检：%s" % "、".join(notes)
        return rows + extra


    #: 多词补检的邻近窗口（条消息）。3 = 一问一答 + 一条追问，通常就是同一个话题。
    #: 实测：整会话共现会把两个常用词在几十条消息里"顺带都提过"当成相关（假阳性），
    #: `超时 回落` 整会话 27 个、K=3 只有 10 个，且样例确实是同一个问题。
    MULTITERM_WINDOW = 3

    def _search_session_level(self, tokens: list[str], limit: int, peer: str = "") -> list[dict]:
        """**多词邻近补检**：多词查询在"同一条消息内 AND"下大量为 0，但同一个话题
        常常是一条提问 + 一条回答合起来才说全 —— 语义单元是**相邻几条消息**，不是单条。

        2026-11-09 实测（12 个双词查询）：消息级 AND 合计 82 条，其中 **10 个查询 0 命中**；
        改成"整个会话任意位置共现"能到 478 个会话，但**假阳性严重**（一个长会话几十条消息，
        两个常用词各自出现过就被判相关，`超时 回落` 会召回卖库存货的会话）。
        最终取**同会话相邻 ≤ `MULTITERM_WINDOW` 条消息内共现**：`超时 回落` 27 → 10，
        样例确认是同一条问题（一条问超时、一条答回落）。

        每个词先用 LIKE 扫一次（单词语约 175 ms，只在主查询不足时才跑）。
        返回的行带 `cross_message=True` / `terms_matched` / `window_span`，
        调用方**必须**如实说明这些是跨消息命中。
        """
        toks = [t for t in tokens if t]
        if len(toks) < 2:
            return []
        # ⚠️ 这里不能用 `_search_like`：它的返回列里**没有 seq**，而邻近判断全靠 seq 距离
        #    （2026-11-09 被测试抓住：缺 seq ⇒ 所有行距离都当 0，窗口等于失效）
        peer_sql = "AND s.peer LIKE ? ESCAPE '\\'" if peer else ""
        sql = ("SELECT m.id, m.session_key, m.seq, m.ts FROM messages m "
               "JOIN sessions s ON s.session_key = m.session_key "
               "WHERE m.text LIKE ? ESCAPE '\\' {peer} LIMIT 4000").format(peer=peer_sql)
        per: dict[str, list[tuple]] = {}
        for t in toks:
            params = ("%" + _like_escape(t) + "%",
                      "%" + _like_escape(peer) + "%") if peer else ("%" + _like_escape(t) + "%",)
            rows = [(r[0], r[1], r[2], r[3]) for r in self.con.execute(sql, params)]
            if not rows:
                return []                     # 有一个词全库没有 ⇒ 交不出东西
            per[t] = rows

        # 按会话聚合：session_key -> [(seq, msg_id, ts, {命中的词})]
        by_sess: dict[str, list[tuple]] = {}
        for t, rows in per.items():
            for mid, sk, seq, ts in rows:
                by_sess.setdefault(sk, []).append((seq or 0, mid, ts, {t}))

        picked = []
        for sk, items in by_sess.items():
            items.sort(key=lambda x: x[0])
            seqs = [x[0] for x in items]
            best = None
            for i, start in enumerate(seqs):
                covered = set()
                for j in range(i, len(items)):
                    if items[j][0] - start > self.MULTITERM_WINDOW - 1:
                        break
                    covered |= items[j][3]
                    if len(covered) == len(toks):
                        best = (start, items[j][0], set(covered))
                        break
                if best:
                    break
            if not best:
                continue                      # 该会话只是"顺带都提过"，不算
            lo, hi, _cov = best
            win = [it for it in items if lo <= it[0] <= hi]
            rep = max(win, key=lambda it: (len(it[3]), it[0]))
            terms = set()
            for it in win:
                terms |= it[3]
            picked.append((rep[1], sorted(terms), hi - lo + 1))

        if not picked:
            return []
        placeholders = ",".join("?" * len(picked))
        sel = """SELECT {cols}
        FROM messages m
        JOIN sessions s ON s.session_key = m.session_key
        WHERE m.id IN ({ph})""".format(cols=self._SEARCH_COLS, ph=placeholders)
        meta = {mid: (terms, span) for mid, terms, span in picked}
        out = []
        for r in self.con.execute(sel, tuple(meta.keys())):
            row = dict(r)
            row["terms_matched"], row["window_span"] = meta[row["id"]]
            row["cross_message"] = True
            out.append(row)
        # 命中词多的优先；同分时窗口越窄越好（越像同一条讨论）
        out.sort(key=lambda r: (len(r["terms_matched"]), -r["window_span"]), reverse=True)
        return out[:limit]

    def _search_all_terms(self, tokens: list[str], limit: int, peer: str = "") -> list[dict]:
        """**中文多词 AND（按词 LIKE 交集）**——修 2026-11-09 的实测缺口。

        为什么必须有这条：`unicode61` 把一整段连续 CJK 当一个 token，所以 `超时` 只在
        "该 token 恰好等于超时"时命中；而 `LIKE` 回退用的是**整句** `%超时 回落%`。
        于是"两个词在同一条消息里、但不挨着"这种情况**两条路都查不到**——实测
        `超时 回落` 消息级 0 条、按词交集 **17 条**；`复核 报告` 7 → **440 条**。

        语义与 FTS5 的 AND 完全一致（同一消息内所有词都出现），**没有放宽相关性**，
        只是换了个能匹配中文子串的实现。成本约 160 ms/词，只在 FTS 命中不足时跑。
        """
        toks = [t for t in tokens if t]
        if len(toks) < 2:
            return []
        peer_sql = "AND s.peer LIKE ? ESCAPE '\\'" if peer else ""
        ids = None
        for t in toks:
            sql = ("SELECT m.id FROM messages m JOIN sessions s ON s.session_key = m.session_key "
                   "WHERE m.text LIKE ? ESCAPE '\\' {peer}").format(peer=peer_sql)
            params = ("%" + _like_escape(t) + "%",
                      "%" + _like_escape(peer) + "%") if peer else ("%" + _like_escape(t) + "%",)
            cur = {r[0] for r in self.con.execute(sql, params)}
            ids = cur if ids is None else (ids & cur)
            if not ids:
                return []
        placeholders = ",".join("?" * len(ids))
        sql = """SELECT {cols}
        FROM messages m
        JOIN sessions s ON s.session_key = m.session_key
        WHERE m.id IN ({ph})
        ORDER BY m.ts DESC
        LIMIT ?""".format(cols=self._SEARCH_COLS, ph=placeholders)
        out = [dict(r) for r in self.con.execute(sql, tuple(ids) + (limit,))]
        for r in out:
            r["terms_matched"] = toks
            r["cross_message"] = False        # 单条消息内命中，不是跨消息
        return out

    def _search_like(self, query: str, limit: int, peer: str = "") -> list[dict]:
        """中文 1~2 字词的兜底：FTS5 两个索引都覆盖不到，只能全表 LIKE。

        排序按 `m.ts` 倒序（LIKE 没有相关性分）；这是**回退**，不是主路，
        调用方必须把 `last_search_via == "like"` 如实报出来。
        """
        peer_sql = "AND s.peer LIKE ? ESCAPE '\\'" if peer else ""
        sql = """
        SELECT {cols}
        FROM messages m
        JOIN sessions s ON s.session_key = m.session_key
        WHERE m.text LIKE ? ESCAPE '\\' {peer}
        ORDER BY m.ts DESC
        LIMIT ?
        """.format(cols=self._SEARCH_COLS, peer=peer_sql)
        params = (("%" + _like_escape(query) + "%",
                   "%" + _like_escape(peer) + "%", limit) if peer
                  else ("%" + _like_escape(query) + "%", limit))
        try:
            return [dict(r) for r in self.con.execute(sql, params).fetchall()]
        except sqlite3.OperationalError as exc:
            # 回退路上的真错误（锁/IO）不许伪装成"没命中"（2026-11-04 glm-5.3 第 2 轮复核）
            raise QueryError(query, str(exc)) from exc

    def stats(self) -> dict:
        """统计**以入库条数为准**（`messages` 表），不用 sessions.message_count ——
        后者对 Hermes 来自导出 frontmatter、含工具消息，与"入索引的 user/assistant 条数"
        不是同一个口径，混用会让人以为索引漏了数据。"""
        rows = self.con.execute(
            "SELECT s.agent AS agent, COUNT(DISTINCT s.session_key) AS sessions, "
            "COUNT(m.id) AS msgs "
            "FROM sessions s LEFT JOIN messages m ON m.session_key = s.session_key "
            "GROUP BY s.agent ORDER BY -sessions").fetchall()
        total_msg = self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        pr = self.con.execute(
            "SELECT COUNT(DISTINCT peer) AS distinct_peers, COUNT(*) AS covered "
            "FROM sessions WHERE peer IS NOT NULL AND peer<>''").fetchone()
        return {"by_agent": [dict(r) for r in rows], "messages": total_msg,
                "sessions": self.con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "peers": {"distinct": pr["distinct_peers"], "sessions": pr["covered"]}}

    def peer_summary(self, limit: int = 20) -> list[dict]:
        """每个工作区身份（peer）的会话数、时间跨度与代表 cwd；空 peer 不列。"""
        return [dict(r) for r in self.con.execute("""
            SELECT s1.peer AS peer, COUNT(*) AS sessions,
                   MIN(s1.started_at) AS first_at, MAX(s1.started_at) AS last_at,
                   (SELECT s2.cwd FROM sessions s2
                    WHERE s2.peer=s1.peer AND s2.cwd IS NOT NULL AND s2.cwd<>''
                    GROUP BY s2.cwd ORDER BY COUNT(*) DESC LIMIT 1) AS top_cwd
            FROM sessions s1
            WHERE s1.peer IS NOT NULL AND s1.peer<>''
            GROUP BY s1.peer ORDER BY sessions DESC LIMIT ?""", (limit,))]

    def backfill_peers(self, peer_fn, only_missing: bool = True) -> dict:
        """给**已入库**的会话补 peer（幂等；`ingest` 对已存在会话会跳过）。

        实测必要性：peer 是 2026-11-04 新加的能力，555 个存量会话的 cwd 都还在，
        但没有 peer 列 —— 不补就得 `rmdir index` 重建 45 秒。
        """
        where = " WHERE peer IS NULL OR peer=''" if only_missing else ""
        rows = self.con.execute("SELECT session_key, cwd FROM sessions" + where).fetchall()
        cache: dict = {}
        updated = 0
        for r in rows:
            cwd = r["cwd"] or ""
            if cwd not in cache:
                try:
                    cache[cwd] = peer_fn(cwd)
                except Exception:                              # noqa: BLE001
                    cache[cwd] = ""
            p = cache[cwd]
            if p:
                self.con.execute("UPDATE sessions SET peer=? WHERE session_key=?",
                                 (p, r["session_key"]))
                updated += 1
        self.con.commit()
        return {"scanned": len(rows), "updated": updated}

    def close(self):
        self.con.close()

    # ── 话题归并（第二阶段 A）────────────────────────────────────────────
    def set_topics(self, session_key: str, entities, intents) -> None:
        """写入一个会话的实体/意图标签（先删后插，保证重跑幂等）。

        元素可以是 `str` 或 `(str, hits)`；hits 用于按"提及强度"排序，
        没有就记 1（老调用方兼容）。
        """
        self.con.execute("DELETE FROM session_topics WHERE session_key=?", (session_key,))
        for kind, labels in (("entity", entities), ("intent", intents)):
            for item in labels:
                label, hits = item if isinstance(item, (tuple, list)) else (item, 1)
                self.con.execute(
                    "INSERT OR REPLACE INTO session_topics (session_key,kind,label,hits) "
                    "VALUES (?,?,?,?)", (session_key, kind, label, int(hits)))

    def topic_pairs(self, min_sessions: int = 2) -> list[dict]:
        """按 (实体 × 意图) 聚合：会话数 + 时间跨度。时间线(B) 的输入。"""
        rows = self.con.execute("""
        SELECT e.label AS entity, i.label AS intent,
               COUNT(DISTINCT e.session_key) AS sessions,
               MIN(s.started_at) AS first_at, MAX(s.started_at) AS last_at
        FROM session_topics e
        JOIN session_topics i ON i.session_key = e.session_key AND i.kind='intent'
        JOIN sessions s ON s.session_key = e.session_key
        WHERE e.kind='entity'
        GROUP BY e.label, i.label
        HAVING sessions >= ?
        ORDER BY sessions DESC, entity
        """, (min_sessions,)).fetchall()
        return [dict(r) for r in rows]

    def topic_summary(self, kind: str) -> list[dict]:
        rows = self.con.execute("""
        SELECT t.label AS label, COUNT(DISTINCT t.session_key) AS sessions,
               MIN(s.started_at) AS first_at, MAX(s.started_at) AS last_at
        FROM session_topics t JOIN sessions s ON s.session_key = t.session_key
        WHERE t.kind=?
        GROUP BY t.label ORDER BY sessions DESC
        """, (kind,)).fetchall()
        return [dict(r) for r in rows]

    def coverage(self) -> dict:
        """标签覆盖率：多少会话至少有一个实体 / 一个意图（衡量词典是否够用）。"""
        total = self.con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        ent = self.con.execute(
            "SELECT COUNT(DISTINCT session_key) FROM session_topics WHERE kind='entity'"
        ).fetchone()[0]
        itn = self.con.execute(
            "SELECT COUNT(DISTINCT session_key) FROM session_topics WHERE kind='intent'"
        ).fetchone()[0]
        return {"sessions": total, "with_entity": ent, "with_intent": itn}

    def set_paths(self, session_key: str, dirs) -> None:
        """写入该会话的工具路径（dirs 为 [(dir, hits)]，先删后插保证幂等）。"""
        self.con.execute("DELETE FROM session_paths WHERE session_key=?", (session_key,))
        for item in dirs or ():
            d, hits = item if isinstance(item, (tuple, list)) else (item, 1)
            self.con.execute(
                "INSERT OR REPLACE INTO session_paths (session_key,dir,hits) VALUES (?,?,?)",
                (session_key, d, int(hits)))

    def paths_for(self, session_key: str) -> list[tuple[str, int]]:
        return [(r[0], r[1]) for r in self.con.execute(
            "SELECT dir, hits FROM session_paths WHERE session_key=? ORDER BY hits DESC",
            (session_key,))]

    def sessions_for(self, entity: str, intent: str = "") -> list[dict]:
        """某实体（可再限定意图）下的会话，**按时间排序** —— 演变时间线(B) 的地基。"""
        if intent:
            sql = """
            SELECT s.session_key, s.agent, s.title, s.started_at, s.cwd
            FROM sessions s
            JOIN session_topics e ON e.session_key=s.session_key AND e.kind='entity'
            JOIN session_topics i ON i.session_key=s.session_key AND i.kind='intent'
            WHERE e.label=? AND i.label=?
            ORDER BY s.started_at
            """
            rows = self.con.execute(sql, (entity, intent)).fetchall()
        else:
            sql = """
            SELECT s.session_key, s.agent, s.title, s.started_at, s.cwd
            FROM sessions s
            JOIN session_topics e ON e.session_key=s.session_key AND e.kind='entity'
            WHERE e.label=?
            ORDER BY s.started_at
            """
            rows = self.con.execute(sql, (entity,)).fetchall()
        return [dict(r) for r in rows]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ingest(store: Store, results: Iterable[CollectResult], verbose: bool = False,
           peer_fn=None) -> dict:
    """批量写入。返回统计。

    `peer_fn(cwd) -> str` 决定每个会话的工作区身份（默认 `peers.peer_for_cwd`，
    测试可注入桩）。cwd 相同只算一次 —— 555 个会话实测极少有 555 个不同的 cwd。
    """
    added = skipped = 0
    by_agent: dict[str, int] = {}
    peer_cache: dict[str, str] = {}
    if peer_fn is None:
        from peers import peer_for_cwd
        peer_fn = peer_for_cwd
    t0 = time.perf_counter()
    for cr in results:
        try:
            from collectors.base import session_key as _sk2
            key2 = _sk2(cr.meta.agent, cr.meta.session_id)
            store.set_paths(key2, cr.tool_dirs)      # 路径每次都更新（不依赖新增）
            cwd = cr.meta.cwd or ""
            if cwd not in peer_cache:
                try:
                    peer_cache[cwd] = peer_fn(cwd)
                except Exception:                    # noqa: BLE001
                    peer_cache[cwd] = ""
            if store.upsert_session(cr, peer=peer_cache[cwd]):
                added += 1
                by_agent[cr.meta.agent] = by_agent.get(cr.meta.agent, 0) + 1
            else:
                skipped += 1
        except Exception as e:                            # noqa: BLE001
            print("  写入失败 %s/%s: %s: %s"
                  % (cr.meta.agent, cr.meta.session_id, type(e).__name__, e))
    return {"added": added, "skipped": skipped, "by_agent": by_agent,
            "sec": round(time.perf_counter() - t0, 1)}
