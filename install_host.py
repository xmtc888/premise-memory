"""给非 DSH 宿主（Codex / Claude Code）安装"开工先读 context.md"的指令片段。

## 为什么要这个（而不是直接改别人的全局配置）

DSH 有插件可以在 systemPrompt 里真注入（`dsh-agent-memory-inject`）；
Codex / Claude Code 没有等价机制，只能靠它们**本来就会读**的规则文件：
  · Codex        `AGENTS.md`（全局 `~/.codex/AGENTS.md`，或项目内任意层）
  · Claude Code  `CLAUDE.md`（全局 `~/.claude/CLAUDE.md`，或项目内任意层）

**关键约束（用户明确要求）**：能改自己机器，不能替别人改配置。所以这里做成：
  ① **自己跑**（一条命令），② `--dry-run` 先看差异，③ 标记块包裹（`--revert` 精确撤销，
     不碰文件里其它内容），④ 默认装**全局**但 `--project <目录>` 可装项目级（影响面更小）。

## 三条纪律（写在片段里，也是这个文件的验收标准）

1. **只读一次**：`context.md` 存在才读，读完不重复读（否则每轮都烧上下文）；
2. **过期要说**：文件头 `check_at` 距今 > 3 天 ⇒ 提示用户刷新，**不要凭过期事实工作**；
3. **块只是索引层**：细节用 `am search` / `am premises` 按需展开，别整块当圣经。

## 幂等

同一份片段重复安装 = 无变化（标记块已存在则替换块内内容）；`--revert` 后文件与安装前
逐字节一致（除被删除的块与为它留出的空行）。
"""
from __future__ import annotations

import os
import time

MARK_BEGIN = "<!-- agent-memory:host-instructions begin v1"
MARK_END = "<!-- agent-memory:host-instructions end -->"

#: host -> (项目级文件名, 全局路径)
HOSTS = {
    "codex": ("AGENTS.md", os.path.join("~", ".codex", "AGENTS.md")),
    "claude": ("CLAUDE.md", os.path.join("~", ".claude", "CLAUDE.md")),
}

SNIPPET_LINES = [
    "## 跨 agent 会话记忆（agent-memory；安装器维护，块内勿手改）",
    "",
    "- **开工先读** `<当前项目>\\.agent-memory\\context.md`（存在才读，**只读一次**）：",
    "  里面是环境事实、常用操作与前提校验；读完不要重复读。",
    "- 若该文件头 `check_at` 距今 **> 3 天**：说明日更没在跑 —— **提示用户**执行",
    "  agent-memory 的 `scripts\\refresh.ps1`；不要自己猜路径，也不要凭过期事实工作。",
    "- 要查历史结论或判断旧路径是否失效：用 **agent-memory 技能**",
    "  （`am.ps1 search \"<关键词>\"` / `am.ps1 premises \"<实体>\"`）；",
    "  注入块只是**索引层**，细节按需展开，不要把整块当结论。",
]


def target_path(host: str, project: str = "") -> str:
    """算目标文件：给了 project 就项目级，否则全局。"""
    if host not in HOSTS:
        raise ValueError("未知宿主: %s（可选 %s）" % (host, "/".join(HOSTS)))
    name, glob = HOSTS[host]
    if project:
        return os.path.join(project, name)
    return os.path.expanduser(glob)


def _read(path: str) -> tuple[str, bool, str]:
    """读文件 → (文本(内部统一 LF), 是否带 BOM, 换行风格)。

    内部统一成 `\\n` 再做比较/插入，避免"文件是 CRLF、渲染块是 LF"导致幂等判断永远
    为"变化"（实测踩到：第二次安装被判成 updated，块被反复重写）。
    写入时由 `_write` 按检测到的 nl 还原。
    """
    if not os.path.exists(path):
        return "", False, "\r\n" if os.name == "nt" else "\n"
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", "replace")
    nl = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), bom, nl


