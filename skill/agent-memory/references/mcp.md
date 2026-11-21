# MCP server：跨宿主接入（Codex / Claude Code / 其他）

> 状态：**只读、3 个工具、走缓存**。设计经 glm-5.3 复核并按 C6–C12 修正，
> 设计与复核记录见本仓库的 `docs/`（不在包内，属于维护者材料）。

## 它提供什么

| 工具 | 用途 | 耗时（实测） |
|---|---|---|
| `memory_context(entity)` | 项目「索引层」记忆块：环境事实（目录改名/失效路径，每条带认证时刻与依据）+ 项目常用操作（带前提判定）+ 块成本 | ~1.2 s |
| `memory_search(query, limit?, peer?)` | 检索本机多 agent 历史会话；**带回溯字段**（via/relaxed/alias_note/cross_note） | ~10 ms |
| `memory_timeline(entity, intent?, per?)` | 某项目某话题的结论演变 | ~1 s |

**没有写工具**：MCP 调用方是 agent，若允许它写"人类诊断"，`by` 字段会冒充人类，
正好破坏「人类诊断 > 自判」的立论。诊断请用人类路径：`am diagnose add <id> "<原话>"`。

## 前置

```powershell
# 1) 索引库与缓存（日更会自动做，也可手动）
am ingest
am procedure --refresh-cache      # 约 12 秒；不建缓存则首次 memory_context 会现算 12 秒

# 2) 自检
am doctor                          # 应 0 error / 0 warn
（仓库内可选）协议自检脚本：21 条冒烟，全过再配宿主；该脚本不进包。
```

## Codex

`~/.codex/config.toml`（Windows 用绝对路径；`command` 指向 .venv 的 python）：

```toml
[mcp_servers.agent-memory]
command = "<技能安装目录>\\.venv\\Scripts\\python.exe"
args = ["<技能安装目录>\\mcp_server.py"]
startup_timeout_sec = 20
tool_timeout_sec = 120          # memory_context 约 1.2s；留余量给首次现算
```

改完**重启 Codex 会话**（MCP 配置在会话启动时读取）。

### ⚠️ Codex 非交互模式（`codex exec`）的两个坑

1. **工具必须声明 `annotations.readOnlyHint`**（本 server 已声明）。
   Codex 的 `requires_mcp_tool_approval()`（上游 `codex-rs/core/src/mcp_tool_call.rs:2840`）
   在**未声明**只读注解时默认"需要审批"；而 `codex exec` 没有交互审批通道，
   于是调用被取消，模型看到的是 `user cancelled MCP tool call`，返回值为空
   （2026-11-17 实测事故：用户跑 `codex exec` 得到 HITS=0，真因就在这里）。
2. 兜底（可选，配置文件层）：给 server 或单个工具设审批模式

```toml
[mcp_servers.agent-memory]
default_tools_approval_mode = "auto"      # Auto=按 annotations 判；Approve/never 等见上游文档

# 或按工具单独设：
# [mcp_servers.agent-memory.tools.memory_search]
# approval_mode = "auto"
```

字段名已在上游源码核实（`custom_mcp_tool_approval_mode()` 读
`tools.<name>.approval_mode` 与 `default_tools_approval_mode`）。

## Claude Code

```powershell
# 项目级（推荐：只影响这个仓库）
claude mcp add agent-memory -- "<技能安装目录>\.venv\Scripts\python.exe" "<技能安装目录>\mcp_server.py"

# 或全局
claude mcp add -s user agent-memory -- "<技能安装目录>\.venv\Scripts\python.exe" "<技能安装目录>\mcp_server.py"

# 查看 / 删除
claude mcp list
claude mcp remove agent-memory
```

也可以手写 `.mcp.json`（项目根）：

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "<技能安装目录>\\.venv\\Scripts\\python.exe",
      "args": ["<技能安装目录>\\mcp_server.py"]
    }
  }
}
```

**非交互模式**（`claude -p "…"`）要显式放行工具，否则同样会被权限拦截：

```powershell
claude -p "调用 memory_search 查 质保，只回 HITS=n" `
  --allowedTools "mcp__agent-memory__memory_search"
```

## 与「指令片段」的关系（两者互补，不互斥）

- **指令片段**（`am install-host`）是"推"：告诉 agent **开工时调一次 `memory_context`**。
  MCP 工具**不会**被 agent 主动调用——没有这一行指令，它多半不会用（这正是本仓最忌的
  "静默不用"失败模式）。
- **MCP** 是"拉"：调用时才查库，拿到的是**当时**的事实，不会读到昨天写死进 context.md 的旧内容。

所以推荐组合：`install-host`（一行指令）+ MCP（取新鲜数据）。

## 安全边界（可复核）

1. **只读连接**：`Store(read_only=True)` 以 SQLite `mode=ro` 打开；误写会抛
   `attempt to write a readonly database`（响亮失败，不是静默）；
2. **无写工具**：`tools/list` 里只有 3 个 `memory_*` 读工具；
3. **不写 `index/`**：`mcp_server.py` 对 `index/` 无写代码路径（`grep -n "open(.*'w'" mcp_server.py`
   只应命中 stderr 日志）；
4. **不联网、不执行命令**：无 `subprocess`、无 `requests`；
5. **stdout 只有协议**：日志走 stderr（宿主对 stdout 非 JSON 行会判定工具不可用）。

## 已知限制（如实）

- `memory_context` 的 procedure 段来自缓存，**默认 TTL 24h**；缓存过期/索引库变动会在
  返回文本里明写「procedure 缓存：…」，不会静默用陈旧数据。
- 首次调用若缓存缺失，会现算约 12 秒（`tool_timeout_sec` 需留余量）。
- `memory_search` 的中文 1~2 字查询走 LIKE 全表回退（约 10 ms，当前语料 46k 消息）；
  语料涨 10 倍后需重新评估（复核 C18）。
