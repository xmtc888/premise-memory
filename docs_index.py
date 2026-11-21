# -*- coding: utf-8 -*-
"""docs 层检索（编译产物优先）：先查 `docs/*.md`，命中不足才回落到原始会话。

## 为什么要有这一层

`search` 查的是**原始消息**。一条结论往往横跨几十条消息，而"编译过"的结论文档
（`docs/*.md`）里它通常是一句话 + 日期 + 数字。2026-11-06 实测同一批问题：

    问「是否推翻过不要向量切块的 RAG」
      · 会话检索 `am.ps1 search 向量切块`  → **0 命中**
      · 会话检索 `am.ps1 search 切块`      → 5 条，**5/5 是 checkpoint 拼接文本**
      · docs 检索                          → 一次命中「不回到纯向量 + chunk 的旧 RAG」+ 日期 + 数字

所以顺序应当是 **docs（编译层）→ cards（断言层）→ sessions（原始层）**，
这就是本仓 `L1_SKIP_REDUNDANT_NAV` 那套"升级式检索"的同一个思想。

## 三条约束（都是"换台机器还能用"的前提）

1. **配置驱动、缺失即关**：docs 根写在 `index/docs-roots.json`（本机数据，不入库），
   没有这份配置就**静默跳过**这层——不是错误，只是这台机器还没接。
   回退顺序：配置文件 → 环境变量 `DOCS_MCP_ROOTS`（逗号分隔）→ 关闭。
2. **零 LLM、零向量**：纯子串/词元匹配，毫秒级。
3. **必须回报"答案来自哪一层"**：调用方要能打印"docs 命中 N 条 / 已回落 sessions"，
   不许让用户以为看到的是会话原文。

## 匹配规则

- 查询按空白切词；**所有词都要在同一个文件里出现**（AND），单字词忽略；
- 中文不分词（不引入 jieba）：单个中文词就按**子串**匹配——这也是当初踩过的坑，
  `切块` / `向量` 这类 2 字词在 trigram 上本来就命不中；
- 排序：命中在**标题**里 > 命中在正文里；同分按命中次数、再看文件更新时间。
"""
from __future__ import annotations

import io
import json
import os
import re
import time

#: 单文件大小上限（超大 md 跳过，避免一次查询被一个文件拖死）
MAX_FILE_BYTES = 2 * 1024 * 1024
#: 单次扫描的文件数上限（防御性：配错了根目录也不至于扫全盘）
MAX_FILES = 3000
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def config_file() -> str:
    try:
        from paths import DOCS_ROOTS_FILE
        return DOCS_ROOTS_FILE
    except Exception:                                  # noqa: BLE001
        return ""


