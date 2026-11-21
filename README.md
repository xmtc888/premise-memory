# multiagent-memory

> 多 Agent 记忆库：把同一台机器上 6 个编码 agent 的会话汇成一个**可检索、可校验**的本地库。
> 原名 `agent-memory`（技能包 slug 未改）。

Local, **zero-model** session memory for coding agents. It indexes the sessions that
DSH, Codex, Claude Code, Hermes, WorkBuddy and CodeBuddy already keep on your machine,
then answers questions like *"what did we decide about X"*, *"does that decision still
hold"*, and *"which conclusion came from which session"* — without calling an LLM and
without sending anything off the machine.

> 中文一句话：把一台机器上多个编码 agent 的历史会话汇成一个可检索的本地库，查旧结论、
> 看话题怎么变的、验前提是否已失效、并把可复用的操作程序挖出来。**不调模型、不联网。**

## Why another memory tool

| | claude-mem / hosted memory | multiagent-memory |
|---|---|---|
| Retrieval | embeddings or LLM summarisation | **no model at all** — SQLite FTS5 (`unicode61` + `trigram`) with a LIKE fallback for 1–2 character CJK queries |
| Data | often a hosted service | everything stays in `./index/*.db`; no network calls in the engine |
| Staleness | usually not modelled | **premises**: an old conclusion carries machine-checkable conditions (`file_exists`, `dir_exists`, `path_env_contains`); when they break you get `dead` / `dead_with_successor` instead of a confident stale answer |
| Reuse | "remember this fact" | **procedure memory**: repeated successful command sequences (`ritual`) and fail→fix→succeed sequences (`repair`), each certified with a basis + recheck condition |
| Host integration | one client | collectors for 6 clients + a read-only MCP server + instruction-snippet install for Codex / Claude Code |

If you want semantic recall over arbitrary text, this is the wrong tool. If you want
*auditable* recall — where every answer can be traced to a session, a message id, and a
checkable premise — this is what it is built for.

## Quick start (Windows)

```powershell
git clone <this-repo> multiagent-memory
cd multiagent-memory
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

.\.venv\Scripts\python.exe cli.py init          # create index/
.\.venv\Scripts\python.exe cli.py ingest        # scan local agent logs
.\.venv\Scripts\python.exe cli.py stats
.\.venv\Scripts\python.exe cli.py search "premise verification"
.\.venv\Scripts\python.exe cli.py timeline "<topic>"
```

Optional: `.\.venv\Scripts\python.exe cli.py inject` prints a compact "environment facts"
block you can paste into a new session; `cli.py mcp-serve` starts the read-only MCP server
(3 tools: `memory_search`, `memory_context`, `memory_timeline`).

## How it works

```
client logs (dsh / codex / claude / hermes / workbuddy / codebuddy)
   │  collectors/*.py  (read-only, per-client format adapters)
   ▼
index/memory.db   sessions · messages · tool_calls · topics
   │  store.py (FTS5 + trigram + LIKE fallback) · topics.py · timeline.py
   ▼
evidence layer   cards.py (conclusions + declarative verify) · premises.py (staleness)
   │              procedures.py (rituals / repairs) · diagnoses.py
   ▼
surfaces         cli.py · mcp_server.py · inject.py
```

Design rules worth knowing:

- **Read-only by default.** `Store(path, read_only=True)` opens SQLite with `mode=ro`; the
  MCP server exposes no write tool (`annotations.readOnlyHint=true`, so hosts do not prompt
  on every call).
- **No silent truncation.** Search results carry `returned` / `limit` / `truncated` /
  `substring_note`, and the CLI can print a substring-vs-token accounting (`--explain`).
- **Explicit uncertainty.** Facts are labelled by evidence class (A: first-party text,
  B: second-hand or derived, C: must be verified on site); C-level items are kept in a
  separate "unverified" list instead of being stated as fact.
- **Machine checks never flip a conclusion.** A failing premise marks a conclusion stale;
  it does not rewrite it.

## Limits (read before trusting it)

- **Windows-first.** The engine avoids POSIX-only APIs, but the test suite and the
  collector fixtures were developed on Windows; other platforms are best-effort only.
- **Lexical, not semantic.** A query that shares no tokens with the stored text will not
  find it. Trigram matching helps CJK, but it is still string matching.
- **Log formats drift.** Collectors are per-client adapters; when a client changes its
  on-disk format, that collector needs an update (it fails loudly rather than silently
  returning nothing).
- **Coverage is per-machine.** The index only knows what exists on this machine.

## Privacy

- No network access in the engine, no API keys, no telemetry.
- The database lives in `./index/` (git-ignored). Deleting that directory deletes
  everything the tool knows.
- If you fork this to run on a shared machine, treat `index/` as sensitive: it contains
  session history.

## License

MIT — see [LICENSE](LICENSE).
