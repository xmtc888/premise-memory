# 变更日志（agent-memory 技能 / 引擎）

> 版本号规范（借鉴 Helm 与 npm）：**包版本 = 技能与引擎的版本**，与「数据 schema 版本」
> 分开记（后者存在索引库 `PRAGMA user_version`，`am.ps1 doctor` 会同时报出两者）。
> 改动**必须**先改这里的条目、再升 `SKILL.md` 的 `version`，否则 `pack_platform.ps1` 会拒绝打包。

## [1.8.3] — 2026-11-20

> 主题：**修复"包里承诺了、包里没有"的死引用**（打包完整性）。经 glm-5.3 复核后执行。

### 修复：`SKILL.md` 指向不进包的文件

`SKILL.md` 曾写着 `python tools/research/gh_scan.py …`，并让人看
`docs/2026-11-19_竞品情报快扫流程.md` —— 但 `tools/` 与 `docs/` **从不进包**
（平台只允许根目录 + 一层子目录）。装包用户照做就是「文件不存在」。

- 脚本进包：`scripts\gh_scan.py`（仅标准库，无新依赖）；
- 方法文档进包：`references/gh-scan-playbook.md`（ASCII 文件名；中文名进 zip 在 Windows
  解压有乱码风险，复核第 2 条）；
- `SKILL.md` 改为包内路径，并明确两件事：**必须显式给 `--out`**（默认值会在装包目录里
  长出 `docs/`，污染技能目录）、**不设 `GITHUB_TOKEN` 走匿名限流**（别让人误以为一定能跑出结果）。

### 新增：死引用闸门 `tools/check_pack_refs.py`

判据（复核第 6 条）：**装包用户会不会照这条动手？** 行动型引用必须指向包内真实文件；
出处型叙述（"源自 tools/…"）可留。检查项：