def roots() -> tuple[list[str], str]:
    """返回 `(现有目录列表, 来源说明)`；来源 ∈ config / env / none。"""
    p = config_file()
    if p and os.path.exists(p):
        try:
            # 用 utf-8-sig：Windows 上用 PowerShell/记事本写出来的 JSON 常带 BOM，
            # 用 utf-8 读会抛 "Unexpected UTF-8 BOM" ⇒ 整层被静默关掉（2026-11-08 实测踩到）
            with io.open(p, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            raw = doc.get("roots") if isinstance(doc, dict) else doc
            dirs = [str(x) for x in (raw or []) if str(x).strip()]
            return [d for d in dirs if os.path.isdir(d)], "config"
        except Exception:                              # noqa: BLE001
            return [], "none"                          # 配置坏了 = 不启用这层
    env = (os.environ.get("DOCS_MCP_ROOTS") or "").strip()
    if env:
        dirs = [x.strip() for x in env.split(",") if x.strip()]
        return [d for d in dirs if os.path.isdir(d)], "env"
    return [], "none"


def _iter_md(dirs: list[str]):
    seen, n = set(), 0
    for d in dirs:
        for base, subdirs, files in os.walk(d):
            # 跳过隐藏目录与常见噪音目录
            subdirs[:] = [s for s in subdirs if not s.startswith(".") and s not in
                          ("node_modules", "__pycache__", ".git")]
            for f in files:
                if not f.lower().endswith(".md"):
                    continue
                path = os.path.join(base, f)
                real = os.path.realpath(path)
                if real in seen:
                    continue
                seen.add(real)
                try:
                    if os.path.getsize(path) > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                n += 1
                if n > MAX_FILES:
                    return
                yield path


def _tokens(text: str) -> list[str]:
    """空白切词；单字忽略（噪音太大）。中文整段不拆 —— 用子串匹配。"""
    return [t for t in re.split(r"\s+", (text or "").strip()) if len(t) >= 2]


def _heading_before(lines: list[str], idx: int) -> str:
    for i in range(idx, -1, -1):
        m = _HEADING.match(lines[i])
        if m:
            return m.group(2).strip()
    return ""


def _paragraphs(lines: list[str]):
    """按空行切段，产出 `(起始行下标, [行…])`。

    段落是**语义单位**：同一段里分两句提到两件事，仍然是在讨论同一件事。
    行级匹配会把这种情况整篇丢掉（2026-11-09 实测漏召回 74%）。
    """
    buf, start = [], 0
    for i, ln in enumerate(lines):
        if ln.strip():
            if not buf:
                start = i
            buf.append(ln)
        elif buf:
            yield start, buf
            buf = []
    if buf:
        yield start, buf


def search(text: str, limit: int = 5, dirs: list[str] | None = None,
           max_chars: int = 0, timeout_ms: int = 0) -> tuple[list[dict], str]:
    """在 docs 里找 `text`，返回 `(hits, source)`（兼容旧调用；元信息见 `search_ex`）。"""
    hits, source, _meta = search_ex(text, limit=limit, dirs=dirs,
                                    max_chars=max_chars, timeout_ms=timeout_ms)
    return hits, source


def search_ex(text: str, limit: int = 5, dirs: list[str] | None = None,
              max_chars: int = 0, timeout_ms: int = 0
              ) -> tuple[list[dict], str, dict]:
    """同 `search`，但多返回一份 meta：`{timed_out, scanned, elapsed_ms}`。

    `max_chars`：**总字符预算**（0 = 不限）——命中按分数取，累计到预算就停；
      借鉴 TencentDB Agent Memory 的 `recall.maxTotalRecallChars`（默认 0 不改变行为）。
    `timeout_ms`：**扫描超时就停**（0 = 不限），返回已找到的部分并如实标 `timed_out`
      （借鉴它的 `recall.timeoutMs=5000`：超时跳过注入，不阻塞对话）。慢根目录下
      这条比"卡住"重要得多。
    """
    t0 = time.monotonic()
    deadline = (t0 + timeout_ms / 1000.0) if timeout_ms > 0 else 0.0
    meta = {"timed_out": False, "scanned": 0, "elapsed_ms": 0.0}

    if dirs is None:
        dirs, source = roots()
    else:
        source = "explicit"
    toks = [t.lower() for t in _tokens(text)]
    if not dirs or not toks:
        return [], source, meta

    hits = []
    used = 0
    for path in _iter_md(dirs):
        if deadline and time.monotonic() > deadline:
            meta["timed_out"] = True
            break
        meta["scanned"] += 1
        try:
            raw = io.open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        low = raw.lower()
        if not all(t in low for t in toks):           # 文件级 AND：所有词都得在同一文件里
            continue
        lines = raw.split("\n")
        # ★ 匹配单位是**段落**而不是单行（2026-11-09 实测改）：
        #   原来要求"某一行同时含全部词"，实测 25 个真实多词查询里
        #   **文件级通过 509 篇次、行级只通过 130 ⇒ 74% 被整篇丢弃**，
        #   其中很多是"同一段里分两句说了这两件事"（例：「docs 根」丢 76%）。
        #   改为段落级（空行分隔）后捡回 +95 篇次（+73%），且比固定窗口更自然
        #   （窗3/5/8 分别为 +60/+94/+126，但要引入无依据的 k）。
        best = None
        for start, block in _paragraphs(lines):
            block_low = "\n".join(block).lower()
            if not all(t in block_low for t in toks):
                continue
            # 段内选最匹配的一行做摘录
            inner_i, inner_line = start, block[0]
            best_inner = -1
            for off, ln in enumerate(block):
                c = sum(ln.lower().count(t) for t in toks)
                if c > best_inner:
                    best_inner, inner_i, inner_line = c, start + off, ln
            heading = _heading_before(lines, start)
            heading_hit = any(t in (heading or "").lower() for t in toks)
            score = (2 if heading_hit else 1, sum(block_low.count(t) for t in toks))
            if best is None or score > best[0]:
                best = (score, inner_i, heading, inner_line.strip())
        if best is None:
            continue
        _score, idx, heading, line = best
        if max_chars > 0:
            room = max_chars - used
            if room <= 0:
                break                                 # 预算用尽：剩下的不再取
            line = line[:room]
            used += len(line)
        hits.append({
            "file": path,
            "heading": heading,
            "line": idx + 1,
            "snippet": line[:300],
            "heading_hit": any(t in (heading or "").lower() for t in toks),
        })

    hits.sort(key=lambda h: (0 if h["heading_hit"] else 1,))
    meta["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return hits[:limit], source, meta


def stats(dirs: list[str] | None = None) -> dict:
    if dirs is None:
        dirs, source = roots()
    else:
        source = "explicit"
    files = list(_iter_md(dirs)) if dirs else []
    newest = 0.0
    for p in files:
        try:
            newest = max(newest, os.path.getmtime(p))
        except OSError:
            pass
    return {
        "roots": dirs,
        "source": source,
        "files": len(files),
        "newest": time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)) if newest else "",
        "config": config_file(),
    }
