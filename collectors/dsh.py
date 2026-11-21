"""DSH（DeepSeek Harness）会话采集器。

格式（实测 2026-11-02）：`~/.dsh/sessions/<slug>/<uuid>/session.v4.jsonl.zstd`
多帧 zstd 压缩的 JSONL，逐行事件：
  {"type":"session", "id","createdAt","cwd","origin","parentSession",...}
  {"type":"session/title",     "data":{"title":...}}
  {"type":"request/context",   "data":{"provider","model","contextWindow"}}
  {"type":"user/message",      "data":{"role","content":[{type:"text",text}],"source"}}
  {"type":"assistant/message", "data":{"message":{"content":[{type:"text"|"reasoning",...}]},"usage"}}
  {"type":"tool/call"|"tool/result", ...}

tool/call 与 tool/result 用 (turn, step) 配对（2026-11-17 探针 A 实测配对率 100%，
callId 只在 call 侧有、result 侧没有）。result.message 有两种形态：
旧=纯文本；新=嵌套 JSON 字符串 {"role":"tool","content":[{"type":"text","text":...}]}——
抽退出码必须解开嵌套再搜 "[exit code: N]"，否则漏掉绝大多数（探针 B 实测踩坑）。
"""
from __future__ import annotations

import glob
import io
import json
import os
import re
from datetime import datetime, timezone

import zstandard

from collectors.base import (CollectResult, Record, SessionMeta, ToolCall,
                             clean_text, is_injected_context, path_dirs_from_args,
                             session_key, texts_from_blocks, top_dirs)

AGENT = "dsh"

#: pwsh 结果里的退出码标记（DSH 固定格式，见会话样例 "[exit code: 1]"）
_EXIT_RE = re.compile(r"\[exit code: (\d+)\]")


def _result_text(msg) -> str:
    """兼容 result.message 的纯文本与嵌套 JSON 两种形态，取出正文。"""
    if isinstance(msg, str):
        s = msg
        if s.lstrip().startswith("{"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    parts = [c.get("text") or "" for c in obj.get("content") or []
                             if isinstance(c, dict)]
                    if parts:
                        return "\n".join(parts)
            except (json.JSONDecodeError, ValueError):
                pass
        return s
    if isinstance(msg, dict):
        parts = [c.get("text") or "" for c in msg.get("content") or []
                 if isinstance(c, dict)]
        return "\n".join(parts)
    return ""


def default_root() -> str:
    return os.path.join(os.path.expanduser("~"), ".dsh", "sessions")


def find_sessions(root: str | None = None) -> list[str]:
    root = root or default_root()
    return sorted(glob.glob(os.path.join(root, "*", "*", "session.v4.jsonl.zstd")))


def _iter_events(path: str):
    with open(path, "rb") as fh:
        raw = fh.read()
    reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw))
    for line in io.TextIOWrapper(reader, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _iso_from_ms(ms) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat()
    except Exception:                                     # noqa: BLE001
        return ""


def read_session(path: str) -> CollectResult | None:
    meta = SessionMeta(agent=AGENT, source_path=path)
    records: list[Record] = []
    seq = 0
    dir_hits: dict[str, int] = {}
    # pwsh 调用与结果的配对缓存（2026-11-17）：(turn, step) -> [command, ts]
    pwsh_calls: dict[tuple, list] = {}
    pwsh_order: list[tuple] = []
    tool_calls: list[ToolCall] = []
    tc_seq = 0
    for ev in _iter_events(path):
        et = ev.get("type") or ""
        data = ev.get("data") or {}
        if et == "session":
            meta.session_id = ev.get("id") or ""
            meta.cwd = ev.get("cwd") or ""
            meta.origin = ev.get("origin") or ""
            meta.parent_session = ev.get("parentSession") or ""
            meta.started_at = _iso_from_ms(ev.get("createdAt"))
        elif et == "session/title":
            meta.title = data.get("title") or meta.title
        elif et == "request/context":
            meta.provider = data.get("provider") or meta.provider
            meta.model = data.get("model") or meta.model
        elif et == "tool/call":
            meta.tool_call_count += 1
            k = (ev.get("turn"), ev.get("step"))
            for d in path_dirs_from_args(data.get("arguments") or ""):
                dir_hits[d] = dir_hits.get(d, 0) + 1
            if (data.get("name") or "") == "pwsh":
                args = data.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, ValueError):
                        args = {}
                cmd = (args or {}).get("command") or ""
                if cmd:
                    pwsh_calls[k] = [cmd, _iso_from_ms(ev.get("time"))]
                    pwsh_order.append(k)
        elif et == "tool/result":
            k = (ev.get("turn"), ev.get("step"))
            if k in pwsh_calls:
                text = _result_text(data.get("message"))
                m = _EXIT_RE.search(text)
                code = int(m.group(1)) if m else None
                err = data.get("error")
                ok = 0 if (err or (code is not None and code != 0)) else 1
                cmd, ts = pwsh_calls.pop(k)
                tool_calls.append(ToolCall(seq=tc_seq, ts=ts, tool="pwsh",
                                           command=cmd[:2000], ok=ok, exit_code=code))
                tc_seq += 1
        elif et == "user/message":
            txt = clean_text(texts_from_blocks(data.get("content")))
            if txt and not is_injected_context(txt):
                records.append(Record(session_key=session_key(AGENT, meta.session_id),
                                      role="user", text=txt, ts=_iso_from_ms(ev.get("time")),
                                      seq=seq))
                seq += 1
        elif et == "assistant/message":
            msg = data.get("message") or {}
            txt = clean_text(texts_from_blocks(msg.get("content")))
            if txt:
                records.append(Record(session_key=session_key(AGENT, meta.session_id),
                                      role="assistant", text=txt,
                                      ts=_iso_from_ms(ev.get("time")), seq=seq))
                seq += 1
    # 没有 result 的 pwsh 调用（会话中途截断）也落库，ok=None：
    # procedure 挖掘只关心"执行过什么"，失败统计自己过滤 NULL。
    for k in pwsh_order:
        if k in pwsh_calls:
            cmd, ts = pwsh_calls[k]
            tool_calls.append(ToolCall(seq=tc_seq, ts=ts, tool="pwsh",
                                       command=cmd[:2000], ok=None, exit_code=None))
            tc_seq += 1
    if not meta.session_id:
        return None
    if not meta.started_at and records:
        meta.started_at = records[0].ts
    if records:
        meta.ended_at = records[-1].ts
    meta.message_count = len(records)
    tool_calls.sort(key=lambda t: t.seq)
    return CollectResult(meta=meta, records=records, tool_dirs=top_dirs(dir_hits),
                         tool_calls=tool_calls)


def collect(root: str | None = None, limit: int = 0):
    """逐个会话产出 CollectResult。limit>0 时只取前 N 个（调试用）。"""
    files = find_sessions(root)
    if limit:
        files = files[:limit]
    for p in files:
        try:
            res = read_session(p)
        except Exception as e:                            # noqa: BLE001
            print("  [dsh] 跳过 %s: %s: %s" % (os.path.basename(os.path.dirname(p)),
                                              type(e).__name__, e))
            continue
        if res:
            yield res
