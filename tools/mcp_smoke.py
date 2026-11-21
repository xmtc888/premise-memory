"""MCP 协议冒烟：起真进程、按 stdio 协议收发，逐条验证复核清单 C6–C12。

不依赖任何 MCP SDK：用 subprocess 直接喂 JSON 行，验证的是**宿主会看到的东西**。
用法：python tools/mcp_smoke.py
退出码 0 = 全过。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
SERVER = os.path.join(ROOT, "mcp_server.py")


class Case:
    def __init__(self, name, ok, detail=""):
        self.name, self.ok, self.detail = name, ok, detail


class Client:
    def __init__(self):
        self.p = subprocess.Popen(
            [PY, SERVER], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self.next_id = 0

    def send(self, obj):
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def read(self):
        line = self.p.stdout.readline()
        if not line:
            return None
        return json.loads(line)

    def call(self, method, params=None, mid=None):
        self.next_id += 1
        rid = mid if mid is not None else self.next_id
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return self.read()

    def notify(self, method, params=None):
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:                                   # noqa: BLE001
            self.p.kill()


def run() -> list:
    out = []
    c = Client()

    # 1. initialize：protocolVersion 原样回显（C10）
    r = c.call("initialize", {"protocolVersion": "2025-06-18",
                              "capabilities": {}, "clientInfo": {"name": "smoke"}})
    out.append(Case("initialize 回显 protocolVersion",
                    r and r.get("result", {}).get("protocolVersion") == "2025-06-18",
                    str(r)[:120]))
    out.append(Case("initialize 声明 tools 能力",
                    r and "tools" in (r.get("result", {}).get("capabilities") or {})))
    out.append(Case("initialize 回 serverInfo.name=agent-memory",
                    r and (r.get("result", {}).get("serverInfo") or {}).get("name")
                    == "agent-memory"))

    # 2. notification 不回响应（C8）：发一条 initialized，然后发 ping，
    #    如果 initialized 被回应了，read 拿到的会是它而不是 ping 的响应
    c.notify("notifications/initialized")
    r = c.call("ping", {})
    out.append(Case("notification 不产生响应（下一条响应属于 ping）",
                    r and r.get("id") == c.next_id and "result" in r, str(r)[:120]))

    # 3. tools/list：3 个工具、inputSchema 必须是 object（C12）
    r = c.call("tools/list", {})
    tools = (r or {}).get("result", {}).get("tools") or []
    names = [t.get("name") for t in tools]
    out.append(Case("tools/list 返回 3 个工具", len(tools) == 3, str(names)))
    out.append(Case("工具名符合预期",
                    names == ["memory_context", "memory_search", "memory_timeline"],
                    str(names)))
    out.append(Case("每个 inputSchema.type == object（C12）",
                    all((t.get("inputSchema") or {}).get("type") == "object" for t in tools)))
    # 2026-11-17 事故回归：缺 annotations.readOnlyHint，Codex 会判"需要审批"，
    # 在 exec 模式无交互通道 ⇒ 取消调用（用户实测 HITS=0 的真因）
    out.append(Case("每个工具声明 readOnlyHint=true（Codex 免审批前提）",
                    all((t.get("annotations") or {}).get("readOnlyHint") is True
                        for t in tools),
                    str([(t.get("name"), (t.get("annotations") or {}).get("readOnlyHint"))
                         for t in tools])[:160]))
    out.append(Case("annotations 声明 openWorldHint=false / destructiveHint=false",
                    all((t.get("annotations") or {}).get("openWorldHint") is False
                        and (t.get("annotations") or {}).get("destructiveHint") is False
                        for t in tools)))

    # 4. tools/call：memory_search 正常返回 content[] 结构（C9）
    r = c.call("tools/call", {"name": "memory_search",
                              "arguments": {"query": "质保", "limit": 3}})
    res = (r or {}).get("result") or {}
    content = res.get("content") or []
    ok_shape = (isinstance(content, list) and content
                and content[0].get("type") == "text"
                and res.get("isError") is False)
    out.append(Case("search 返回 {content:[{type:text}], isError:false}（C9）",
                    ok_shape, str(res)[:150]))
    payload = json.loads(content[0]["text"]) if content else {}
    out.append(Case("search 带回溯字段 via/relaxed/alias_note（C21）",
                    isinstance(payload, dict) and "meta" in payload
                    and "via" in (payload.get("meta") or {}),
                    str(payload.get("meta"))[:150]))
    out.append(Case("search 命中非空", bool((payload.get("items") or [])),
                    "hits=%d" % len(payload.get("items") or [])))

    # 5. 工具内部错误用 isError=true，而不是 JSON-RPC error（C9）
    r = c.call("tools/call", {"name": "memory_search", "arguments": {"query": ""}})
    out.append(Case("空 query → isError=true（不是协议错误）",
                    (r or {}).get("result", {}).get("isError") is True
                    and "error" not in (r or {}), str(r)[:150]))

    # 6. 未知工具 → isError=true
    r = c.call("tools/call", {"name": "nope", "arguments": {}})
    out.append(Case("未知工具 → isError=true",
                    (r or {}).get("result", {}).get("isError") is True, str(r)[:120]))

    # 7. 未知方法 → JSON-RPC -32601
    r = c.call("nope/method", {})
    out.append(Case("未知方法 → error.code=-32601",
                    (r or {}).get("error", {}).get("code") == -32601, str(r)[:120]))

    # 8. id 保真：字符串 id 原样回显（C11）
    rid = "req-abc-123"
    r = c.call("ping", {}, mid=rid)
    out.append(Case("字符串 id 原样回显（C11）", r and r.get("id") == rid, str(r)[:120]))

    # 9. memory_context 能出块且带成本行；耗时应在秒级
    import time
    t0 = time.perf_counter()
    r = c.call("tools/call", {"name": "memory_context",
                              "arguments": {"entity": "示例知识库"}})
    dt = time.perf_counter() - t0
    res = (r or {}).get("result") or {}
    text = (res.get("content") or [{}])[0].get("text") or ""
    out.append(Case("memory_context 返回文本", bool(text) and res.get("isError") is False,
                    "chars=%d" % len(text)))
    out.append(Case("memory_context 含成本行与索引层说明",
                    "本块约" in text and "索引层" in text))
    out.append(Case("memory_context 用缓存（<3s，现挖需 12s）", dt < 3.0,
                    "%.2f s" % dt))

    # 10. memory_timeline
    r = c.call("tools/call", {"name": "memory_timeline",
                              "arguments": {"entity": "示例知识库", "per": 2}})
    res = (r or {}).get("result") or {}
    out.append(Case("memory_timeline 返回文本",
                    bool((res.get("content") or [{}])[0].get("text"))
                    and res.get("isError") is False, str(res)[:100]))

    # 11. 坏 JSON 行 → -32700，且不影响后续
    c.p.stdin.write("{not json\n")
    c.p.stdin.flush()
    r = c.read()
    out.append(Case("坏 JSON → error.code=-32700",
                    (r or {}).get("error", {}).get("code") == -32700, str(r)[:120]))
    r = c.call("ping", {})
    out.append(Case("坏 JSON 后服务仍可用", r and "result" in r, str(r)[:80]))

    # 12. stdout 只有 JSON-RPC（C6）：stderr 可以有日志，stdout 不能有非 JSON 行
    c.close()
    err = c.p.stderr.read() if c.p.stderr else ""
    out.append(Case("stderr 有启动日志（说明日志没走 stdout）",
                    "started" in (err or ""), (err or "")[:80].replace("\n", " ")))
    return out


def main() -> int:
    cases = run()
    failed = 0
    print("=== MCP 协议冒烟（真实起进程）===")
    for c in cases:
        if not c.ok:
            failed += 1
        print("  [%s] %s%s" % ("PASS" if c.ok else "FAIL", c.name,
                               ("  | " + c.detail) if c.detail else ""))
    print("  -> %d 条，失败 %d" % (len(cases), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
