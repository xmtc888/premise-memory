"""agent-memory 的 MCP server（stdio，零新依赖）。

## 为什么做（以及为什么是"薄"的）

DSH 有插件真注入；Codex / Claude Code 没有等价机制。指令片段是"推"（文件），
MCP 是"拉"（工具）——拉式的好处是**新鲜**：工具被调用时才去查库，
不会读到昨天写死进 context.md 的旧事实。

设计依据：`docs/2026-11-17_MCP方案复核_glm-5.3.md`（glm-5.3 复核 + 本体逐条核实）。
要点全部按复核修正：
  · 只读：`Store(read_only=True)`（ro 直连，误写会响亮报错；不复用会建表的 `Store()`）
  · 3 个工具而非 6 个（每个工具的 schema 每轮都在上下文里，是真实成本）
  · 不提供写工具（MCP 调用方是 agent；agent 写诊断会在 by 上冒充人类，破坏
    WASP-Mem「人类诊断 > 自判」的立论）
  · procedure 走缓存（现挖 11.8 s，工具调用必须毫秒级）
  · search 必须带回溯字段（via / 别名补检 / 跨消息），否则违反"静默截断=误导"

## 协议实现清单（复核 C6–C12，写错会"静默不兼容"）

1. **stdout 只输出 JSON-RPC**：任何 print 都会毁帧 ⇒ 本文件禁止 print，
   日志一律走 stderr；工具实现**直接调 Python 函数**，绝不 subprocess 调 cli.py。
2. **stdin/stdout 双向 UTF-8**：Windows 默认 GBK，中文查询会 mojibake/断流。
3. **notification 不回响应**（`notifications/initialized`、`notifications/cancelled`）。
4. **tools/call 结果**：`{"content":[{"type":"text","text":...}], "isError": bool}`；
   工具内部错误用 `isError: true`，不用 JSON-RPC error（否则宿主显示为"无信息失败"）。
5. **protocolVersion**：客户端给字符串就原样回显（工具集稳定，不挑版本）；
   没给才用我们的默认值。
6. **id 保真**：字符串/数字/空 原样回显，不改类型。
7. **framing**：每行一个 JSON（不是 LSP 的 Content-Length 头）。

## 用法

```powershell
python mcp_server.py            # stdio；宿主配置见 references/mcp.md
```
"""
from __future__ import annotations

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from paths import DB_FILE                                            # noqa: E402

SERVER_NAME = "agent-memory"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2025-06-18"

#: 数据库路径只来自 AGENT_MEMORY_HOME / 默认位置，**不接受客户端传路径**
DB = os.environ.get("AGENT_MEMORY_DB") or DB_FILE


def log(msg: str) -> None:
    """日志只走 stderr（stdout 是协议通道）。"""
    sys.stderr.write("[agent-memory] %s\n" % msg)
    sys.stderr.flush()


# ── 工具实现（全部只读）───────────────────────────────────────────────────
def _open_ro():
    from store import Store
    return Store(DB, read_only=True)


def tool_context(entity: str = "", max_chars: int = 1400) -> dict:
    """返回某项目的「索引层」注入块：环境事实 + 常用操作（带前提判定与成本行）。"""
    import inject
    import procedures

    if not entity:
        raise ValueError("entity 必填（项目实体名，如 demo-kb）")
    if not os.path.exists(DB):
        raise FileNotFoundError("索引库不存在：%s" % DB)
    chunks: list[str] = []
    sizes: list[tuple[str, int]] = []

    env = inject.build_injection(DB, entity, max_chars=max_chars)
    chunks.append(env["text"])
    sizes.append(("环境事实", len(env["text"])))

    blk = procedures.pick_cached(DB, entity, max_items=3, max_repairs=1)
    ptext = procedures.render_block(blk, max_chars=500)
    if ptext.strip():
        chunks.append(ptext)
        sizes.append(("procedure", len(ptext)))

    total = sum(n for _l, n in sizes)
    parts = " / ".join("%s 约 %d token" % (l, round(n / 1.6)) for l, n in sizes)
    cost = ("> 本块约 %d token（%s）。索引层；细节用 memory_search / am premises 展开。"
            % (round(total / 1.6), parts))
    text = "\n\n".join(c.rstrip("\n") for c in chunks if c.strip())
    # 成本行插在 MARKER 之后（与 cli 的 _insert_cost_line 同规则）
    lines = text.split("\n")
    if lines and lines[0].startswith("<!-- agent-memory:auto-generated"):
        text = "\n".join([lines[0], "", cost, ""] + lines[1:])
    else:
        text = cost + "\n\n" + text
    note = blk.get("cache_note") or ""
    if note:
        text += "\n\n> procedure 缓存：%s（built_at %s）" % (
            note, blk.get("cache_built_at") or "?")
    return {"text": text, "chars": len(text), "entity": entity}