1. 出现 `tools/`、`docs/` 路径引用；
2. 出现本机绝对路径（`F:\`）；
3. 反引号里引用的 `.py/.ps1` 不在包内。

命中即 `PACK FAIL`。已接入 `pack_platform.ps1`（与私有词/控制字符闸门并列）。

### 说明

- 版本号 1.8.2 → 1.8.3（patch）：这是打包/引用修复，不是新功能；
- 仓库里的 `docs/2026-11-19_竞品情报快扫流程.md` 保留（面向开发者、含仓库内路径），
  包内看 `references/gh-scan-playbook.md`；两份内容对应，各自面向不同读者。

## [1.8.2] — 2026-11-19

> 主题：**竞品情报快扫工具** `tools/research/gh_scan.py` —— 把"发现 + 采数"机械化，
> 人只做判定。配套方法：`docs/2026-11-19_竞品情报快扫流程.md`。

### 为什么

前三次同类项目调研（11-17 生态调研、11-19 两篇公众号对比）全是手工：curl 逐个抓 JSON、
人眼比 README、手写表。两个后果：慢；而且**手工算错过**——MemoraX 的客户端重叠我写成了 3/6，
实际 5/6（漏了 README 第一行的 Codex 与 Claude Code），是 GLM 复核才抓出来的。
这类**可机械计算的数字**不该再用眼睛算。

### 新增

```powershell
python tools/research/gh_scan.py --query "topic:agent-memory" --limit 12   # 发现
python tools/research/gh_scan.py --repo owner/name                         # 点名
python tools/research/gh_scan.py --query "..." --prev docs/research/repos/<日期>.json  # 算 Δstars
```

机器做三件事：① 指标（stars/forks/created/pushed/许可/归档）；② README 事实命中
（客户端、云/账号、训练叙事、前提失效、遥测上传）；③ **与我方 6 个来源的客户端交集**（这列已实测：
MemoraX 算出 5/6，与 GLM 复核结论一致；Super Kanban 算出 1/6）。

### 三条硬规矩（写进脚本，都是踩出来的）

1. **命中一律不算事实**：所有事实列只表示"README 里能 grep 到"，判定必须回一手材料
   （MemoraX 的 training 词就是教训）；
2. **否定句要反向读**：`It does not upload ...` 裸 grep `upload` 会得出相反结论 —— 脚本做否定检测并标「否定句?」；
3. **发现模式必有噪音**：本次 31 个 repo 里约 5 个是数据库/书/框架（tidb、dolt、haystack、一本 AI Agent 书），
   过滤顺序 = 客户端交集 → desc/topics → 人工扫一眼。

### 快照

产出 `docs/research/repos/<UTC 日期>.json|.md`，JSON **进库**（`--prev` 靠它算增速，别只放 gitignore 的 `work/`）。

## [1.8.1] — 2026-11-19

> 主题：**修 `cards influence` 的假阳性** —— 重启 DSH、第一次真跑闭环时暴露的口径问题。

### 问题（实测，不是推测）

第一版按"assistant 回复里**任意出现**卡片 id"统计。重启后首次 `ingest --agent dsh`
+ `cards influence` 报：`c-fe68cca5` 3 次、`c-51f1f5ea` 1 次，全部来自**上一个会话**。
而那个会话是在**讨论这套披露机制本身**（回复里出现 id 是因为在解释它），
并不是"实质采用了该结论去回答问题"。

**「提到」≠「引用」**：任意匹配会把讨论机制、写文档、解释功能的回复全算进引用率。

### 修法

`store.id_mentions(strict=True)`（默认）只认**行首披露句式**：
`^(本次)?受 agent-memory 卡片 c-xxxxxxxx 影响`（允许列表前缀、可选"本次"、大小写与连字符变体）。
SQL 先 `LIKE '%agent-memory 卡片%'` 粗筛、再用正则精确判行首，避免整表读进 Python。

- `cards influence` 默认 strict，输出里**明写口径**；
- `--loose` 保留任意提及口径，只用于排查"到底在哪提过"，**不能当引用率**；
- 回归测试钉死这次假阳性：`test_strict_ignores_mechanism_discussion`。

### 同一库同一时刻的实测对照

| 口径 | 结果 |
|---|---|
| strict（默认） | 被引用 **0** 张（正确：上一会话只讨论机制，没有行首披露） |
| `--loose` | `c-fe68cca5` 3 次 / `c-51f1f5ea` 1 次（即假阳性） |

## [1.8.0] — 2026-11-19

> 主题：**记忆影响披露** —— 让"这条记忆有没有被用上"变成一句话 + 一个可机械核验的数字。

### 为什么

对比 MemoraX Code 时发现它有一件事我们没做（`docs/2026-11-19_对比MemoraXCode_托管记忆vs本地可校验.md` §3）：
它在回答开头说明"这次是哪条记忆实质影响了任务"。我们的注入块同样占了模型上下文，
但**没人知道它有没有用**——这正是 11-17 调研里"成本可见"的另一面（影响可见）。

### 新增：披露要求 + id 锚点 + 引用统计

三层，缺一层就退化成口号：

| 层 | 做什么 | 在哪 |
|---|---|---|
| 注入 | 每条卡行尾带 `c-<sha8>`（`--no-card-ids` 可关） | `inject.py` `build_cards_block` |
| 披露 | 块尾一行固定要求：实质采用某条时，回答开头写「受 agent-memory 卡片 c-xxxxxxxx 影响」；没用到就不写、不要编 id | 插件 `lib/block.mjs` `DISCLOSE_HINT`（**v0.3.0，需重启 DSH**） |
| 核验 | `cards influence`：只统计 assistant 消息里出现的卡片 id，报引用次数/最近会话/未引用数 | `store.id_mentions` + `cli.py` |

**为什么要求带 id**：id 是注入块与回答里**同一串字符**，"模型自称用了哪条"和"统计到哪条"
才能对账。这也解释了为什么不许编 id——编了会直接污染统计。

### 边界（如实说）

- 历史会话里没有 id（id 是本次才进注入块的）⇒ 引用统计从启用时刻开始，
  `cards influence` 会说明"零引用"的两种原因，不把"还没开始"讲成"没用"；
- 披露是**软约束**（模型可能不遵守），但核验是硬的：不写就没有 id，统计里就是零。
  这一层结构至少让"没披露"和"披露了但没写"可区分（后者会在统计里出现"有引用但无对应会话记录"）。

### 兼容性

- 纯增量；`--no-card-ids` 可退回旧格式；
- 插件从 0.2.0 → **0.3.0**（行为变更 minor），仓库与 `vendor/dsh-agent-memory-inject` 已同步，
  **需重启 DSH** 生效；
- 新增测试 `tests/test_cards_influence.py`（8 条：id 进块/可关/空作用域边界、
  只认 assistant、since 过滤、未知 id 归零、空输入）。

## [1.7.0] — 2026-11-19

> 主题：**人工确认账本**（`cards ack`）—— 注入不再天天推同一句话，但也不会把结论埋掉。

### 为什么

注入是每轮成本。卡片攒到几十张后，"同一句话每天在 system prompt 里出现一遍"就是纯噪音；
而人又不能靠"删卡"解决（删了就丢了经验）。

### 新增：`cards ack` 确认账本

```powershell
am cards ack <卡id>          # 确认：内容不变期间不再注入
am cards ack --show          # 账本 + 每条现状态（免打扰/内容已变/过期/卡已删）
am cards ack <卡id> --undo   # 撤销确认
am cards ack --prune         # 清理失效条目
am cards ack <卡id> --by codex   # 记录谁确认的（默认 human）
am inject --cards            # 默认跳过已确认（--include-acked 忽略）
```

- 账本是**独立文件** `index/cards-ack.jsonl`，不写进 `cards.jsonl`：确认是"阅读状态"，
  卡片是"内容"，生命周期不同；
- 记录**内容指纹**（claim/status/topic/scopes/evidence 的 sha12，**不含时间戳**）——
  同内容重写不让确认失效，`status` 被巡检改成 `stale` 必须失效；
- **两条自动恢复路径**：指纹变（如巡检改状态、证据增删）或超 TTL（默认 30 天，`--ack-ttl-days` 可改）；
- 跳过数写进注入统计行（`已确认跳过 N`）——"少推"必须可解释；
- 账本坏了按"没确认"处理：注入不会因账本损坏而整块消失；写入是原子替换。

### 兼容性

- 纯增量：卡片文件格式不变、无需迁移；账本为空时行为与 1.6.1 完全一致；
- `cards export/import` **不带**账本（确认是这台机器上的阅读状态，不是结论内容）；
- 确认只影响注入，`cards list/search` 永远能看到全部卡。

### 已知边界

DSH 注入插件只保留以 `- [` 与标题开头的行，所以 CLI 里那句"另有 N 条已确认"**不进 system prompt**——
插件路径是静默少推（统计只在 CLI `inject` 输出可见）。要它可见得改插件的 `extractBlock`。

## [1.6.1] — 2026-11-18

> 主题：**机器状态类结论的活体验证**（`cards validate --machine`）+ 一处自环 bug 修复。

### 新增：卡片可带声明式活体检查

有些结论说的是"本机现在是什么样"（哪个 `codex` 在 PATH、目录还在不在），
它的证据不是"谁说过什么"，而是**当时的机器状态**——用聊天消息锚定它，等于把
"可复核"降级成"有人说过"。现在这类卡可以带检查项，由 `am cards validate --machine`
逐条跑：

```powershell
am cards add --claim "…" --ev "…" `
  --verify-spec "dir_exists:C:\path\to\tool" `
  --verify-spec "path_env_contains:C:\path\in\PATH"

am cards validate --machine      # 通过=仍成立；失败则退出码 1 并逐条说明
```

- 只允许三种**声明式**检查：`file_exists` / `dir_exists` / `path_env_contains`；
- **不执行任意命令**（卡片是本机数据文件，让它能跑命令等于开后门；需要命令的场景应写进 `tools/`）；
- 检查失败**如实报出**（退出码 1），不静默、不自动改状态；
- 日更 `refresh.ps1` 会跑一次并把结果写进日志（失败只记日志，不中断当日 ingest）。

### 修复：卡片"推翻自己"的自环

同一 claim 重新 `cards add` 且顺手 `--supersedes-card <它自己的 id>` 时，`upsert` 命中同一张卡
⇒ 卡片把自己写成 `supersedes`，渲染成「⇢ 推翻了 c-xxxx」（就是它自己）。现在 `upsert` 会
剔掉与自身 id 相同的条目，并有测试钉住。

### 说明

- `verify` 是**可选字段**，旧卡片不受影响；`cards validate` 会校验其结构（未知类型/缺字段即报错）；
- 与「证据锚点」的关系：锚点回答"谁说的、原文在哪"，活体检查回答"现在还是不是这样"，
  两者互补——机器状态类卡优先用后者。

## [1.6.0] — 2026-11-17

> 主题：**procedure memory（程序性记忆）+ 跨宿主接入**。1.5.20 之后的所有改动都在本版。

### 新增：`tool_calls` 采集（全部 agent）

原先只索引"人说的话 / agent 的结论"；现在把**执行过的命令**也落库（表 `tool_calls`），
这是 procedure memory 的原料。

- 覆盖 DSH / Codex / Claude Code / WorkBuddy / CodeBuddy / Hermes；
- 命令与结果按各宿主的结构化键配对（DSH 用 `(turn,step)`、Codex 用 `call_id`、
  Claude 用 `tool_use.id ↔ tool_use_id`、WorkBuddy 用 `callId`）；
- 失败判定双通道：结构化 `error` 键 与 结果里的 `[exit code: N]` / `Process exited with code N`；
- schema 升 v2（`CREATE TABLE IF NOT EXISTS`，老库免迁移）。

### 新增：`am procedure` —— 程序性记忆（零模型）

从 `tool_calls` 挖两类可复用步骤：

- **修复对**（"怎么修"）：失败 → 5 条内同程序改正 → 成功，且两条命令**足够相似**
  （字符级相似度，避免把"换了个脚本继续做"误判成修复）；
- **仪式**（"怎么做"）：同目录跨 ≥3 会话重复的命令（如 `cd <项目>` +
  `$PYTHONIOENCODING` + 项目 venv 的 python）。

配套：

- `--judge` 给每条挂**前提判定**（🟢活 / 🟡项目已迁移 / 🔴前提已失效 / ⚪不可判）；
- 每条带**认证四件套**：认证时刻 + 依据（"2 条存在；1 条已迁移"）+ 失效条件；
- 注入块里**每条**也带认证标记，只注入前提没失效的；
- 结果按需缓存（`index/procedures-cache.json`，日更预计算）——现挖约 12 秒，
  缓存后工具/注入毫秒级。

### 新增：人类诊断 `am diagnose`

依据市场调研（WASP-Mem）：**人定的规则化组织会拖低性能，一轮人类诊断胜过四轮自判**。

- `am diagnose list` 列待诊断候选（带**稳定 id**，由归一化形态对哈希得出）；
- `am diagnose add <id> "<人话>"` 落盘人的原话（**逐字保存**，不改写）；
- 已诊断 = 强证据：不需要跨会话重复也会进注入块，并标注「人已诊断」。

### 新增：注入块成本可见 + 分层

- 块头写**本块 token 成本**（各段分列）与展开命令：块是**索引层**，细节按需展开；
- `inject --procedures` 追加"本项目常用操作/修法"（按项目作用域挑、前提已校验）。

### 新增：跨宿主接入

- **指令片段**：`am install-host codex|claude` 把「开工先读 `context.md`」装进
  `AGENTS.md` / `CLAUDE.md`（标记块，`--dry-run` 预览、`--revert` 逐字节还原）；
- **MCP server**（`mcp_server.py`，标准库零依赖）：3 个**只读**工具
  `memory_context` / `memory_search` / `memory_timeline`，供 Codex / Claude Code 等
  通过工具调用取**新鲜**记忆（推 + 拉互补）。工具声明 `annotations.readOnlyHint`，
  否则 Codex 会在非交互模式把调用判成"需要审批"并取消。

### 修复

- **Codex 漏采**：新版工具名是 `shell_command`（旧名 `exec_command`），原先只认后者 ⇒
  83 个有工具调用的会话里只有 13 个能抽到命令（漏 84%）；
- **检索口径**：`memory_search` 的 `meta` 拆成 `returned / limit / truncated /
  substring_note`——短中文词走 FTS **token** 匹配，会漏掉"含在更长词里"的消息
  （实测 `质保` token 命中 94、子串命中 325），必须如实报缺口；
- **`inject --check` 失明**：块头 MARKER 被挤到 400 字符之外时误报"不是 agent-memory 生成的"
  （现读 4000 字符，且顺序纪律写进代码注释）；
- 环境事实措辞：目录还在但文件不在原位时，不再说成"目录已不存在"。

### 说明

- 数据 schema **v2**（v1 库首次运行会提示先跑 `ingest`）；
- 打包期机器检仍全过：层级 ≤1、SKILL.md 在根、私有词 0 命中、控制字符 0；
- 新增 Python 模块：`procedures.py` / `proc_cache.py` / `diagnoses.py` /
  `install_host.py` / `mcp_server.py`（已加入打包清单）。

## [1.5.20] — 2026-11-10

> 改进清单第 8、9 条（收尾）。1.5.19 未发布，本版含其全部内容。

### 新增：`cards distill` —— SOP 结晶（零 LLM）

同 topic 下攒了 N 张卡之后，人只能 `cards list --topic` 一条条看，看不出"这件事到底怎么做的"。
市场里 `@a9i5k4/dsh-auto-memory` 讲 skill crystallization、`@weibaohui/hermes-loop` 讲"提炼成技能"，
但都要 LLM —— **我们只做确定性重排**：同样的输入永远产出同样的文本（有测试钉住）。

- `cards distill --topic X [--dst 路径]`：固定四段
  `一、前提` → `二、结论` → `三、证据` → `四、历史：被推翻的做法（勿照做）`。
- **前提只取还活着的卡**：dead 卡的 `scope` 是"当时"的适用条件，已被推翻，不能当现在的前提
  （第一版没过滤，实测把一张已推翻卡的 `v1.0 时代` 当成了当前前提）。
- 文件头明说"**草稿，不是结论文档**"，并如实统计证据里有多少条带 `msg` 锚点（跨机不可回查）。
- 不自动提交、不落 docs —— **人工审阅后**才该落。

### 新增：DSH 注入插件 `plugin/dsh-agent-memory-inject`（实验性，**默认未激活**）

这是清单第 9 条：agent-memory 是查询期记忆，缺的是"模型开口前把相关结论递上去"。
插件挂 `agent/pre-step` waterfall，调用 `cli.py inject --cards` 并把结果作为一条 user 消息追加。

四条保守策略（宁可不注入，也不污染上下文/不破坏宿主）：
**幂等**（本轮已有注入块就跳过）/ **冷却 120s**（不每步 fork Python）/ **CLI 超时 3s** /
**任何异常原样 `next()`**。

契约不是猜的：`@local/dsh-runaway-guard` 的源码注释记录了 DSH 的 hook 形状
（waterfall `(payload,next)`、返回 `{kind:"enter",messages}`、**arity 承重**、改写要 spread）。

- 自带 `install.ps1`：**只复制文件 + 打印 YAML，不改 `cordis.patch.yml`**（插件条目需重启，
  自动改风险太大）；`-Uninstall` 可回滚。
- 8 条 node 单测只覆盖纯逻辑（幂等/冷却/空输出/不改原数组/标记）；
  **宿主 hook 的实际行为未验证** —— 需重启 DSH 才能验，所以标为实验性、默认不启用。

### 测试
- `tests/test_cards_distill.py` 9 条：四段结构 / 前提排除 dead 的 scope / 被推翻进历史并划删除线 /
  草稿声明 / **确定性**（同输入同输出）/ 锚点统计 / 空 topic / 状态计数 / 不给 dst 不写盘。
- 插件 `node --test test/block.test.mjs` 8 条。
- 全量 pytest **258 passed**。

### 说明
- 插件是**可选项**：不装、不激活，agent-memory 的一切功能不受影响。
- 激活步骤与回滚写在 `plugin/dsh-agent-memory-inject/README.md`；
  重启后若钩子不触发，再试 `inject: ["agents"]`（别瞎声明服务，声明不存在的会让插件不激活）。

## [1.5.19] — 2026-11-10

> 改进清单第 7 条。1.5.18 未发布，本版含其全部内容。

### 改进：导出把「被推翻」置顶，并带去重键

借鉴 `dsh-gokifu-compact` 的「failure memory that survives compaction / transposition detection」：
迁移/重排之后，"哪条被推翻过"必须**还在且显眼** —— 它比成立的结论更值钱（照着做会重犯同一个错误）。

- `export --md`（给人看）改成**固定三段**：
  `## 一、已被推翻（勿再采信）` → `## 二、仍成立 / 未验证` → `## 三、前提已变（用前先核实）`；
  每段内按 topic 分组；dead 段带一句"它们比成立的结论更值钱"的说明。
- `export`（JSONL，给机器/换机）按 `dead → stale → alive → unverified` 排序，**dead 置顶**。
- 每张导出的卡带 **`dedupe_key`**（规范化 claim 的 sha8）：
  **不用 id** —— id = sha8(claim+"||"+topic)，同一句断言挂到两个 topic 下会得到两个 id，
  而那正是最需要被发现的"重复登记"。`dedupe_key` 只写进导出副本，**不改库**。
- `import`（merge/replace）合并后报告 `duplicate_groups`：库里有几组"同一断言、多个 id"，
  并写入 warnings —— **只报告，不自动合并**（自动合并会丢证据，裁决要靠人）。

### 实测

```
cards export --md  → 共 2 张卡｜被推翻 1／仍成立 1／前提已变 0
                     ## 一、已被推翻（勿再采信）  ← 置顶
cards export       → dead c-844d1fcb dedupe_key=cedc118b
                     alive c-51f1f5ea dedupe_key=ba7d8dc1
```

### 测试
- 新增 `tests/test_cards_export.py` 8 条：JSONL dead 置顶 / 导出加 dedupe_key 且不改库 /
  dedupe_key 忽略 topic 与标点（id 不同但 key 相同）/ 重复分组 / 导入报重复组但不合并 /
  Markdown 三段顺序 / 重复登记提示 / 被推翻标注。全量 **249 passed**。

### 说明
- 重复检测的判据是"规范化 claim 完全相同"（去标点空格、小写）；**近义改写不在此列**
  （那属于矛盾/合并判断，需要语义，我们不做 LLM）。
- 自动合并**明确不做**：合并错误不可逆，而报告错误只是多看一眼。

## [1.5.18] — 2026-11-10

> 改进清单第 6 条。1.5.17 未发布，本版含其全部内容。

### 新增：`cards conflicts` —— 矛盾检测（只报告，不自动改）

**问题**：`supersedes` 链一直靠**人工**维护。两张卡说相反的话，系统不发现、人也没注意，
它们就会同时"成立"。市场里 `memory-eternal` 讲自研去重、`dsh-mneme` 讲 autoDream consolidation：
共识是同一主题的多条记忆需要裁决。但我们**不做**自动合并（要 LLM，且规则抽取噪音高），
只把可疑配对摆出来。

### 判据（零 LLM，可解释，三条同时满足才报）

1. 同 `topic`，且都是 `alive`/`unverified`（dead 是历史，不参与）；
2. **未建立** `supersedes`/`superseded_by` 关系 —— 已裁决过的矛盾不必再提；
3. 剥掉否定词与情态动词后，两条 claim 的**汉字 2-gram Jaccard ≥ 0.6**，且**否定极性不同**。

关键细节（第一版踩过）：只剥否定词会**不对称** —— `要机器检` 剥完还是 `要机器检`，
`不要机器检` 剥完是 `机器检`，相似度只有 0.62，卡在阈值边缘。改成**否定与肯定情态成对剥离**
（`不要/不用/禁止/避免/别/不/没/无/勿` ↔ `要/需要/必须/应该/允许/推荐/可以`），两侧都变成
`机器检`，相似度 1.00。

### 两条验收标准（写进测试）

1. 造一对"否定翻转"矛盾卡（`要机器检` vs `不要机器检`）⇒ **必须抓到**（相似度 1.00）；
2. 现有真实卡 ⇒ **误报必须为 0**（`test_real_library_has_no_false_positive` 直接跑真库）。

同时钉住"不该报"的三种：同极性高相似（那是重复，不是矛盾）、不同主张（`机器检` vs `人工检`，
相似度 0.33）、不同 topic。

### 实测

```
cards conflicts --topic 矛盾测试
→ 发现 1 对：相似度 1.00 ｜ A c-725a2ce9 [alive] 要机器检 ／ B c-3003b545 [alive]（含否定）不要机器检
→ 裁决方式：cards supersede --old-id … --new-id …（系统不会自动改）

建 supersede 后再跑 → 未发现疑似矛盾（已裁决的不重复报）
真实卡（2 张）      → 未发现疑似矛盾（0 误报）
```

### 测试
- 新增 `tests/test_cards_conflicts.py` 10 条：否定翻转必抓 / 不可用-可用必抓 /
  同极性不报 / 不同主张不报 / 不同 topic 不比 / 已 supersede 不报 / dead 不比 /
  相似度对称性 / 否定极性识别 / **真实库 0 误报**。全量 **241 passed**。

### 边界
- 只认**否定翻转**这一类矛盾；"数值不一致"（`工期 30 天` vs `45 天`）、"范围冲突"等更复杂的
  矛盾没有判据，不假装能测。
- `--min-sim` 可调（更高更严）；调低会引入更多候选，但**误报由你承担**，默认 0.6。

## [1.5.17] — 2026-11-10

> 改进清单第 5 条。1.5.16 未发布，本版含其全部内容。

### 新增：卡片写入审计（`index/cards-audit.ndjson` + `cards log`）

**为什么现在做**：上一版（1.5.16）的巡检是**第一处会自动改卡片状态**的功能，而写卡的可能是
4 个不同 agent。没有审计，就追不到"谁把它变成 stale 的、改前是什么样"。
市场里 `dsh-memento` 把「auditable」写进定位、`dsh-engram` 有「audit」——
多写多改之后，**变更留痕比功能本身更基础**。

- 每次 `add` / `update` / `supersede` / `superseded_by` / `restore` / `audit_stale` /
  `import` 都追加一行 NDJSON：`{at, op, card_id, by, before, after, note}`。
- **`before` / `after` 是两个内容哈希**（规范化 JSON 的 sha256 前 8 位）——只记一个等于没记；
  为此在改字段**之前**复制快照（第一版踩过：改完才拍，前后哈希相同）。
- `by` 取 `AGENT_NAME`（多 agent 协作时由调用方注入）→ `USERNAME` → `unknown`，
  实测设 `AGENT_NAME=test-agent` 后审计里就是 `test-agent`。
- `cards log [<卡id>]`：新 → 旧显示，带前后哈希；按卡过滤。

### 四条硬规矩

1. **fail-soft**：审计写失败（目录不可写等）**绝不影响写卡** —— 审计是副业、写卡才是业务，
   有测试用"父级被文件占位"造不可写来钉这条；
2. 记**改前/改后**两个哈希；
3. 坏行跳过（NDJSON 手改/截断是常态）；
4. **文件有界**：超过 2 MB 时滚动保留最后 5000 行（append-only 也不能无限长）。

### 实测

```
cards add …            → cards log: add  xmtx  c-983cee71  [— → 9290aeb8]
cards supersede …      → supersede     [旧前 → 旧后]  note="被 … 推翻"
                         superseded_by [新前 → 新后]  note="推翻了 …"
audit --apply          → audit_stale   note="前提已失效：…（dead_no_successor）"
update（改证据）        → update        before=7ed46a29  after=f5e32acb
```

### 测试
- 新增 `tests/test_cards_audit_log.py` 9 条：新增记录含哈希与 by / 更新前后哈希不同 /
  推翻记两张卡 / 巡检写审计 / 按卡过滤且新→旧 / 缺文件返回空 / 坏行跳过 /
  **审计失败不影响写卡** / 滚动只留最后 N 行。全量 **231 passed**。

### 说明
- 审计只记"变了什么"，不记完整内容（哈希 + note）；要还原内容请用 `cards backup/restore`。
- 手动删卡不会删审计记录 —— 那正是审计的意义（历史不该被后来的清理抹掉）。

## [1.5.16] — 2026-11-10

> 改进清单第 4 条。1.5.15 未发布，本版含其全部内容。

### 新增：`cards audit` —— 前提巡检（让"过期"自己浮出来）

**问题**：卡片的 `status` 一直靠**人**改。而"结论里提到的路径现在还在不在"完全可以机器判 ——
`premises.py` 早就有三路判定（活 / 项目已迁移 / 真失效）。市场里 `stratagate-dsh` 把
"旧的会褪色成摘要"当卖点、`dsh-memoir` 讲 lifecycle：**记忆需要生命周期**是共识。

- `cards audit`：从每张 `alive`/`unverified` 卡的 `claim/scope/evidence.quote` 抽路径，
  过 `is_project_asset` 过滤，再交给 `premises.judge_path` 三路判定；命中失效前提的卡列出来。
- `cards audit --apply`：把命中卡置 `stale`，写 `stale_reason` 与 `status_history`
  （`{at, from, to, reason}`），**可人工回滚**。
- 判据由调用方注入（`audit_cards(analyze, ...)`）—— **cards 层不依赖 premises**，
  将来换判据源（版本号是否还匹配、外部链接是否 404）可复用同一函数。

### 四条安全边界（这是本功能唯一会自动改状态的地方，所以写死）

1. **默认只报告**，`--apply` 才写盘；
2. 命中失效前提**只置 `stale`**，不置 `dead`、**不删除** —— 路径没了 ≠ 结论错了；
3. 改状态**必须留痕**（`status_history`），否则无从回滚；
4. `dead` 卡不参与巡检（已被推翻的东西不需要再巡检）。

### 实测

造一张 `claim` 引用 `F:\demo-ghost\missing.yaml` 的卡：

```
cards audit            → 前提失效 1 张（仅报告，未改动），状态仍 alive
cards audit --apply    → 已置 stale；stale_reason="前提已失效：…（dead_no_successor）"
                         status_history=[{from:"alive", to:"stale", reason:"…", at:"2026-11-10T13:10:36"}]
```

现有 2 张真实卡：扫描 1 张（dead 那张跳过）、无失效前提 —— 不误报。

### 测试
- 新增 `tests/test_cards_audit.py` 8 条：dry-run 不写盘 / apply 置 stale 且留痕 /
  不判死且不删卡 / dead 不参与扫描 / 无失效前提不改 / 无可验证前提单独计数 /
  二次 apply 不重复追加历史 / 坏库不崩。全量 **222 passed**。

### 说明
- 只做"路径是否还在"这一种前提；版本号/外部链接等前提需要各自判据，留待后续。
- 巡检**不会**把卡恢复成 alive（路径又出现时不做反向操作）——避免自动来回抖动。

## [1.5.15] — 2026-11-10

> 改进清单第 3 条。1.5.14 未发布，本版含其全部内容。

### 新增：`inject --cards` —— 有界的结论注入

**为什么必须有界**：注入是**每轮成本**。卡片越攒越多，无脑全塞会挤爆上下文，
而且噪声会盖掉信号。市场里做注入的插件（`dsh-memoir`「bounded Hot Memory」、`dsh-memento`「Bounded」）
都把"有界"写进定位 —— 这不是优化，是正确性。

- `inject --cards [--topic X] [--max-cards N] [--card-chars M] [--include-dead]`
- 可与实体同时给：**卡片块在前、环境事实在后**，写进同一个 `context.md`；
  只给 `--cards` 时只输出卡片块（不再强制要求 entity）。
- 每行格式：`- [成立] 断言（machine；证据 N 条）` —— 带作用域与证据数，让人能判断可信度。

### 优先序（都可在输出里核对）

1. `status`：`alive` > `unverified` > `stale` >（默认排除）`dead`
2. 同状态内**证据条数多的先上**（可核验性高的优先）
3. 再按 `updated_at` 新的优先

### 两种预算都要报，且要说是哪条卡住的

- `--max-cards`（默认 5，条数）、`--card-chars`（默认 900，字符）**同时生效**。
- 截断时输出：`另有 N 条未列出（候选 M 条；卡住的是**条数上限 N 条**）：调大 --max-cards …`
  —— 第一版这里只说"调大 --max-cards"，但字符预算卡住时那是**误导**（用户会去调错参数），
  所以加了 `bound_by` 区分，实测两种提示都准确。

### 诚实边界

**没有按"最近验证时间"排序** —— 因为 `cards validate --verify` 目前只报告、不回写 `verified_at`。
不假装有这个字段（改进清单里列为后续项）。

### 实测

```
inject --cards                                   → 1 条（另 1 条 dead 默认排除）
inject --cards --include-dead                    → 2 条，dead 标 [已被推翻]
inject --cards --topic 预算测试 --max-cards 1     → 1 条 + "另有 1 条未列出（条数上限 1 条）"
inject --cards --topic 预算测试 --card-chars 20   → 0 条 + "另有 2 条未列出（字符上限 20 字符）"
```

### 测试
- 新增 `tests/test_inject_cards.py` 9 条：优先序（alive 先于 unverified 先于 stale）、
  dead 默认排除/按需包含、同状态按证据数排序、条数预算截断与提示、字符预算截断与提示、
  全放得下时无截断、topic 过滤、scope 前缀过滤、空库不崩。全量 **214 passed**。

## [1.5.14] — 2026-11-10

> 改进清单第 2 条（`docs/design/2026-11-10_agent-memory改进清单_借鉴市场机制.md`）。
> 1.5.13 未发布，本版含其全部内容。

### 新增：结构化作用域 `scopes`

**改的理由**：原来的 `scope` 是**自由文本**（"agent-memory >=1.5.3；seed 与 last_search_via"），
机器无法据此过滤 —— "按当前 cwd 注入相关结论""只导出一个项目的卡"都做不了。
借鉴 `dsh-engram` 的 dual-scope（用户级 + 工作区级）、`yolk_vat-y/dsh-project-memory` 的 per-workspace。

- 卡片新增 `scopes: ["machine", "project:F:/demo-base"]`；旧的 `scope` 文本字段**保留**作人类说明。
- `cards add --scopes "machine,project:F:\demo-base"`；`cards list/search --scope-filter <scope>`。
- **路径归一化**（反斜杠→斜杠、去尾斜杠、小写）——否则同一个项目会变成两个作用域。
- **项目作用域按路径前缀匹配**：`project:F:/demo` 覆盖 `project:F:/demo/docs`，反向也成立
  （子项目要能查到父项目的结论）。
- **旧卡零迁移**：没有 `scopes` 字段的历史卡自动视为 `machine`。
- 校验的三条边界（都是"静默出错"的高发区）：
  1. 写了非法值（`everywhere`）⇒ **报错，不静默丢弃**（丢弃会让卡在最常见的过滤里消失）；
  2. `project:` 缺路径 ⇒ 报错；
  3. **显式空列表 `scopes: []` ⇒ 报错**（≠ 缺省）：缺省是 machine，空列表意味着
     "任何 `--scope-filter` 都查不到"，那是静默消失。
- `build()` 不传 scopes ⇒ 默认 `["machine"]`（"这台机器通用"是绝大多数结论的情况）。

### 实测

造两张卡（machine / project:F:\demo-base）：
```
list --scope-filter machine                    → 只出「全机通用」1 张
list --scope-filter "project:F:\demo-base\docs" → 只出「仅项目A」1 张（前缀匹配生效）
```
旧的两张真实卡渲染为 `scopes=machine`，内容与行为不变。

### 测试
- 新增 `tests/test_cards_scopes.py` 17 条：字符串/列表解析、路径归一化、去重、
  非法值保留给校验、旧卡兼容、build 缺省 machine、四类非法值、双向前缀匹配、
  search 过滤、render 显示。全量 **205 passed**。

### 说明
- 这一步是第 3/4/9 条（注入预算、前提巡检、实时注入）的**前置**：没有结构化作用域，
  "按当前 cwd 注入"就无从筛起。
- 现有卡都不需要迁移。

## [1.5.13] — 2026-11-10

> 1.5.12 未发布，本版含其全部内容。

### 新增：`cards validate --verify` —— 证据回到原文核对

**修的是一个一直没兑现的承诺**：`validate()` 只保证 quote「非空、不超过 200 字」，
**从不检查它是否真出自原文** —— 于是卡片可以被随手编一段话录进来，而「可溯源」是这套卡片
存在的理由。

- `cards.py` 新增 `verify_evidence(fetch_text, card)` / `verify_cards(fetch_text)`：
  对每条带 `msg` 锚点的证据，取 `messages.text` 全文，检查 quote 是否为原文子串。
  `fetch_text` 由调用方注入（CLI 传 `store.message_by_id` 的包装），**cards 层不依赖 store**。
- 比对前**归一化**（去所有空白 + 忽略大小写）：原文的换行/缩进与摘录里的一行是同一句话，
  不让排版差异造成假失败。
- **允许省略但每段必须命中**：quote 可用 `…`/`...`/`⋯` 跳过大段原文，但按省略号切出的
  **每一段**（≥4 字）都必须在原文里找到 —— 防止「抄一半编一半」。
- **三种结果分开统计，不混为一谈**（这是本功能最容易被做坏的地方）：
  `verified` / `mismatch`（有锚点但原文里没有 ⇒ **真问题，退出码 1**）/
  `missing_msg`（锚点在但本机库里没这条消息 ⇒ 跨机或库被重建，如实计数）/
  `no_anchor`（没有 `msg`，跨机卡片本来就无法回查 ⇒ **不计入分母**，不当作失败）。
- 输出「**可验证率** = verified /(verified+mismatch+missing_msg)」，分母定义写在输出里。

### 实测

造两张卡引同一条真实消息（`msg=45049`）：一张 quote 正确、一张把「禁止」改成「允许」。

```
校验 4 张 ｜ 非法 0 张
=== 证据核验（回到原文）===
  卡片 4 张 ｜ 证据 4 条：可验证 1 ／ 不符 1 ／ 消息已不在 0 ／ 无锚点 2
  **可验证率 50%**
  x c-6ad1523f 验证测试：篡改引用
      evidence[1] 原文里找不到：'允许派 subagent、允许全盘 Glob'
退出码 1
```

清理测试卡后回到 2 张真实卡（都没有锚点 ⇒ 可验证率的分母为 0，如实显示为 0%）。

### 测试
- 新增 `tests/test_cards_evidence.py` 9 条：正确引用通过 / **篡改被抓** / 空白与大小写归一 /
  省略号分段（每段须命中）/ 无锚点不计失败 / 消息已不在单独计数 / 过短摘录不构成证据 /
  整库聚合与坏卡清单 / 坏数据不崩。全量 **188 passed**。

### 说明
- 这是 DSH 市场调研（`docs/design/2026-11-10_agent-memory改进清单_借鉴市场机制.md`）的第 1 条，
  借鉴 `dsh-memento`「auditable」与 `dsh-engram`「evidence」的共识：**可信度来自证据可回查**。
- 现有 2 张真实卡都没有 `msg` 锚点（是加锚点功能之前写的）。**建议补**：重跑 `cards add` 带上
  锚点，或接受它们停留在「不可验证」状态 —— 工具会如实显示，不会假装通过。

## [1.5.12] — 2026-11-09

> 1.5.11 未发布（同日继续做"注入块保鲜"），本版含其全部内容。

### 背景：日更任务从没被注册过

`scripts/refresh.ps1` 里**一直都有** inject 步骤，但没有任何东西调度它。实测：
`index/daily-refresh.log` 最后一条是 **2026-11-03 20:22**（手工跑过两次就断了），
某个接入项目（targets.json 里配置的那份）的 `.agent-memory/context.md` 的 `check_at` 停在 11-03 —— **5.9 天没人发现**。
而 agent 会照着里面"旧目录已改名/已确认失效"这类事实工作：**过期事实比没有事实更危险**
（本仓实测：沿旧路径要 16 次工具调用，读它对只要 2 次）。

### 新增：注入块过期自报

- 注入块文件头改为 `check_at=… stale_after_days=3`，正文顶部加一句明确指引：
  「若今天距 check_at 超过 3 天，说明**日更任务没在跑** —— 先重跑 refresh，不要直接按下面的事实工作」。
- `inject --check`：逐个目标报 `OK` / `STALE`（含 age、check_at、阈值），**有 STALE 退出码 1**，
  供日更前置检查、CI、以及 agent 开工前自检使用。`--write` 也可指向单个文件。
- `doctor` 增一项「注入块」：过期/未生成 → **WARN**（不阻断），新鲜 → OK。
- 阈值取 **3 天**而不是 7：这是**每日**任务，正常 age <1 天；连续 3 天没更新就等于断了
  （留周末余量）。判据用文件头 `check_at` 而不是 mtime —— 文件会被复制/git 检出，mtime 会骗人。

### 新增：`scripts/register-refresh-task.ps1`（注册日更任务）

- 注册当前用户的计划任务 `agent-memory-daily`（每日 06:30，`/IT` 仅登录时运行：
  ingest 需要读用户自己的 agent 会话文件）。
- `-Status` 查状态、`-Remove` 删除、重复执行安全（`/F` 覆盖）。
- 纯 ASCII：PS 5.1 会把无 BOM 的 .ps1 按 GBK 解码，中文会破坏解析。

### `refresh.ps1` 增前置检查

跑之前先 `inject --check`：若已有注入块已过期，日志里记一行
`WARN: injection block stale before this run -> …` —— 这样"任务断了"在日志尾部就能看见，
不用等人去算日期。

### 测试
- 新增 `tests/test_inject_staleness.py` 8 条：新鲜/过期/阈值边界/文件缺失/无 check_at/
  check_at 优先于 mtime/默认阈值/头部写明阈值。全量 **179 passed**。

## [1.5.11] — 2026-11-09

> 1.5.10 未发布（同一天继续查会话层召回），本版含 1.5.10 全部内容。

### 修复：中文多词查询在会话层**两条路都查不到**（实测：`超时 回落` 0 → 17 条）

`search` 的两条召回路径对中文多词都不成立：

1. FTS5（`unicode61`）把**整段连续 CJK 当一个 token** ⇒ `超时` 只在"该 token 恰好是超时"时命中；
2. `LIKE` 回退用的是**整句** `%超时 回落%` ⇒ 要求两个词**挨着**。

于是"两个词在同一条消息里、但中间隔着别的字"这种情况，**两条路都是 0 命中**。
实测 12 个双词查询：消息级 AND 合计仅 82 条，其中 **10 个查询 0 命中**；而按词交集：
`超时 回落` 0 → **17**、`证据 摘录` 0 → **17**、`复核 报告` 7 → 440、`预算 字符` 9 → 83。

- 改法：FTS 命中不足时，按**词逐个 LIKE 取 id 集合求交**（等价于精确 AND，**不放宽相关性**），
  再取回完整行；输出 `索引=like-and`，让用户知道走的是哪条路。成本约 160 ms/词，只在不足时付。
- `复核 报告` 这类 FTS 本来就命中的查询**行为不变**（7 条，不回归）。

### 新增（显式开关）：`--cross-message` 邻近补检

- 两词分属**不同消息**时，默认返回 0 并给出可执行提示：
  「这些词没有在同一条消息里共现；若要按『同一会话相邻消息』再找一次，加 --cross-message」。
- `--cross-message` 才启用：同会话**相邻 ≤3 条消息**内共现即算命中，逐条标 `terms_matched`
  与窗口跨度；无命中时也明说「跨消息补检也无命中」。
- **为什么默认关闭**：先试过"整会话共现"，实测假阳性严重（长会话里两个常用词各自出现过就算相关，
  `超时 回落` 召回卖库存货的会话）。收窄到相邻 3 条后仍只作可选——精确 AND 返回 0 是**诚实结果**，
  把弱相关命中混进默认结果才是误导。

### 顺带修的两个真实缺陷（都是测试/实测抓出来的，不是事后想到）

1. `_search_like` 的返回列里**没有 `seq`**，而邻近判断全靠 `seq` 距离 ⇒ 窗口形同虚设
   （测试 `test_cross_message_respects_window` 抓住）。改成本方法自取 `(id, session_key, seq, ts)`。
2. 默认路径会同时打印"多词不共现"与"请加 --trigram"两条提示，互相打架 ⇒ 前者已给时不再叠加后者。

### 测试
- 新增 4 条：中文多词 AND 走 like-and（且 `cross_message=False`）、默认不掺结果但给提示、
  `--cross-message` 命中窗口、相隔超窗不命中。全量 **171 passed**。

## [1.5.10] — 2026-11-09

> 1.5.9 未发布（打包后当天又发现下面这个问题），本版含 1.5.9 全部内容。

### 修复：docs 层多词查询大面积漏召回（实测 74%）

`search_ex` 原来要求**某一行同时含全部查询词**；文件级 AND 通过后，若凑不齐这样一行，
就把**整篇文档丢掉**。实测（305 篇 docs、25 个真实多词查询）：

| 匹配单位 | 通过篇次 | 相对行级 |
|---|---|---|
| 文件级 AND（所有词在文件里出现） | 509 | — |
| **行级**（改前实际采用） | **130** | 基准 |
| **段落级**（改后采用，空行分隔） | **225** | **+95（+73%）** |
| 连续 3 / 5 / 8 行窗口 | 190 / 224 / 256 | +60 / +94 / +126 |

典型损失：`docs 根` 丢 76%、`超时 回落` 丢 89%、`文档 预算` 丢 89%、`卡片 快照` 丢 86%。
原因很朴素：**同一段里分两句说了两件事**，其实是同一个话题，却被"必须同一行"判死。

- 改法：改为**段落级 AND**（空行分隔的连续非空行），段内取最匹配的一行做摘录与行号；
  排序仍按「标题命中优先 → 段内词频」。
- **不做**固定窗口：k=3/5/8 的收益要靠人挑一个没有依据的 k，段落是自然语义单位，不需要调参。
- 精度边界保留：**跨段不算命中**（`## 一` 讲 docs、`## 二` 讲预算 ⇒ 不命中），有测试钉住。

### 顺带

- 把 `F:\agent-memory\docs`（本项目自己的 research/reviews/design）加进本机 `index/docs-roots.json`
  —— 之前 `--docs-first` **搜不到自己的复核报告**。现 4 个根 / 305 篇 md（本机数据，不入包）。

### 测试
- 新增 3 条：段内跨行命中、跨段不命中（精度）、段落命中的行号与标题归因。全量 **167 passed**。

## [1.5.9] — 2026-11-09

> 四件事都来自对 TencentDB Agent Memory 的调研（`docs/research/2026-11-09_腾讯云TencentDB-Agent-Memory调研.md`）。
> 借的是它的**工程卫生**（生产事故沉淀出的防护），不是它的架构——四层金字塔/LLM 自动抽取不适合我们。

### 新增：证据锚点下钻 `cards open`（对标它的 `node_id` 回原文）
- 卡片证据可选带 `msg`（= 本机 `messages.id`）；`cards add --ev "agent|date|quote|session|msg_id"`。
- `cards open <卡id>`：打出卡片摘要 + **每条带锚点证据的原文全文**（agent/role/时间/会话/标题）。
  `search` 只回 400 字、卡片 quote 只有 200 字，所以这一层是"从结论回到证据"的确定性路径。
- **跨机不伪造**：库里找不到就明说「本机库里没有这条消息（跨机或库被重建过）」。
- `store.message_by_id()` 按 id 取全文，找不到返回 `None`（有测试钉住）。

### 新增：写前快照 + `cards restore`（对标它的 BackupManager / fail-soft）
- 每次 `save()` 前把现有文件复制成 `cards.jsonl.<时间戳>.bak`，**只留最近 5 份**。
  原子写只能防"写一半断电"，防不了**逻辑损坏**（错误的 merge、手滑的 replace）。
- `cards backup` 列快照；`cards restore [<快照>] --yes` 恢复（不带 `--yes` 拒绝，防止误操作）。
- **恢复本身可反悔**：写回前当前文件也会留一份快照。
- 修一个自己造的坑：`load()` 是 fail-soft（读不了 ⇒ 空表 + warning），所以 `restore` 必须区分
  "真空表"与"根本读不了"——否则一份坏快照能把卡表**静默清空**。现在坏快照直接拒绝且不动当前文件。

### 新增：`cards export --md`（对标它"顶层存 Markdown 保白盒"）
- 按 topic 分组成人类可读 Markdown（状态标记、适用范围、证据、被谁推翻），
  落到 `docs/` 之后**能被 `search --docs-first` 直接检索**——卡片层与 docs 层就此打通。

### 新增：docs 层召回预算与超时（对标它的 `maxTotalRecallChars` / `timeoutMs=5000`）
- `search --docs-first --max-chars N`：**总字符预算**（0 = 不限，默认不变），命中按分数取到预算为止。
- `search --docs-first --timeout-ms N`：扫描超时就停，**如实打印"docs 层超时（N ms），扫了 M 个文件后
  停下并回落会话"**，不许静默变成"docs 里没有"；返回已找到的部分。
- `docs_index.search_ex()` 返回 `(hits, source, meta)`，`meta = {timed_out, scanned, elapsed_ms}`；
  旧签名 `search()` 保持兼容（有测试钉住）。

### 测试
- 新增 15 条：msg 锚点校验与渲染、快照轮转与上限、恢复（含坏快照拒绝、无快照失败）、
  Markdown 导出的分组/状态/推翻标注、预算截断与 0=不限、超时部分返回与全扫、`message_by_id` 命中与缺失。
  全量 **164 passed**。

## [1.5.8] — 2026-11-08

> 1.5.7 已通过第三轮独立复核（GLM，判「可发」，报告 `docs/reviews/v1.5.7_glm_review.md`），
> 但它同时指出一个"低概率、可达"的读侧缺口。本版只修这一处，其余内容与 1.5.7 完全一致。

### 修复：cards 读侧遇到误编码会整层崩（复核发现）
- 现象：`cards.load()` 原来只接 `OSError`；`io.open(..., encoding="utf-8")` 遇到 **GBK/UTF-16**
  文件抛的是 `UnicodeDecodeError`，**不继承 OSError** ⇒ `cards list/search/stats/validate/import/export`
  **全部 traceback**。而模块自己的规矩 3 写的是"读不了就当空表"。
- 可达路径：`index/cards.jsonl` 被人用记事本（ANSI）或 PowerShell 5.1 的 `>` / `Out-File`
  （默认非 UTF-8）编辑、合并过一次 —— 之后任一 cards 子命令即崩。
- 修法：`except (OSError, UnicodeDecodeError)` ⇒ 空表 + warning；读侧同时改 `utf-8-sig`。
- 附带：**带 BOM** 的文件原来不崩，但首行会被判成坏 JSON（`Unexpected UTF-8 BOM`），
  **静默丢一张卡并只留一条 warning**；`utf-8-sig` 后完整读回。

### 测试
- `tests/test_cards.py` 增 2 条：GBK 文件降级为空表且带 `UnicodeDecodeError` 警告、
  BOM 文件完整读回且无警告。全量 149 passed。

## [1.5.7] — 2026-11-08

### 新增：docs 层（编译产物优先，`search --docs-first`）
`search` 查的是**原始消息**；一条结论往往横跨几十条消息，而编译过的 `docs/*.md` 里它通常是一句话
加日期和数字。2026-11-06 同一批问题的实测对比（两套检索、同一晚）：

| 问「是否推翻过不要向量切块的 RAG」 | 结果 |
|---|---|
| `search 向量切块`（会话层） | **0 命中** |
| `search 切块`（会话层） | 5 条，**5/5 是 checkpoint 拼接文本** |
| docs 检索 | 一次命中「不回到纯向量 + chunk 的旧 RAG」+ 日期 + 数字 |

于是把顺序改成 **docs（编译层）→ sessions（原始层）**，与本仓 `L1_SKIP_REDUNDANT_NAV`
的"升级式检索"同一个思想。

- `search --docs-first`：先 grep docs 的标题与正文，命中输出 `文件:行号` + 小节 + 摘录；
  命中后默认不再查会话层（要看加 `--with-sessions`）；无命中会明说「回落到会话检索」。
- `docs`（=`docs stats`）：看这台机器接了哪些根、多少 md、最近更新；**没配置时明确说"这层关闭"**，
  并打印接法。
- **配置驱动、缺失即关**：根写在 `index/docs-roots.json`（本机数据，不入库），
  回退到环境变量 `DOCS_MCP_ROOTS`（逗号分隔），都没有 ⇒ 静默跳过这层（不是错误）。
- 匹配规则：按空白切词、**所有词须同一文件出现**（AND），单字忽略；中文不分词按子串匹配
  （`切块`、`向量` 这类 2 字词正是 FTS trigram 的盲区）；标题命中的排在正文命中之前。
- 零 LLM、零向量；单文件上限 2 MB、最多扫 3000 个 md（配错根也不至于扫全盘）。

### 修复
- `docs-roots.json` 用 `utf-8-sig` 读：Windows 上 PowerShell/记事本写出的 JSON 常带 BOM，
  用 `utf-8` 读会抛 "Unexpected UTF-8 BOM" ⇒ **整层被静默关掉**（实测踩到，输出仍是"未接入"，
  很容易误判成"没配"）。

### 测试
- 新增 `tests/test_docs_index.py` 14 条：配置读取（含 BOM）、环境变量回退、坏配置降级、
  AND 语义、中文子串、子目录递归、标题优先排序、limit、统计、关闭态。全量 147 passed。

## [1.5.6] — 2026-11-08

> **1.5.4 / 1.5.5 均为内部构建，作废、从未发布**：
> 1.5.4 漏了 `cards.py` 进打包白名单（装上一用就 ImportError）；
> 1.5.5 补上了，但 changelog 是本条目上一稿，被 PowerShell 转义写坏（`$engFiles` 变成 `\$`、
> 反引号+字母变成 TAB/BEL、反引号+汉字被吞成单字符）。两处都是"包内容与预期不符"，
> 所以本版重新生成，并给这两类错误各加一道机器闸门。

### 新增：结论卡（编译层）
把「会话检索」补上一层**编译产物**：`search` 只能返回消息片段，一条决策往往横跨几十条消息，
理解成本全压在查询期；`docs/` 里的结论文档证明编译过的东西好查得多，但它属于**某个项目**。
卡片补的就是这一层，且**独立于任何项目目录**。

- `index/cards.jsonl`（JSONL，一行一张卡；`index/` 本来就不进库）：字段
  `claim / topic / status / scope / evidence / supersedes / superseded_by / created_at`。
- **状态四态**：`alive` / `stale` / `dead` / `unverified`（也认中文：仍成立 / 前提已变 /
  已被推翻 / 未验证）。**被推翻的卡不删**，用 `supersede` 连成链——"过去这么认为、现在不成立"
  本身就是最有价值的经验。
- **证据三要素强制**：`agent` + `date` + `quote`（摘录不超过 200 字）。`session` 可选且
  **只在本机有效**，所以摘录是跨机可迁移的证据下限；没有摘录的卡直接判非法，不许入库。
- 命令：`cards add | list | search | supersede | export | import | validate | stats`；
  `export/import` 就是复制 JSONL，`import --mode merge` 按 id 去重、取 `updated_at` 较新的一份，
  `replace` 需 `--yes` 确认。
- 读侧韧性：坏行/非对象行**跳过并报 warning**，文件缺失当空表 —— 一张卡的语法错误不许让检索变砖。

### 新增：两道打包闸门（都是被今天这两类错误逼出来的）
- `tools/check_pack_manifest.py`：扫描 stage 内每个 `.py` 的本地 `import`，**缺模块即 PACK FAIL**。
  同一个错误两天内犯了两次（11-07 漏 `aliases.py`、11-08 漏 `cards.py`），手写白名单靠人记必漏。
- `tools/check_pack_text.py`：扫描 stage 内 `.md/.txt/.ps1/.py` 的 **BEL/BS/NUL/FF 与 .md 里的 TAB**，
  命中即 PACK FAIL —— 这次 changelog 被写坏就是它该拦住的。
- 两个闸门都由 `pack_platform.ps1` 调用，失败时**先打印清单再中止**（原来 `$ErrorActionPreference='Stop'`
  会把校验器的 stderr 当终止错误，导致"死了但不告诉你哪里错"）。

### 修复
- `build()` 的 status 原来把不认识的取值**悄悄退化成 alive**；现在原样保留，由 `validate()` 报错
  —— 拼错的 `ded` 不该被当成"仍成立"。

### 说明
- 不做自动抽结论：规则抽取的产物在 `timeline` 里已经见过，噪音高；写卡由当时的 agent 负责。
- 不做向量：与主项目路线一致。
- 测试：新增 `tests/test_cards.py` 25 条（校验 / 推翻链 / 幂等导入 / 坏行韧性 / 迁移往返）。

## [1.5.3] — 2026-11-07

> 1.5.2 **从未发布**：独立复核（GLM，非本项目同源模型）判「不可发」，两条缺陷带可达路径。
> 本版把 1.5.2 的别名能力与下面三处修复一起发；1.5.2 无 release note。

### 修复
- **别名补检污染「如实现报」标志**（复核发现 1）：补检内部会再走一次检索，而那条路径会写
  `last_query_relaxed` / `last_search_via`。后果是**用户一句纯中文查询也会被告知「查询含 FTS5
  语法字符」或「已回退 LIKE 全表扫描」——都是假话**。现在补检前后快照并恢复这两个标志，
  补检自身只经由 `last_alias_note` 汇报。
- **`last_alias_error` 不随查询重置**（复核发现 1 附带）：复用同一个 Store 连续检索时，
  上一轮的补检错误会串进下一轮的输出。现在每次 `search()` 开头清零。
- **随包 changelog 有 8 行被控制字符损坏**（复核发现 2）：「topics / timeline / tests / aliases.py」
  等词的首字母被 `\t` / `\a` 吞掉（`references/changelog.md` 随包分发，用户看的就是这份）。
  已修复。根因：用 PowerShell 双引号 here-string 写含反引号的内容（`` `t `` 是 TAB、`` `a `` 是 BEL）；
  本仓纪律本来就是「带反引号/中文的内容一律用 Python 写文件」，这次是违规踩到。

### 新增
- `aliases.duplicates()` + `doctor` 新增一行「实体别名表」自检（复核发现 3）：同一写法被挂到
  多个 canonical 下时，反查表 `setdefault` **先到先得且全程无提示**，会确定性地解析到错的实体。
  现在只报 **WARN**（不阻断检索），并列出前 3 条重复写法。

### 记录（不改代码）
- mtime 缓存在同一时钟 tick 内察觉不到文件变化：CLI 每次新建进程、单次查询内文件不会变，
  **无经发布包的触发路径**（复核发现 4，记为已知限制）。
- 2 字别名不参与自由文本补检：这是 1.5.2 明示的防误撞边界，显式实体参数不受影响（复核发现 5）。

### 测试
- 新增 3 条回归：别名补检后 `last_query_relaxed` / `last_search_via` 保持主查询的值；
  `last_alias_error` 每次查询清零；`duplicates()` 能检出重复写法。

## [1.5.2] — 2026-11-06

### 修复
- **查询侧不认实体旧名**：`index/entities.json` 的别名表此前只被 `topics` 读，`timeline` /
  `search` 拿字面串直接比 `session_topics.label`，于是项目改名后「数据一条没丢、查询侧查不到」。
  实测（本机索引库）：按 canonical 名查能得到数十个会话与多条结论，换个旧名查是 **0 / 0**。
  现在两处都先过 `aliases.py`：`timeline` 把输入展开成 canonical + 全部别名再查（`label IN (…)`）；
  `search` 在**主查询命中不足 limit** 时按别名补检一轮（按 `m.id` 去重，**不改写主查询**）。
- 新增 `aliases.py`（单一事实源）：只读 JSON、按 mtime 缓存、**文件缺失或损坏静默退化为空表**
  （新机器还没跑过 `init` 时检索照常工作）。自由文本只认 ≥3 字的写法，2 字别名仅用于显式实体参数。
- 两处输出都**如实回报**，不静默改写用户查询：`timeline` 多一行「别名解析：A → B（共 N 种写法）」，
  `search` 多一行「↪ 别名补检：`X` +N 条」。

### 测试
- 新增 `tests/test_alias_resolution.py`：别名语义 / 缺失与损坏退化 / timeline 别名与 canonical
  结果一致 / search 补检的触发与不触发 / 别名层坏掉不影响检索。

### 边界
- `search` 的补检**只在命中不足 limit 时触发**：大库上常见词一次就打满 limit，此时不补，
  也不许诺「改写结果集」。真正的收益在 `timeline`（显式实体）与稀有词。

> ⚠️ **1.5.2 从未发布。** 独立复核（GLM）判定不可发，两条缺陷见 1.5.3；该版本号仅存在于
> 本地 `dist/` 的一次内部构建。

## [1.5.1] — 2026-11-05

### 修复
- **包内不再含真实项目/客户名与内部路径**：实测 v1.4.0/v1.5.0 的上传包 14/28 个文件命中
  （真实客户名 5 处、自有项目名 66 处、内部绝对路径 12 处）。本次把注释、docstring、示例、
  帮助文本里的真名统一换成通用示例（`demo-kb` / `demo-base` / `示例客户项目(DEMO2021)` /
  `F:\demo-main` 等）。**代码语义未变**，95 条测试全绿。
- 打包新增**私有词机检**：词表 `.private-terms.txt`（gitignore，不进包、不进公开仓）+
  `tools/check_private_terms.py`；命中任一即 `PACK FAIL` 并逐条打印 `文件: 词`。

## [1.5.0] — 2026-11-04

### 新增
- `am.ps1 doctor` / `cli.py doctor`：环境自检（工程目录、Python、依赖、索引库、数据 schema 版本、
  会话/消息数、peer 覆盖、最近入库、技能版本），退出码 0/1 —— 装完没跑通时先跑它。
- 索引库 `PRAGMA user_version`（当前 **1**）：包版本 ≠ 数据版本；库比代码新时**直接拒绝**运行，
  防止旧代码读新结构给出错答案。
- `references/changelog.md`（本文件）：随包分发，用户能自己看"这版改了什么"。

### 借鉴来源
Helm 的 `version` 与 `appVersion` 分离、`openviking-server doctor`、npm 的 dist-tag/`npm doctor` 思路。

## [1.4.1] — 2026-11-04

### 修复
- **未安装时的报错不可操作**：原来只抛 `agent-memory home not found. Set AGENT_MEMORY_HOME ...`。
  现在退出码 **3**，并直接给三种修法（设 `AGENT_MEMORY_HOME` / 工程 `install.ps1` / 包内 `bootstrap.ps1`）。
- `python not found` 同样给修法，而不是只说结论。
- 打包新增闸门：**版本号未变但内容变了 → 拒绝打包**（1.4.0 曾对应两份不同内容）。

## [1.4.0] — 2026-11-04

### 新增
- 采集来源 4 → **6**：WorkBuddy（CN/AI）与 CodeBuddy（`collectors/workbuddy.py`、`codebuddy.py`）。
  处理三个坑：`<system-reminder>` 包裹（单行最大 87 KB）、`<user_query>` 提取、cwd 只挂在非 message 事件。

### 修复
- `search "agent-memory"` 因 `-` 是 FTS5 运算符直接报错 → 现在自动包成短语重试（`C++` 仍按语法错误报出）。
- 同 key 会话碰撞时比对 `message_count` 并打印警告（CN/AI 实测 22 个同名 session）。

## [1.3.0] — 2026-11-04

### 新增
- `peers` 命令 + `search --peer`：工作区身份按 **git origin** 派生（无 origin 回退 `path-<名>-<hash8>`），
  主仓与 linked worktree 归同一 peer；`ingest` 自动补算，`peers --backfill` 补存量。

## [1.2.0] — 2026-11-04

### 修复
- 1~2 字中文查不到（trigram 只索引 3 字滑窗）→ 自动 **LIKE 回退**，输出标 `索引=like`。
- FTS5 语法错误被静默吞成"零命中" → 抛错并 exit 2（`C++` 等）。
- A/B 数字加限定：单任务、n=1/n=3，**token 未减少**（9B 实验 +14%），省的是步数与错误率。

## [1.1.1] — 2026-11-04

### 修复
- 平台包按「两级目录」规则重排（引擎放到 zip 根，去掉 `scripts\engine\` 嵌套）。
- `bootstrap.ps1` 改为真实源文件并加解释器探测（uv → py → python → 常见目录）；
  `SKILL.md` 不再让人去跑包里不存在的 `install.ps1`。
- `inject` 不再把仍存在的目录报成「已确认失效（勿照做）」。

## [1.1.0] — 2026-11-03

### 新增
- 话题归并（实体 × 意图）、演变时间线、前提校验（premises）、环境事实注入块（inject）、
  DSH 技能包装（`SKILL.md` + `references/` + `templates/`）。

## [1.0.0] — 2026-11-02

### 新增
- 四个来源（DSH / Codex / Claude Code / Hermes）会话采集 + SQLite FTS5 双索引（unicode61 + trigram）
  + 检索（毫秒级、零模型）。
