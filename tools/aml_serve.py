"""AML 契约的本地自测适配器（HTTP 服务，标准库实现，不新增依赖）。

## 契约来源（A 级）

官方文档 `https://agentmemoryleaderboard.ai/docs`（2026-11-17 抓取）原文：

    POST  Add      {request_id, messages:[{role, timestamp?, content}], user_id, session_id}
          -> 200  {success:true, request_id, user_id, session_id}
             · 三个 ID 必须与请求完全一致
             · 写入完成且**立即可检索**后才能返回 200
             · 同一 request_id 会被平台有界重试（幂等）
    POST  Search   {query, options?, user_id, top_k}
          -> 200  {data:[{id, content, score?, created_at?}]}
             · data 必须是数组，**不要 items 包装层，不要顶层数组**
             · 返回条数**不得超过 top_k**（超量判契约错误，不静默截断）
             · 只返回证据，不得生成答案
    GET   Health   无鉴权，任意 2xx 即正常
    鉴权     Authorization 或 X-Api-Key（与申请时选择的方式一致）
    隔离     Search 只能在该 user_id 范围内检索（跨用户禁止）

## 这个适配器是干什么的

**只做本地自测**：验证"我们这套检索在 AML 契约下的接口形态"是合规的，
并用自建题集量出能力分数。它**不参赛**、不对外、不带任何评测数据外传。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aml_store import AmlStore                                    # noqa: E402

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "work", "aml", "eval.db")

#: 单请求体积上限（契约说单请求最长 30 分钟；这里只防内存炸）
MAX_BODY = 32 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    """把 HTTP 请求翻译成 AmlStore 调用；严格按契约返回。"""

    server_version = "aml-selftest/1.0"
    api_key = ""
    store: AmlStore = None
    #: 请求体是否已被读过（_read_json 会置位；_drain_body 据此避免重复读阻塞）
    _body_consumed = False

    # ── 工具 ────────────────────────────────────────────────────────────
    def log_message(self, fmt, *args):            # 安静：自测不需要 access log
        if os.environ.get("AML_VERBOSE"):
            sys.stderr.write("[http] " + fmt % args + "\n")

    def _send(self, code: int, payload=None) -> None:
        body = b"" if payload is None else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 显式关连接：自测流量小，换来"绝不因半读请求体而中止连接"的确定性
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def _err(self, code: int, reason: str) -> None:
        """契约推荐格式：{"detail": {"reason": "..."}}（不含密钥）

        ⚠️ 拒绝请求前必须**把请求体读完**（或显式关闭连接），否则客户端还在发 body
        时服务端已返回并关连接 → 客户端报 ConnectionAborted/WinError 10053（实测踩到）。
        """
        self._drain_body()
        self._send(code, {"detail": {"reason": reason}})

    def _drain_body(self) -> None:
        """把**尚未读**的请求体读掉（只读内存，不解析），避免连接被中止。

        ⚠️ 必须记住 body 是否已读过：`_read_json()` 读过之后，再按 Content-Length
        读一遍会一直阻塞（实测踩到 —— 客户端等不到响应就 ConnectionAborted）。
        """
        if self._body_consumed:
            return
        self._body_consumed = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < n <= MAX_BODY:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    def _authorized(self) -> bool:
        if not self.api_key:
            return True
        auth = self.headers.get("Authorization") or ""
        key = self.headers.get("X-Api-Key") or ""
        if auth.startswith("Bearer "):
            auth = auth[len("Bearer "):]
        return (auth or key) == self.api_key

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._body_consumed = True
            return None, "Content-Length 不是整数"
        if n <= 0:
            self._body_consumed = True
            return None, "请求体为空"
        if n > MAX_BODY:
            return None, "请求体过大"
        raw = self.rfile.read(n)
        self._body_consumed = True
        try:
            return json.loads(raw.decode("utf-8")), ""
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, "请求体不是合法 JSON：%s" % type(exc).__name__

    # ── 路由 ────────────────────────────────────────────────────────────
    def do_GET(self):                                        # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/health", "/healthz", "/"):
            # 契约：Health 无需鉴权，任意 2xx 即服务正常
            self._send(200, {"status": "ok", "memories": self.store.stats()})
            return
        self._err(404, "unknown path: %s" % path)

    def do_POST(self):                                       # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if not self._authorized():
            self._err(401, "缺少或错误的 API key（Authorization / X-Api-Key）")
            return
        if path == "/add":
            self._handle_add()
        elif path == "/search":
            self._handle_search()
        else:
            self._err(404, "unknown path: %s" % path)

    def _handle_add(self) -> None:
        body, why = self._read_json()
        if body is None:
            self._err(422, why)
            return
        request_id = body.get("request_id")
        user_id = body.get("user_id")
        session_id = body.get("session_id")
        messages = body.get("messages")
        if not isinstance(request_id, str) or not request_id.strip():
            self._err(422, "request_id 必填且为非空字符串")
            return
        if not isinstance(user_id, str) or not user_id.strip():
            self._err(422, "user_id 必填且为非空字符串")
            return
        if not isinstance(session_id, str) or not session_id.strip():
            self._err(422, "session_id 必填且为非空字符串")
            return
        if not isinstance(messages, list) or not messages:
            self._err(422, "messages 必填且为非空数组")
            return
        for i, m in enumerate(messages):
            if not isinstance(m, dict):
                self._err(422, "messages[%d] 必须是对象" % i)
                return
            if not isinstance(m.get("content"), str) or not m["content"].strip():
                self._err(422, "messages[%d].content 必须是非空字符串" % i)
                return
            if not isinstance(m.get("role"), str) or not m["role"].strip():
                self._err(422, "messages[%d].role 必须是字符串" % i)
                return
        try:
            res = self.store.add(request_id, user_id, session_id, messages)
        except Exception as exc:                             # noqa: BLE001
            self._err(500, "写入失败：%s" % type(exc).__name__)
            return
        # 契约：同步完成 —— 上面 add() 已 commit，返回后立即可检索
        self._send(200, {"success": True,
                         "request_id": res["request_id"],
                         "user_id": res["user_id"],
                         "session_id": res["session_id"]})

    def _handle_search(self) -> None:
        body, why = self._read_json()
        if body is None:
            self._err(422, why)
            return
        query = body.get("query")
        user_id = body.get("user_id")
        top_k = body.get("top_k")
        if not isinstance(query, str) or not query.strip():
            self._err(422, "query 必填且为非空字符串")
            return
        if not isinstance(user_id, str) or not user_id.strip():
            self._err(422, "user_id 必填且为非空字符串")
            return
        if not isinstance(top_k, int) or top_k <= 0:
            self._err(422, "top_k 必填且为正整数")
            return
        items = self.store.search(query, user_id, top_k)
        # 契约：不得超过 top_k（不静默截断，但这里是我们自己控制，取前 top_k）
        items = items[:top_k]
        # 契约：data 是数组，不要 items 包装层，不要顶层数组
        self._send(200, {"data": items})


def make_server(db_path: str, host: str, port: int, api_key: str = "") -> ThreadingHTTPServer:
    store = AmlStore(db_path)
    handler = type("BoundHandler", (Handler,), {"store": store, "api_key": api_key})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    srv._aml_store = store          # 便于调用方关闭
    return srv


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AML 契约本地自测适配器（只做自测，不参赛）")
    ap.add_argument("--db", default=os.path.normpath(DEFAULT_DB))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--api-key", default=os.environ.get("AML_API_KEY", ""),
                    help="留空则不校验（仅本地自测）")
    ap.add_argument("--reset", action="store_true", help="启动前清空评测库")
    args = ap.parse_args(argv)

    if args.reset and os.path.exists(args.db):
        st = AmlStore(args.db)
        st.reset()
        st.close()

    srv = make_server(args.db, args.host, args.port, args.api_key)
    host, port = srv.server_address[:2]
    print("AML self-test adapter listening on http://%s:%d" % (host, port))
    print("  GET  /health   无需鉴权")
    print("  POST /add      Add 契约")
    print("  POST /search   Search 契约")
    print("  db: %s" % args.db)
    if not args.api_key:
        print("  ⚠ 未设置 --api-key / AML_API_KEY：本地自测不校验鉴权")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        getattr(srv, "_aml_store", None) and srv._aml_store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