def tool_search(query: str = "", limit: int = 10, peer: str = "") -> dict:
    """检索历史会话消息；**带回溯字段**（走哪条路、是否别名补检、是否跨消息）。"""
    if not query or not query.strip():
        raise ValueError("query 必填")
    lim = int(limit or 10)
    st = _open_ro()
    try:
        rows = st.search(query, lim, peer=peer or "", explain_substring=True)
        # 2026-11-17 实测教训：调用方把 `hits=94`（limit=500 时的 token 命中数）
        # 当成了"全库总量"，而真实子串命中是 325。字段名含糊就会误导，
        # 所以拆成 returned / limit / truncated / substring_note 分开说。
        meta = {
            "via": st.last_search_via,
            "relaxed": bool(st.last_query_relaxed),
            "alias_note": st.last_alias_note,
            "cross_note": getattr(st, "last_cross_note", ""),
            "returned": len(rows),
            "limit": lim,
            "truncated": len(rows) >= lim,
            "substring_note": getattr(st, "last_substring_note", ""),
        }
    finally:
        st.close()
    return {"items": rows, "meta": meta}


def tool_timeline(entity: str = "", intent: str = "", per: int = 3) -> dict:
    """某实体（可再按意图）的结论演变时间线。"""
    import timeline
    if not entity:
        raise ValueError("entity 必填")
    # build_timeline 的参数是 limit（会话数），render 的参数是 max_concl（每会话结论数）
    tl = timeline.build_timeline(DB, entity, intent=intent or "", limit=200)
    return {"text": timeline.render(tl, max_concl=int(per or 3)), "entity": entity,
            "intent": intent}


#: 工具注解（MCP spec 的 `annotations`）。
#: **不是可选项**（2026-11-17 实测事故）：Codex 判定"要不要审批"就看它——
#: `codex-rs/core/src/mcp_tool_call.rs:2840` 的 `requires_mcp_tool_approval()`:
#:   read_only_hint 为真 ⇒ 免审批；未声明 ⇒ destructive/openWorld 默认 true ⇒ **要审批**；
#: 而 `codex exec` 没有交互审批通道 ⇒ 取消并返回 "user cancelled MCP tool call"
#: （用户实测 HITS=0 就是这个原因，不是工具本身失败）。
#: 本 server 三个工具全是只读查询，所以如实声明 readOnlyHint，让宿主直接放行。
ANNOTATIONS = {
    "readOnlyHint": True,       # 只读：不写库、不写文件、无副作用
    "destructiveHint": False,   # 不破坏数据
    "idempotentHint": True,     # 同参数重复调用结果一致
    "openWorldHint": False,     # 不访问外部世界（不联网）
}

