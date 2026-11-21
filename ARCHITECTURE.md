# Architecture

Four layers, bottom-up. Everything is stdlib + `zstandard` + `pyyaml`; no network, no model calls.

## 1. Collectors — `collectors/*.py`

Each client keeps its own on-disk format, so each gets an adapter that yields the same
normalised record shape (`base.py` defines it): session key, agent, cwd, message list,
tool calls.

| module | client | source of truth |
|---|---|---|
| `dsh.py` | DeepSeek Harness | `$DSH_HOME/sessions/**/session.v4.jsonl.zstd` (multi-frame zstd) |
| `codex.py` | Codex | `~/.codex/sessions/**/rollout-*.jsonl` |
| `claude.py` | Claude Code | `~/.claude/projects/**/*.jsonl` |
| `hermes.py` | Hermes | Hermes session store |
| `workbuddy.py` | WorkBuddy | CodeBuddy session store |
| `codebuddy.py` | CodeBuddy | same store, different layout |

Collectors are **read-only** and **idempotent**: `store.ingest()` upserts by session key, so
re-running ingest only picks up new or changed sessions.

## 2. Index — `store.py`

SQLite. Two FTS indexes over different tokenisation needs, plus a LIKE fallback:

- `messages_fts` — `unicode61` tokenizer: normal word/substring behaviour for latin and for
  CJK runs that tokenise cleanly;
- `messages_trgm` — trigram tokenizer: makes 1–2 character CJK queries work;
- `LIKE` fallback — last resort for queries the above miss, and the source of the
  `substring_note` accounting.

Why three: FTS5 `unicode61` does not index single CJK characters, so a two-character Chinese
query silently returns nothing. That failure is invisible without an explicit count, so
`search(..., explain_substring=True)` reports how many rows the fallback added and how many
matches were **not** returned because of `limit`.

Schema is versioned (`SCHEMA_VERSION`); `Store(path, read_only=True)` skips migration and
verifies the on-disk version matches the code instead of mutating a database it may not own.

## 3. Evidence — `cards.py`, `premises.py`, `procedures.py`, `diagnoses.py`

This layer is the reason the tool exists.

- **cards** — durable conclusions, each with a claim, evidence (message ids), a scope, and
  optional `verify` steps. `verify` is deliberately limited to *declarative* checks
  (`file_exists`, `dir_exists`, `path_env_contains`): a card can be re-checked mechanically,
  but a machine check never rewrites the claim. `cards validate --machine` reports which
  cards currently fail, and it never flips a card's status on its own — that would be
  the tool silently drawing a conclusion on the user's behalf.
- **premises** — the conditions a conclusion depends on. When a premise breaks, the
  conclusion becomes `dead` (no successor) or `dead_with_successor` (a later session
  superseded it). This is what turns "the model confidently repeats a two-week-old path"
  into "this fact is stale, here is what replaced it".
- **procedures** — reusable *behaviour*, mined two ways:
  - `repair`: inside one session, a failing command followed by a correction and a success
    (similarity-gated, so unrelated commands do not pair up);
  - `ritual`: the same command sequence in the same working directory across ≥3 sessions.
  Each procedure is certified as a quadruple: `certified_at` + `basis` (per-path counts) +
  `recheck` (when it stops being valid) + verdict. `proc_cache.py` materialises the mining
  result into `index/procedures-cache.json` so a query costs ~1 s instead of ~12 s.
- **diagnoses** — human-in-the-loop labels for repeated failures (stable id
  `rp_<sha1(bad\0good)[:12]>`). A diagnosed repair bypasses the confidence gate: a human
  said this pairing is the fix.

## 4. Surfaces — `cli.py`, `mcp_server.py`, `inject.py`, `install_host.py`

- `cli.py` — one entry point, subcommands grouped by layer (`ingest`, `search`, `timeline`,
  `cards …`, `procedure …`, `diagnose …`, `inject`, `mcp-serve`, `install-host`, `doctor`).
- `mcp_server.py` — stdio JSON-RPC 2.0, newline-delimited. Three read-only tools; every tool
  declares `annotations.readOnlyHint=true` (without it, hosts that gate MCP calls on
  approval will refuse to run them in non-interactive mode).
- `inject.py` — renders the compact "environment facts" block from premises + cards.
- `install_host.py` — writes a **marked** instruction block (`MARK_BEGIN` / `MARK_END`) into
  a host's instruction file, preserving line endings and BOM, and can revert it. Idempotent
  by construction: re-running replaces the block, it does not append.

## Testing

`tests/` runs offline against temporary databases and fixture session files. The suite is
the contract for the behaviours above; if a change makes a test pass by weakening an
assertion, that is a regression, not a fix.
