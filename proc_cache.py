"""procedure 缓存：把 9 秒级的挖掘结果预计算，供 MCP 工具秒级读取。

## 为什么必须有这一层（2026-11-17 实测）

| 操作 | 耗时 |
|---|---|
| `mine_repairs`（全量） | **9.1 s** |
| `mine_rituals`（全量） | 2.7 s |
| `select_for_entity`（= 上面两者 + judge） | **11.8 s** |

MCP 工具每次调用都现挖 = 单次 12 秒，不可用（glm-5.3 复核 C19 指出，实测更严重）。
所以：**日更时预计算一次**，工具读缓存；缓存缺失/过期才现算，并如实标注"数据可能陈旧"。

## 缓存内容与失效

`index/procedures-cache.json`：
  built_at      预计算时刻
  db_mtime      当时索引库的 mtime（库被重写即视为过期）
  tool_calls    当时的 tool_calls 行数（轻量一致性信号）
  rituals       全量仪式（含 cwd / sessions / shape / sample）
  repairs       全量修复对（含 cwd / sessions / raw_hits / sim / samples）

**过期判据**（任一即过期）：文件缺失 / JSON 坏 / `built_at` 超过 TTL（默认 24h）/
`db_mtime` 或 `tool_calls` 与当前库不一致。

## 为什么缓存"全量"而不是"按实体"

实体归属（live dirs / dir_entity_map）会随时变（目录改名、项目搬家），缓存按实体切就必然
和归属漂移打架。缓存**与归属无关的全量挖掘结果**，归属在调用时算（毫秒级），
这样缓存不会"把旧归属固化下来"。
"""
from __future__ import annotations

import json
import os
import time

from paths import DATA

CACHE_FILE = os.path.join(DATA, "procedures-cache.json")

#: 默认 TTL（小时）。日更是每天一次，24h 对齐；调小会让工具频繁现算（12 秒）
DEFAULT_TTL_HOURS = 24


def _db_signals(db_path: str) -> dict:
    """库的两个轻量指纹（mtime + tool_calls 行数）。"""
    sig = {"db_mtime": 0, "tool_calls": 0}
    try:
        sig["db_mtime"] = int(os.path.getmtime(db_path))
    except OSError:
        return sig
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
        sig["tool_calls"] = con.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        con.close()
    except Exception:                                        # noqa: BLE001
        pass
    return sig


def is_fresh(doc: dict | None, db_path: str, ttl_hours: float = DEFAULT_TTL_HOURS) -> tuple[bool, str]:
    """返回 (是否新鲜, 原因)。原因要能直接报给调用方（不能静默用陈旧数据）。"""
    if not doc:
        return False, "缓存不存在"
    built = doc.get("built_at") or ""
    try:
        age_h = (time.time() - time.mktime(time.strptime(built, "%Y-%m-%dT%H:%M:%S"))) / 3600.0
    except (ValueError, OverflowError):
        return False, "built_at 无法解析"
    if age_h > ttl_hours:
        return False, "缓存已过期 %.1f 小时（TTL %.0f）" % (age_h, ttl_hours)
    sig = _db_signals(db_path)
    if doc.get("db_mtime") and sig["db_mtime"] and doc["db_mtime"] != sig["db_mtime"]:
        return False, "索引库已更新（mtime 变了）"
    if doc.get("tool_calls") is not None and sig["tool_calls"] != doc.get("tool_calls"):
        return False, "tool_calls 行数变了（%s → %s）" % (doc.get("tool_calls"), sig["tool_calls"])
    return True, "新鲜（%.1f 小时）" % age_h


def load() -> dict | None:
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or "rituals" not in doc or "repairs" not in doc:
        return None
    return doc


def save(doc: dict) -> str:
    parent = os.path.dirname(os.path.abspath(CACHE_FILE))
    os.makedirs(parent, exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, CACHE_FILE)
    return CACHE_FILE


def build(con, db_path: str) -> dict:
    """全量挖掘并落盘。**只应在日更/手动刷新时调用**（9~12 秒）。"""
    import procedures

    t0 = time.perf_counter()
    rituals = procedures.mine_rituals(con, min_sessions=3)
    repairs = procedures.mine_repairs(con)
    sig = _db_signals(db_path)
    doc = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "version": 1,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "db_mtime": sig["db_mtime"],
        "tool_calls": sig["tool_calls"],
        "rituals": rituals,
        "repairs": repairs,
    }
    save(doc)
    return doc


def ensure(con, db_path: str, ttl_hours: float = DEFAULT_TTL_HOURS,
           allow_build: bool = True) -> tuple[dict, str]:
    """取缓存：新鲜就直接用；不新鲜且允许现算则重建。

    返回 (doc, note)。note 必须带回给调用方（"现算 11 秒"/"用的是陈旧缓存"都要说）。
    """
    doc = load()
    fresh, why = is_fresh(doc, db_path, ttl_hours)
    if fresh:
        return doc, "缓存命中：%s" % why
    if not allow_build:
        if doc:
            return doc, "缓存%s，但仍返回（调用方需知道数据可能陈旧）" % why
        raise RuntimeError("procedure 缓存缺失且不允许现算：先跑 `am procedure --refresh-cache`")
    doc = build(con, db_path)
    return doc, "缓存%s ⇒ 已现算（耗时 %.1f s）" % (why, doc["elapsed_ms"] / 1000.0)