def _write(path: str, text: str, bom: bool, nl: str) -> None:
    """按原文件的 BOM/换行风格写回（避免把别人的 CRLF 文件改成 LF）。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    data = text.replace("\r\n", "\n")
    if nl != "\n":
        data = data.replace("\n", nl)
    payload = (b"\xef\xbb\xbf" if bom else b"") + data.encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(payload)
    os.replace(tmp, path)


def _marker(created: bool = False) -> str:
    return MARK_BEGIN + (" created=1" if created else "") + " -->"


def render_block(created: bool = False) -> str:
    return "\n".join([_marker(created)] + SNIPPET_LINES + [MARK_END])


def _find_block(text: str) -> tuple[int, int] | None:
    """返回标记块在文本里的 (start, end) 下标；没有返回 None。"""
    i = text.find(MARK_BEGIN)
    if i < 0:
        return None
    j = text.find(MARK_END, i)
    if j < 0:
        return None
    return i, j + len(MARK_END)


def status(path: str) -> dict:
    text, _bom, _nl = _read(path)
    span = _find_block(text)
    return {"path": path, "exists": os.path.exists(path),
            "installed": span is not None, "chars": len(text)}


def install(host: str, project: str = "", dry_run: bool = False) -> dict:
    """安装（幂等）：不存在则追加，已存在则替换块内内容。"""
    path = target_path(host, project)
    text, bom, nl = _read(path)
    created_file = not os.path.exists(path)
    span = _find_block(text)

    if span:
        old = text[span[0]:span[1]]
        # 保留原块里的 created 标记：丢了它，撤销时就不知道该不该删文件
        block = render_block(created=("created=1" in old))
        if old == block:
            return {"path": path, "action": "unchanged", "created_file": False,
                    "chars_before": len(text), "chars_after": len(text)}
        new_text = text[:span[0]] + block + text[span[1]:]
        action = "updated"
    else:
        block = render_block(created=created_file)
        base = text
        if base and not base.endswith("\n"):
            base += nl
        if base and not base.endswith(nl * 2):
            base += nl
        new_text = base + block + nl if not base else base + block + nl
        action = "created" if created_file else "appended"

    if not dry_run:
        _write(path, new_text, bom, nl)
    return {"path": path, "action": action, "created_file": created_file,
            "chars_before": len(text), "chars_after": len(new_text),
            "added_lines": len(new_text.splitlines()) - len(text.splitlines())}


def revert(host: str, project: str = "", dry_run: bool = False) -> dict:
    """撤销：删掉标记块；若文件是安装器创建的且只剩空白，则删文件。"""
    path = target_path(host, project)
    if not os.path.exists(path):
        return {"path": path, "action": "missing"}
    text, bom, nl = _read(path)
    span = _find_block(text)
    if not span:
        return {"path": path, "action": "not-installed"}
    created = "created=1" in text[span[0]:span[1]]
    before = text[:span[0]]
    after = text[span[1]:]
    # 去掉为块留出的多余空行（最多两层），避免留下"空洞"
    before = before.rstrip("\r\n")
    after = after.lstrip("\r\n")
    new_text = (before + (nl if before else "")) if before else ""
    if after:
        new_text = (new_text.rstrip("\r\n") + nl * 2 + after) if new_text else after
    if not dry_run:
        if created and not new_text.strip():
            os.remove(path)
            return {"path": path, "action": "deleted-file"}
        if new_text.strip():
            _write(path, new_text if new_text.endswith("\n") else new_text + nl, bom, nl)
        else:
            _write(path, "", bom, nl)
    return {"path": path, "action": "reverted",
            "chars_before": len(text), "chars_after": len(new_text)}


def plan(host: str, project: str = "") -> dict:
    """给 --dry-run 用：返回将要写入的块与目标路径。"""
    path = target_path(host, project)
    text, _bom, _nl = _read(path)
    span = _find_block(text)
    return {"path": path, "exists": os.path.exists(path),
            "already_installed": span is not None,
            "block": render_block(created=not os.path.exists(path)),
            "current_tail": (text[-400:] if text else "")}


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
