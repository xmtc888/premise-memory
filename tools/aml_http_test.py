"""AML 适配器的 HTTP 端到端契约测试（起真服务、发真请求、按契约校验）。

这一层测的是**接口形态**（AML 真正校验的东西）：状态码、字段名、包装层、
ID 回显、top_k 上限、鉴权、错误格式。store 层已由 aml_selftest 覆盖。

用法：python tools/aml_http_test.py [--port 8799]
退出码：0 = 全部通过；1 = 有失败。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aml_serve import make_server                                    # noqa: E402

API_KEY = "selftest-key-2026"


class Case:
    def __init__(self, name, ok, detail=""):
        self.name, self.ok, self.detail = name, ok, detail


def _post(base, path, payload, key=API_KEY, raw=None):
    data = raw if raw is not None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "null")
        except json.JSONDecodeError:
            return e.code, body


def _get(base, path, key=None):
    req = urllib.request.Request(base + path)
    if key:
        req.add_header("X-Api-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def run(base: str) -> list[Case]:
    out: list[Case] = []
    uid = "eval:http:run:conv-0"
    sid = "eval:http:run:sample:0"

    # 1. Health 无需鉴权、2xx
    code, body = _get(base, "/health")
    out.append(Case("GET /health 无鉴权返回 2xx", 200 <= code < 300, "code=%d" % code))

    # 2. Add：正常请求 -> 200 + 三 ID 原样回显
    add = {"request_id": "eval:http:chunk-0",
           "messages": [{"role": "user", "content": "The importer purges old rows before compile.",
                         "timestamp": 1704067200000}],
           "user_id": uid, "session_id": sid}
    code, body = _post(base, "/add", add)
    echo_ok = (isinstance(body, dict)
               and body.get("success") is True
               and body.get("request_id") == add["request_id"]
               and body.get("user_id") == add["user_id"]
               and body.get("session_id") == add["session_id"])
    out.append(Case("POST /add 返回 200 且回显三 ID", code == 200 and echo_ok,
                    "code=%d body=%s" % (code, str(body)[:100])))

    # 3. Add：缺字段 -> 422（契约：字段校验失败用 422 且结构化错误）
    code, body = _post(base, "/add", {"request_id": "x", "messages": [], "user_id": uid})
    out.append(Case("POST /add 缺 session_id -> 422", code == 422, "code=%d" % code))
    out.append(Case("错误体为 {detail:{reason:...}}",
                    isinstance(body, dict) and isinstance(body.get("detail"), dict)
                    and "reason" in body["detail"],
                    str(body)[:100]))

    # 4. Add：非 JSON -> 422
    code, body = _post(base, "/add", None, raw=b"not-json")
    out.append(Case("POST /add 非法 JSON -> 422", code == 422, "code=%d" % code))

    # 5. 鉴权：错 key -> 401
    code, body = _post(base, "/search", {"query": "importer", "user_id": uid, "top_k": 5},
                       key="wrong-key")
    out.append(Case("POST 错误 key -> 401", code == 401, "code=%d" % code))

    # 6. 无鉴权 -> 401
    code, body = _post(base, "/search", {"query": "importer", "user_id": uid, "top_k": 5},
                       key=None)
    out.append(Case("POST 无鉴权 -> 401", code == 401, "code=%d" % code))

    # 7. Search：结构必须是 {data:[...]}，且能召回刚写入的
    code, body = _post(base, "/search", {"query": "purges old rows", "user_id": uid, "top_k": 10})
    ok_struct = (code == 200 and isinstance(body, dict)
                 and isinstance(body.get("data"), list)
                 and "items" not in body)
    out.append(Case("POST /search 返回 {data:[...]}（无 items 包装）", ok_struct,
                    "code=%d keys=%s" % (code, list(body)[:5] if isinstance(body, dict) else body)))
    hit = bool(body.get("data")) if isinstance(body, dict) else False
    out.append(Case("Search 能召回刚写入的记忆（同步可见）", hit,
                    "n=%d" % (len(body.get("data") or []) if isinstance(body, dict) else -1)))

    # 8. Search 条目字段
    items = (body.get("data") or []) if isinstance(body, dict) else []
    field_ok = all(isinstance(i.get("id"), str) and i["id"]
                   and isinstance(i.get("content"), str) and i["content"] for i in items)
    out.append(Case("Search 条目含非空 id/content", field_ok or not items,
                    "n=%d" % len(items)))

    # 9. top_k 上限：请求 2，返回不得超 2
    for k in (1, 2):
        code, body = _post(base, "/search", {"query": "importer", "user_id": uid, "top_k": k})
        n = len((body or {}).get("data") or []) if isinstance(body, dict) else -1
        out.append(Case("返回条数不超过 top_k=%d" % k, n <= k, "n=%d" % n))

    # 10. 缺 top_k -> 422
    code, body = _post(base, "/search", {"query": "importer", "user_id": uid})
    out.append(Case("POST /search 缺 top_k -> 422", code == 422, "code=%d" % code))

    # 11. 空结果 -> 200 + data:[]（不是 null / 不是 404）
    code, body = _post(base, "/search",
                       {"query": "zzz-no-such-token-qqq", "user_id": uid, "top_k": 10})
    ok_empty = (code == 200 and isinstance(body, dict) and body.get("data") == [])
    out.append(Case("无命中返回 200 + data:[]", ok_empty, "code=%d" % code))

    # 12. user_id 隔离：另一个 user 检索同一 query 不应看到
    code, body = _post(base, "/search",
                       {"query": "purges old rows", "user_id": "eval:http:other:conv-9",
                        "top_k": 10})
    out.append(Case("跨 user_id 不泄漏", code == 200 and (body or {}).get("data") == [],
                    "code=%d n=%d" % (code, len((body or {}).get("data") or []))))

    # 13. 未知路径 -> 404
    code, body = _post(base, "/nope", {"x": 1})
    out.append(Case("未知 path -> 404", code == 404, "code=%d" % code))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "..", "work", "aml", "http_test.db"))
    args = ap.parse_args(argv)
    db = os.path.normpath(args.db)
    for suffix in ("", "-wal", "-shm"):
        p = db + suffix
        if os.path.exists(p):
            os.remove(p)
    srv = make_server(db, "127.0.0.1", args.port, API_KEY)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = "http://127.0.0.1:%d" % args.port
    print("=== AML HTTP 契约端到端测试（%s）===" % base)
    try:
        cases = run(base)
    finally:
        srv.shutdown()
        srv.server_close()
        st = getattr(srv, "_aml_store", None)
        if st:
            st.close()
    failed = 0
    for c in cases:
        if not c.ok:
            failed += 1
        print("  [%s] %s%s" % ("PASS" if c.ok else "FAIL", c.name,
                               ("  | " + c.detail) if c.detail else ""))
    print("  -> %d 条，失败 %d" % (len(cases), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