TOOLS = [
    {
        "name": "memory_context",
        "description": ("取某个项目（实体）的「索引层」记忆块：环境事实（目录改名/失效路径，"
                        "每条带认证时刻与依据）+ 项目常用操作（带前提判定）。"
                        "开工时调一次即可；约 700 token，块内自带成本。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "项目实体名，如 demo-kb"},
                "max_chars": {"type": "integer", "description": "环境事实字符上限（默认 1400）"},
            },
            "required": ["entity"],
        },
        "handler": tool_context,
    },
    {
        "name": "memory_search",
        "description": ("检索本机多个编码 agent 的历史会话消息（DSH/Codex/Claude Code/…）。"
                        "返回命中片段与**检索路径说明**（via/别名补检/跨消息），"
                        "用于回溯旧结论、旧路径是否还存在。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询词（支持中文；1~2 字走 LIKE 回退）"},
                "limit": {"type": "integer", "description": "返回条数（默认 10）"},
                "peer": {"type": "string", "description": "只查某工作区身份（可选）"},
            },
            "required": ["query"],
        },
        "handler": tool_search,
    },
    {
        "name": "memory_timeline",
        "description": "看某项目某个话题的结论演变（按时间排序，含冲突提示）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "项目实体名"},
                "intent": {"type": "string", "description": "意图标签（可选，如 检索与排序）"},
                "per": {"type": "integer", "description": "每段最多几条结论（默认 3）"},
            },
            "required": ["entity"],
        },
        "handler": tool_timeline,
    },
]

_BY_NAME = {t["name"]: t for t in TOOLS}


# ── JSON-RPC / MCP 分发 ──────────────────────────────────────────────────
def _text_result(payload, is_error: bool = False) -> dict:
    """tools/call 的统一结果结构（复核 C9）。"""
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def handle_call(name: str, arguments: dict) -> dict:
    tool = _BY_NAME.get(name)
    if not tool:
        return _text_result({"error": "未知工具：%s（可用：%s）"
                             % (name, ", ".join(_BY_NAME))}, is_error=True)
    try:
        out = tool["handler"](**(arguments or {}))
        return _text_result(out)
    except TypeError as exc:                     # 参数不匹配：明确说清哪个工具/什么错
        return _text_result({"error": "%s 参数错误：%s" % (name, exc)}, is_error=True)
    except Exception as exc:                     # noqa: BLE001
        log("tool %s failed: %s: %s" % (name, type(exc).__name__, exc))
        log(traceback.format_exc())
        return _text_result({"error": "%s 执行失败：%s: %s"
                             % (name, type(exc).__name__, exc)}, is_error=True)


def dispatch(msg: dict) -> dict | None:
    """处理一条 JSON-RPC 消息；返回响应，通知返回 None。"""
    method = msg.get("method") or ""
    mid = msg.get("id", None)
    has_id = "id" in msg

    # 通知（无 id）一律不回应 —— 复核 C8
    if not has_id:
        return None

    if method == "initialize":
        params = msg.get("params") or {}
        want = params.get("protocolVersion")
        # 复核 C10：客户端给字符串就原样回显；没给才用默认
        version = want if isinstance(want, str) and want else DEFAULT_PROTOCOL
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [{"name": t["name"], "description": t["description"],
                       "inputSchema": t["inputSchema"],
                       # 缺了 annotations，Codex 会要求审批（见 ANNOTATIONS 注释）
                       "annotations": ANNOTATIONS} for t in TOOLS]}}

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return {"jsonrpc": "2.0", "id": mid,
                    "result": _text_result({"error": "arguments 必须是对象"}, True)}
        return {"jsonrpc": "2.0", "id": mid, "result": handle_call(name, args)}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": "Method not found: %s" % method}}


def serve(stdin=None, stdout=None) -> int:
    """主循环：每行一个 JSON。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    # 复核 C7：双向 UTF-8（Windows 默认 GBK，中文查询会断流）
    for stream in (stdin, stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    log("started (db=%s, read-only)" % DB)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _write(stdout, {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "Parse error"}})
            continue
        # 批量请求（JSON-RPC 允许数组）
        if isinstance(msg, list):
            outs = [r for r in (dispatch(m) for m in msg if isinstance(m, dict)) if r]
            if outs:
                _write(stdout, outs)
            continue
        if not isinstance(msg, dict):
            _write(stdout, {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "Invalid Request"}})
            continue
        resp = dispatch(msg)
        if resp is not None:
            _write(stdout, resp)
    return 0


def _write(stdout, payload) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stdout.flush()


def main() -> int:
    try:
        return serve()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
