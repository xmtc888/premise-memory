# -*- coding: utf-8 -*-
"""采集器契约：四种真实格式都要能解析出"人说的话"与"agent 的结论"。

夹具用**真实样本片段**（各取 2~3 行，字段与线上一致），不用手编理想格式 ——
否则测的是我的想象，不是采集器。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.base import clean_text, is_injected_context, texts_from_blocks  # noqa: E402
from collectors import claude, codex, dsh, hermes, workbuddy                    # noqa: E402


def test_texts_from_blocks_skips_reasoning():
    blocks = [{"type": "reasoning", "text": "内部思考"},
              {"type": "text", "text": "可见结论"}]
    assert texts_from_blocks(blocks) == "可见结论"


def test_injected_context_detected():
    """agent 注入的"伪 user 消息"必须被识别（实测来源各一例）。"""
    assert is_injected_context("<system-reminder> x</system-reminder>")
    assert is_injected_context("Current runtime context. This snapshot …")
    assert is_injected_context("# AGENTS.md instructions\n<INSTRUCTIONS>")
    assert is_injected_context("<app-context>\n# Codex desktop context")
    # 人说的话不能被误杀
    assert not is_injected_context("你好，请用一句话介绍自己")
    assert not is_injected_context("刚才的测试2次都用了很长实际")


def test_clean_text_truncates():
    s = clean_text("x" * 30000)
    assert len(s) < 20100 and s.endswith("[截断]")


def test_codex_parses_message_and_counts_tools(tmp_path):
    p = tmp_path / "rollout-x.jsonl"
    lines = [
        {"timestamp": "2026-10-31T08:22:16Z", "type": "session_meta",
         "payload": {"session_id": "abc123", "cwd": "C:\\proj", "model_provider": "deepseek",
                     "timestamp": "2026-10-31T08:20:00Z"}},
        {"timestamp": "2026-10-31T08:22:17Z", "type": "response_item",
         "payload": {"type": "message", "role": "developer",
                     "content": [{"type": "input_text", "text": "系统注入"}]}},
        {"timestamp": "2026-10-31T08:22:18Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "帮我审计这个仓库"}]}},
        {"timestamp": "2026-10-31T08:22:19Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell"}},
        {"timestamp": "2026-10-31T08:22:20Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "先看目录结构。"}]}},
    ]
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
    res = codex.read_session(str(p))
    assert res.meta.session_id == "abc123"
    assert res.meta.cwd == "C:\\proj"
    assert [r.role for r in res.records] == ["user", "assistant"]
    assert res.records[0].text == "帮我审计这个仓库"
    assert res.meta.tool_call_count == 1


def test_claude_parses_text_blocks_and_skips_noise(tmp_path):
    p = tmp_path / "s.jsonl"
    lines = [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-06-20T01:25:07Z",
         "sessionId": "s1"},
        {"type": "user", "timestamp": "2026-06-20T01:25:08Z", "sessionId": "s1",
         "cwd": "c:\\proj",
         "message": {"role": "user", "content": [{"type": "text", "text": "你好"}]}},
        {"type": "assistant", "timestamp": "2026-06-20T01:25:09Z", "sessionId": "s1",
         "message": {"role": "assistant",
                     "content": [{"type": "thinking", "thinking": "…"},
                                 {"type": "text", "text": "你好！"}]}},
    ]
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
    res = claude.read_session(str(p))
    assert [r.text for r in res.records] == ["你好", "你好！"]
    assert res.meta.session_id == "s"          # 来自文件名
    assert res.meta.title == "你好"            # 首条 user 兜底


def test_hermes_parses_md_sections(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("\n".join([
        "---",
        'session_id: "20260611_133723_9721b1"',
        'title: "测试标题"',
        'source: "tui"',
        'created_at: "2026-06-11T05:37:26Z"',
        "message_count: 2",
        "---",
        "",
        "## Messages",
        "",
        "### User — 2026-06-11T05:37:28Z",
        "",
        "为什么后台停了？",
        "",
        "### Assistant — 2026-06-11T05:38:13Z",
        "",
        "因为参数写错了。",
        "",
        "## Tool calls",
        "```json",
        "[]",
        "```",
    ]), encoding="utf-8")
    res = hermes.read_session(str(p))
    assert res.meta.title == "测试标题"
    assert [r.role for r in res.records] == ["user", "assistant"]
    assert res.records[0].text == "为什么后台停了？"
    assert res.records[1].text == "因为参数写错了。"


def test_dsh_tool_calls_pairing_and_exit_code(tmp_path):
    """pwsh 命令与结果按 (turn,step) 配对；退出码双通道判定（2026-11-17 探针 A 实测）。

    夹具覆盖三种 result 形态：旧纯文本（exit code 标记）、新嵌套 JSON、无结果。
    写成 zstd 压缩（与线上 session.v4.jsonl.zstd 同格式）。
    """
    import io as _io
    import zstandard as _zstd
    p = tmp_path / "session.v4.jsonl.zstd"
    nested = json.dumps({
        "role": "tool",
        "toolCallId": "call_1",
        "content": [{"type": "text", "text": "out\n[exit code: 1]"}],
    }, ensure_ascii=False)
    lines = [
        {"type": "session", "id": "s1", "createdAt": 1731800000000, "cwd": "F:\\proj"},
        {"type": "tool/call", "turn": 1, "step": 10,
         "data": {"name": "pwsh", "arguments": json.dumps({"command": "python -c 'bad'"})}},
        {"type": "tool/result", "turn": 1, "step": 10,
         "data": {"message": "ok\n[exit code: 0]"}},
        {"type": "tool/call", "turn": 1, "step": 11,
         "data": {"name": "pwsh", "arguments": json.dumps({"command": "node x.mjs"})}},
        {"type": "tool/result", "turn": 1, "step": 11,
         "data": {"message": nested}},
        {"type": "tool/call", "turn": 2, "step": 1,
         "data": {"name": "pwsh", "arguments": json.dumps({"command": "git status"})}},
        {"type": "tool/call", "turn": 2, "step": 2,
         "data": {"name": "read", "arguments": json.dumps({"file_path": "a.py"})}},
        {"type": "tool/result", "turn": 2, "step": 2,
         "data": {"error": {"name": "FsError", "code": "FS_NOT_OBSERVED"}}},
    ]
    raw = ("\n".join(json.dumps(x, ensure_ascii=False) for x in lines)).encode("utf-8")
    cctx = _zstd.ZstdCompressor()
    p.write_bytes(cctx.compress(raw))
    res = dsh.read_session(str(p))
    tcs = res.tool_calls
    # 三条 pwsh：两条配上了结果，一条（git status）无结果落 NULL
    assert len(tcs) == 3
    assert [t.command for t in tcs] == ["python -c 'bad'", "node x.mjs", "git status"]
    assert tcs[0].ok == 1 and tcs[0].exit_code == 0
    # 嵌套 JSON 形态的退出码必须解出来（2026-11-17 探针 B 踩坑：不解会漏 90% 失败）
    assert tcs[1].ok == 0 and tcs[1].exit_code == 1
    assert tcs[2].ok is None and tcs[2].exit_code is None
    # read 工具的失败不进 tool_calls（不是命令类）
    assert all(t.tool == "pwsh" for t in tcs)


# ── 其它 agent 的 tool_calls（2026-11-17 补；夹具按各格式实测形态）────────
def test_codex_tool_calls_call_id_pairing(tmp_path):
    """Codex：exec_command 的 cmd 字段 + call_id 配对；退出码从 'Process exited with code N' 读。"""
    p = tmp_path / "rollout-x.jsonl"
    lines = [
        {"timestamp": "2026-11-17T00:00:00Z", "type": "session_meta",
         "payload": {"session_id": "c1", "cwd": "C:\\proj"}},
        {"timestamp": "2026-11-17T00:00:01Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "call_id": "call_a", "arguments": json.dumps({"cmd": "git status"})}},
        {"timestamp": "2026-11-17T00:00:02Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call_a",
                     "output": "Process exited with code 0\nOutput:\nclean"}},
        {"timestamp": "2026-11-17T00:00:03Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "call_id": "call_b", "arguments": json.dumps({"cmd": "pytest -q"})}},
        {"timestamp": "2026-11-17T00:00:04Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call_b",
                     "output": "Process exited with code 1\nOutput:\nfailed"}},
        # 非命令类工具不能进 tool_calls
        {"timestamp": "2026-11-17T00:00:05Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "view_image",
                     "call_id": "call_c", "arguments": json.dumps({"path": "a.png"})}},
        {"timestamp": "2026-11-17T00:00:06Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call_c", "output": "ok"}},
    ]
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
    res = codex.read_session(str(p))
    tcs = res.tool_calls
    assert [t.command for t in tcs] == ["git status", "pytest -q"], tcs
    assert tcs[0].ok == 1 and tcs[0].exit_code == 0
    assert tcs[1].ok == 0 and tcs[1].exit_code == 1


def test_claude_tool_calls_is_error(tmp_path):
    """Claude：Bash 的 command + tool_use.id ↔ tool_result.tool_use_id 配对，is_error 判成败。"""
    p = tmp_path / "s.jsonl"
    lines = [
        {"type": "user", "timestamp": "2026-11-17T00:00:00Z", "sessionId": "s1",
         "cwd": "c:\\proj", "message": {"role": "user",
                                        "content": [{"type": "text", "text": "跑一下测试"}]}},
        {"type": "assistant", "timestamp": "2026-11-17T00:00:01Z", "sessionId": "s1",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash",
              "input": {"command": "npm test"}}]}},
        {"type": "user", "timestamp": "2026-11-17T00:00:02Z", "sessionId": "s1",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
              "content": "1 failing"}]}},
        {"type": "assistant", "timestamp": "2026-11-17T00:00:03Z", "sessionId": "s1",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t2", "name": "Bash",
              "input": {"command": "npm test -- --run"}}]}},
        {"type": "user", "timestamp": "2026-11-17T00:00:04Z", "sessionId": "s1",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t2", "is_error": False,
              "content": "all pass"}]}},
    ]
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
    res = claude.read_session(str(p))
    tcs = res.tool_calls
    assert [t.command for t in tcs] == ["npm test", "npm test -- --run"], tcs
    assert tcs[0].ok == 0
    assert tcs[1].ok == 1


def test_workbuddy_tool_calls_status_and_error_prefix(tmp_path):
    """WorkBuddy：Bash 的 command + callId 配对；completed=成功，output 以 Error 开头=失败。"""
    p = tmp_path / "w.jsonl"
    lines = [
        {"type": "message", "role": "user", "sessionId": "w1", "cwd": "c:\\proj",
         "timestamp": 1763000000000, "content": [{"type": "input_text", "text": "看下目录"}]},
        {"type": "function_call", "name": "Bash", "callId": "k1",
         "arguments": json.dumps({"command": "ls -la"}), "timestamp": 1763000001000},
        {"type": "function_call_result", "callId": "k1", "status": "completed",
         "output": {"type": "text", "text": "total 0"}, "timestamp": 1763000002000},
        {"type": "function_call", "name": "Bash", "callId": "k2",
         "arguments": json.dumps({"command": "ls /nope"}), "timestamp": 1763000003000},
        {"type": "function_call_result", "callId": "k2", "status": "completed",
         "output": {"type": "text", "text": "Error: glob error: no such dir"},
         "timestamp": 1763000004000},
    ]
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
    res = workbuddy.read_session(str(p))
    tcs = res.tool_calls
    assert [t.command for t in tcs] == ["ls -la", "ls /nope"], tcs
    assert tcs[0].ok == 1
    assert tcs[1].ok == 0
