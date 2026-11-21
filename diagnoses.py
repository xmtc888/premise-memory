"""人类诊断的落盘层：procedure 修复对的「人话解释」。

## 为什么要这一步（WASP-Mem, arXiv 2611.04003 的实测结论）

论文在 TheAgentCompany 上实测出的分工是：
  · **人定的 schema / 去重规则会拖低性能**（"prescriptive knowledge organization hurts"）；
  · **一轮人类诊断胜过四轮自判 7.0~10.9%**；
  · 结论：**agent 负责组织，人类负责诊断**。

本仓现状：`mine_repairs` 用规则自动判定"失败→改正"，本质就是"prescriptive"路线。
这层的作用是把自动判定的结果**交回给人诊断**：
  1. `am diagnose list` 列出待诊断候选（带稳定 id）；
  2. 人写一句"当时为什么错、该怎么改"；
  3. `am diagnose add <id> "<人话>"` 落盘；
  4. 已诊断的条目才允许进注入块（且注入时标明"人已诊断"）。

## 为什么 id 要稳定

诊断是**人对某条候选**说的话，候选每次重新挖掘都会重算。若 id 随排序/次数变化，
昨天写的诊断今天就对不上了。所以 id 由**归一化形态对**哈希得出：
同一对命令（不论出现几次、在哪个会话）永远同一个 id。

## 落盘

`index/procedure-diagnoses.json`（本机数据目录，gitignore，换机复制即迁移）。
写文件用"临时文件 + 原子替换"，避免写一半断电留下坏 JSON。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from paths import DATA

#: 诊断文件（与 cards.jsonl 同属本机数据，不进版本库）
DIAGNOSES_FILE = os.path.join(DATA, "procedure-diagnoses.json")

SCHEMA_VERSION = 1


def stable_id(bad: str, good: str) -> str:
    """由归一化形态对算稳定 id（同一对命令永远同一个 id）。"""
    h = hashlib.sha1(("%s\x00%s" % (bad, good)).encode("utf-8")).hexdigest()
    return "rp_%s" % h[:12]


def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "items": {}}


def load(path: str = "") -> dict:
    """读诊断文件；不存在/坏掉都返回空结构（诊断是增量数据，坏了不该阻塞主流程）。"""
    p = path or DIAGNOSES_FILE
    if not os.path.exists(p):
        return _empty()
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), dict):
        return _empty()
    doc.setdefault("version", SCHEMA_VERSION)
    return doc


def save(doc: dict, path: str = "") -> str:
    """原子写（临时文件 + os.replace），避免半截 JSON。"""
    p = path or DIAGNOSES_FILE
    parent = os.path.dirname(os.path.abspath(p))
    os.makedirs(parent, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
    return p


def record(item: dict, diagnosis: str, by: str = "user", path: str = "") -> dict:
    """记录一条人类诊断。

    `item` 是 `mine_repairs` 的候选（需含 bad/good/sample_bad/sample_good/cwd）。
    `diagnosis` 是人写的原话，**不做任何改写**（改写就等于又用规则替代人）。
    """
    text = (diagnosis or "").strip()
    if not text:
        raise ValueError("诊断内容不能为空")
    doc = load(path)
    rid = item.get("id") or stable_id(item.get("bad") or "", item.get("good") or "")
    rec = {
        "id": rid,
        "bad": item.get("bad") or "",
        "good": item.get("good") or "",
        "sample_bad": (item.get("sample_bad") or "")[:400],
        "sample_good": (item.get("sample_good") or "")[:400],
        "cwd": item.get("cwd") or "",
        "diagnosis": text,
        "diagnosed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "by": by,
    }
    doc["items"][rid] = rec
    save(doc, path)
    return rec


def get(rid: str, path: str = "") -> dict | None:
    return load(path)["items"].get(rid)


def all_items(path: str = "") -> list[dict]:
    items = list(load(path)["items"].values())
    items.sort(key=lambda r: r.get("diagnosed_at") or "", reverse=True)
    return items


def stats(path: str = "") -> dict:
    items = all_items(path)
    return {"file": path or DIAGNOSES_FILE, "total": len(items)}
