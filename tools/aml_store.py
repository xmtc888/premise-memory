"""AML 本地自测适配器的存储层（独立评测库，不碰主库 index/sessions.db）。

## 为什么独立建库

AML 的 `user_id` 是**评测作用域**（形如 `eval:<run>:locomo:conv-0`），语义与主库的
`session_key` 完全不同；混用会让一次自测把主库写脏，也做不到契约要求的
"跨 user_id 不得检索"。所以这里另开一个库（默认 `work/aml/eval.db`）。

## 检索策略：与主库同构（不是另一套算法）

主库实测结论直接沿用（见 store.py 文档）：
  · `unicode61` **不切分 CJK** ⇒ 中文子串查不到；
  · `trigram` 按 3 字滑窗 ⇒ 3 字以上中文能命中，1~2 字命中不了 ⇒ 回退 LIKE。
这里建同样的双 FTS5 索引 + 同样的 LIKE 回退，**语义与主库一致**，
这样自测分数才代表"我们这套东西的真实能力"，而不是适配器另写的一套。

## 隔离

每条记忆带 `user_id`，所有查询强制 `WHERE user_id=?`；进程内不做任何跨用户缓存。
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import uuid

#: 与 store.py 一致：CJK 判定决定要不要走 LIKE 回退
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
#: FTS5 语法字符：出现就不做"包短语"重试（与 store 的宽松重试策略对齐）
_HARD_SYNTAX = re.compile(r'[+\*^()"]')
_RELAXABLE = re.compile(r"[-:.]")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,          -- AML 契约里的 data[].id（稳定标识）
    user_id     TEXT NOT NULL,             -- 评测作用域，强隔离键
    session_id  TEXT NOT NULL,             -- 来源会话（仅组织用，不作筛选条件）
    request_id  TEXT NOT NULL,             -- Add 的 request_id（幂等/回显）
    role        TEXT NOT NULL,
    ts          INTEGER,                   -- Unix 毫秒（契约字段，可选）
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL              -- ISO8601（契约字段，可选）
);
CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_mem_req ON memories(request_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, content='memories', content_rowid='rowid', tokenize='unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_trgm USING fts5(
    content, content='memories', content_rowid='rowid', tokenize='trigram'
);

-- Add 幂等：同一 request_id 重复写入不重复落（平台会"有界重试"同一 request_id）
CREATE TABLE IF NOT EXISTS add_requests (
    request_id  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    chunks      INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _relax(query: str) -> str | None:
    """把含 `- : .` 的普通词包成 FTS5 短语（与 store._relax_query 同策略）。

    返回 None = 含真语法字符，不做宽松重试。
    """
    parts = [p for p in query.split() if p]
    if not parts:
        return None
    out = []
    for p in parts:
        if _HARD_SYNTAX.search(p):
            return None
        out.append('"%s"' % p.replace('"', '""') if _RELAXABLE.search(p) else p)
    return " ".join(out)


class AmlStore:
    def __init__(self, path: str, threaded: bool = True):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # threaded=True：HTTP 适配器是多线程的（ThreadingHTTPServer），而 sqlite3 连接
        # 默认禁止跨线程使用。这里开 check_same_thread=False 并**用一把锁串行化**，
        # 保证同一时刻只有一个线程在碰这个连接（WAL 下读多写少，够用且简单）。
        self._lock = threading.Lock()
        self.con = sqlite3.connect(path, timeout=15.0,
                                   check_same_thread=not threaded)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    # ── Add ─────────────────────────────────────────────────────────────
    def add(self, request_id: str, user_id: str, session_id: str,
            messages: list[dict]) -> dict:
        """写入一批消息；**返回前已持久化且可检索**（契约要求）。

        幂等：同一 request_id 第二次进来直接认作已写入（平台会重试同一 request_id）。
        """
        with self._lock:
            return self._add_locked(request_id, user_id, session_id, messages)

    def _add_locked(self, request_id, user_id, session_id, messages) -> dict:
        row = self.con.execute(
            "SELECT request_id FROM add_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            # 契约：成功响应必须原样回显三个 ID；重复请求也按成功返回
            return {"success": True, "request_id": request_id,
                    "user_id": user_id, "session_id": session_id, "duplicate": True}

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        n = 0
        for m in messages:
            content = (m.get("content") or "")
            if not isinstance(content, str) or not content.strip():
                continue                      # 契约：content 必须非空
            mid = "mem_%s" % uuid.uuid4().hex[:16]
            ts = m.get("timestamp")
            if not isinstance(ts, int):
                ts = None
            self.con.execute(
                "INSERT INTO memories (id,user_id,session_id,request_id,role,ts,content,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (mid, user_id, session_id, request_id, (m.get("role") or "user"),
                 ts, content, now_iso),
            )
            rowid = self.con.execute(
                "SELECT rowid FROM memories WHERE id=?", (mid,)
            ).fetchone()[0]
            self.con.execute("INSERT INTO memories_fts (rowid,content) VALUES (?,?)",
                             (rowid, content))
            self.con.execute("INSERT INTO memories_trgm (rowid,content) VALUES (?,?)",
                             (rowid, content))
            n += 1
        self.con.execute(
            "INSERT INTO add_requests (request_id,user_id,session_id,chunks,created_at)"
            " VALUES (?,?,?,?,?)", (request_id, user_id, session_id, n, now_iso))
        self.con.commit()
        return {"success": True, "request_id": request_id, "user_id": user_id,
                "session_id": session_id, "stored": n}

    # ── Search ──────────────────────────────────────────────────────────
    def search(self, query: str, user_id: str, top_k: int = 100) -> list[dict]:
        """返回按相关性排序的记忆证据（契约：只回证据，不生成答案）。

        顺序即相关度；调用方**不得**超过 top_k 条（超量判契约错误，不静默截断）。
        """
        q = (query or "").strip()
        if not q:
            return []
        with self._lock:
            rows = self._search_fts(q, user_id, top_k)
            if not rows and _CJK_RE.search(q) and len(q) <= 2:
                # 1~2 字中文在 trigram 上命不中、unicode61 不切 CJK ⇒ LIKE 回退（同主库）
                rows = self._search_like(q, user_id, top_k)
            if not rows and _CJK_RE.search(q):
                rows = self._search_like(q, user_id, top_k)
            return rows

    def _search_fts(self, q: str, user_id: str, top_k: int) -> list[dict]:
        attempts = [q]
        relaxed = _relax(q)
        if relaxed and relaxed != q:
            attempts.append(relaxed)
        for attempt in attempts:
            for table in ("memories_fts", "memories_trgm"):
                try:
                    rows = self.con.execute(
                        "SELECT m.id, m.content, m.created_at, m.ts "
                        "FROM {t} f JOIN memories m ON m.rowid = f.rowid "
                        "WHERE {t} MATCH ? AND m.user_id = ? "
                        "ORDER BY rank LIMIT ?".format(t=table),
                        (attempt, user_id, top_k),
                    ).fetchall()
                except sqlite3.OperationalError:
                    continue                  # 语法错误：换下一次尝试（不静默当成 0 命中）
                if rows:
                    return [self._to_item(r, rank=i) for i, r in enumerate(rows)]
        return []

    def _search_like(self, q: str, user_id: str, top_k: int) -> list[dict]:
        pat = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.con.execute(
            "SELECT id, content, created_at, ts FROM memories "
            "WHERE user_id=? AND content LIKE ? ESCAPE '\\' LIMIT ?",
            (user_id, pat, top_k)).fetchall()
        return [self._to_item(r, rank=i) for i, r in enumerate(rows)]

    @staticmethod
    def _to_item(row, rank: int = 0) -> dict:
        """映射成契约要求的 data[] 条目；score 越大越相关（按返回位置给递减分）。"""
        return {
            "id": row["id"],
            "content": row["content"],
            "score": round(max(0.0, 1.0 - rank * 0.01), 4),
            "created_at": row["created_at"],
        }

    # ── 自测辅助 ────────────────────────────────────────────────────────
    def stats(self) -> dict:
        with self._lock:
            total = self.con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            users = self.con.execute(
                "SELECT COUNT(DISTINCT user_id) FROM memories").fetchone()[0]
        return {"memories": total, "users": users}

    def reset(self) -> None:
        """清空（只用于自测；契约要求不得跨 run 复用）"""
        with self._lock:
            for t in ("memories_fts", "memories_trgm"):
                self.con.execute("INSERT INTO {t}({t}) VALUES('delete-all')".format(t=t))
            self.con.execute("DELETE FROM memories")
            self.con.execute("DELETE FROM add_requests")
            self.con.commit()


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else r"F:\agent-memory\work\aml\eval.db"
    st = AmlStore(db)
    print("stats:", st.stats())
    st.close()
