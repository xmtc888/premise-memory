# -*- coding: utf-8 -*-
"""实体别名解析（单一事实源）：把「用户写的那串名字」换成 `session_topics.label` 里的写法。

## 为什么需要它

`index/entities.json` 里的别名表**早就有了**（`canonical -> [别名…]`），但它此前只被
`topics.load_entities()` 读；`timeline` / `search` 拿字面串直接查 `session_topics.label`，
于是同一个话题换个写法就查不到。2026-11-06 在本机实测（1,281 个会话的库上）：

    timeline '示例知识库' '架构与选型'  → 64 个会话 / 抽到 21 条结论
    timeline 'demo-kb'   '架构与选型'  →  0 个会话 /  0 条结论
    search   'demo-kb'                →  只 2 条逐字命中（历史会话写的是 demo-wiki）

项目改名（`demo-wiki → demo-kb`）之后这条尤其致命：**数据一条没丢，是查询侧不认旧名。**

## 设计约束

* 只读 JSON，**不 import topics** —— 避免把 jieba 等技术栈拖进检索冷路径；
* 按 `mtime` 缓存：手改 `entities.json` 立刻生效，不用重启进程；
* 文件缺失/损坏 ⇒ **静默退化为空表**，检索照常工作（新机器还没跑过 `cli.py init`
  时就是这种状态，此时等价于「没有别名知识」，不是错误）；
* 解析结果**显式回报**给调用方（`describe()`）。命令行必须如实打印「按别名 A → B 解析」，
  不许静默改写用户的查询 —— 否则用户会以为库里真有这个词。
"""
from __future__ import annotations

import json
import os

#: (path, mtime, alias_to_canon, canon_to_aliases)
_CACHE: tuple = ("", 0.0, {}, {})

#: 自由文本里做别名替换时的**最短**别名长度。2 字别名（如「明珠」）在长查询里极易误撞，
#: 只允许显式的实体参数命中它；自由文本至少 3 字才参与。
MIN_FREE_TEXT_ALIAS = 3


def _entities_file() -> str:
    try:
        from paths import ENTITIES_FILE  # 延迟导入：单测可替换
        return ENTITIES_FILE
    except Exception:  # noqa: BLE001
        return ""


def _load(path: str = "") -> tuple[dict, dict]:
    """读 `entities.json`，返回 `(alias_to_canon, canon_to_aliases)`（均小写键）。"""
    global _CACHE
    path = path or _entities_file()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    if _CACHE[0] == path and _CACHE[1] == mtime:
        return _CACHE[2], _CACHE[3]

    a2c: dict[str, str] = {}
    c2a: dict[str, list[str]] = {}
    if path and mtime:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            ents = doc.get("entities") or {}
            for canon, aliases in ents.items():
                if not canon:
                    continue
                names = [canon] + [a for a in (aliases or []) if a]
                c2a[canon] = names
                for n in names:
                    a2c.setdefault(n.strip().lower(), canon)
        except Exception:  # noqa: BLE001
            # 坏文件不许让检索变砖：退化为空表（调用方只会失去别名能力）
            a2c, c2a = {}, {}
    _CACHE = (path, mtime, a2c, c2a)
    return a2c, c2a


def canonical_of(name: str) -> str | None:
    """`name` 是某个 canonical 或它的别名时，返回 canonical；否则 None。"""
    if not name:
        return None
    a2c, _ = _load()
    return a2c.get(name.strip().lower())


def variants(name: str) -> list[str]:
    """返回应拿去和 `label` 比对**全部写法**（canonical + 别名）。

    未知名字原样返回 `[name]` —— 调用方的 SQL 不用改形状，行为与解析前一致。
    """
    if not name:
        return [name]
    _a2c, c2a = _load()
    canon = canonical_of(name)
    if canon and canon in c2a:
        return list(c2a[canon])
    return [name]


def describe(name: str) -> str:
    """给命令行用的一行说明；无需解析时返回空串。"""
    canon = canonical_of(name)
    if not canon or canon == name:
        return ""
    n = len(variants(name))
    return "%s → %s（别名表共 %d 种写法）" % (name, canon, n)


def reset() -> None:
    """清掉 mtime 缓存。给测试用：换 `entities.json` 后不必等 mtime 变化。"""
    global _CACHE
    _CACHE = ("", 0.0, {}, {})


def expansion(text: str) -> list[tuple[str, list[str]]]:
    """在自由文本查询里找出**出现的**实体名，返回 `[(canonical, 全部写法), …]`。

    只认长度 ≥ `MIN_FREE_TEXT_ALIAS` 的名字（2 字别名太容易误撞），大小写不敏感。
    """
    if not text:
        return []
    low = text.lower()
    _a2c, c2a = _load()
    hit: list[tuple[str, list[str]]] = []
    for canon, names in c2a.items():
        for n in names:
            if len(n) >= MIN_FREE_TEXT_ALIAS and n.lower() in low:
                hit.append((canon, list(names)))
                break
    return hit


def duplicates() -> dict[str, list[str]]:
    """同一个写法被挂在多个 canonical 下时，返回 `{写法: [canonical…]}`。

    为什么单独暴露（2026-11-07 GLM 复核发现 3）：`_load` 用 `setdefault` 建反查表，
    一条别名挂两处时**先到先得、全程无提示**，它会确定性地解析到错的那个实体
    （`timeline` 取错标签组、`search` 补错写法）。别名表靠手工维护
    （`cli.py` init 明示"改名脚本不猜，请手工补"），手抖就能造出这种表。
    本函数**不阻断检索**，只让 `doctor` 能把它报出来。
    """
    path = _entities_file()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:  # noqa: BLE001
        return {}
    seen: dict[str, list[str]] = {}
    for canon, names in (doc.get("entities") or {}).items():
        for n in [canon] + list(names or []):
            if not n:
                continue
            lst = seen.setdefault(n.strip().lower(), [])
            if canon not in lst:
                lst.append(canon)
    return {k: v for k, v in seen.items() if len(v) > 1}
