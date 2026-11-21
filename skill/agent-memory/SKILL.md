---
name: agent-memory
slug: agent-memory
display_name: 智能体会话记忆
display_name_en: Agent Session Memory
description: >-
  检索本机多个编码 agent（DSH / Codex / Claude Code / Hermes / WorkBuddy / CodeBuddy）的历史会话结论，
  判断旧结论是否已失效，并生成「环境事实」注入块。触发词：上次怎么决定的、
  之前讨论过什么、这个项目不同 agent 说过什么、旧路径还有效吗、为什么改成这样、
  agent-memory。
description_zh: 把一台机器上多个 agent 的会话汇成可检索库，查历史结论、看话题演变、验前提是否失效。
description_en: Unify session history across coding agents on one machine; search past conclusions, trace topic evolution, and verify whether old premises are still valid.
allowed-tools: Bash, Read, Grep, Glob
version: "1.8.3"
examples_zh:
  - "上次我们为什么把某个默认值从 60 改成 15？"
  - "这个项目在 Codex 和 DSH 里分别讨论过什么？"
  - "结论里提到的那条路径还能用吗？"
examples_en:
  - "Why did we change that default from 60 to 15?"
  - "What has this project discussed across Codex and DSH?"
  - "Is the path mentioned in old conclusions still valid?"
author: xmtx
---

# 智能体会话记忆

把本机多个 agent 的会话当**同一个项目的集体记忆**来查。核心用途是**少走弯路**：
别人（或另一个 agent）已经试过的方案、踩过的坑、改掉的路径，不用再试一遍。

## 环境（换机器会变，别写死）

- 工程目录由环境变量 `AGENT_MEMORY_HOME` 决定；未设时包装脚本会自己往上找 `cli.py`。
- **一律通过包装脚本调用**，不要手拼解释器路径：
  ```
  <skill>/scripts/am.ps1 <子命令> [参数]
  ```
  它负责解析工程目录、venv 与数据目录。**新机器首次使用先跑 `scripts\bootstrap.ps1`。**

## 首次使用（新机器必做）

平台包形态（本包：`SKILL.md` 在根，Python 引擎 `cli.py` 与 `collectors\` 也在根，
包装脚本在 `scripts\`；**平台只允许一级子目录**）：
```
powershell -NoProfile -ExecutionPolicy Bypass -File <skill>\scripts\bootstrap.ps1
```
它会：建 venv → 装依赖（**首次需联网**）→ 采集本机会话（约 45 秒）→ 打话题标签 →
生成本机 `index/entities.json` 与 `index/targets.json` → 打印**本机的实体名列表**。

- 解释器自动发现顺序：`uv` → `py` 启动器 → PATH 上的 `python` → 常见安装目录；都找不到会明确报错退 1。
- 数据落在 `<skill>\index\`（本机数据；会话原文不出本机）。
- 若拿到的是 **git 工程形态**（`cli.py` 在工程根、技能在 `skill\agent-memory`），
  首次安装改用工程根的 `install.ps1`——本包不含它。

## 什么时候用

| 用户在问 | 跑什么 |
|---|---|
| 「上次某件事怎么定的」「之前讨论过什么」 | `am.ps1 search` |
| 「这个方案是怎么一步步变成现在这样的」 | `am.ps1 timeline` |
| 「这条结论写在哪个文档里（而不是哪轮对话）」 | `am.ps1 search "<词>" --docs-first` |
| 「结论里那条路径/命令还能用吗」 | `am.ps1 premises` |
| 「这个结论现在还成立吗」「谁推翻过它」 | `am.ps1 cards list/search` |
| 「这条结论的原文上下文是什么」 | `am.ps1 cards open <卡id>` |
| 「卡片文件被我改坏了」 | `am.ps1 cards backup` / `am.ps1 cards restore --yes` |
| 「哪条结论的前提已经不成立了」 | `am.ps1 cards audit`（加 `--apply` 才写回） |
| 「这张卡是谁改的、改了什么」 | `am.ps1 cards log [<卡id>]`（每次写卡/推翻/巡检都留痕） |
| 「我有两张卡是不是在打架」 | `am.ps1 cards conflicts`（只报告，裁决靠 supersede） |
| 「让结论跟人换机器/进 docs」 | `am.ps1 cards export --md --dst <docs>/cards.md`（被推翻置顶） |
| 「这个事到底怎么做，给我一份要点」 | `am.ps1 cards distill --topic "<话题>" --dst <草稿路径>`（零 LLM，审后落 docs） |
| 「这条我已经看过了，别再推给我」 | `am.ps1 cards ack <卡id>`（内容一变或过期 30 天自动重现） |
| 「这些结论到底被用上了几条」 | `am.ps1 cards influence`（机械核验回答里的卡片 id 引用） |
| 「GitHub 上还有哪些同类项目、谁在追上来」 | `python scripts\gh_scan.py --query "topic:agent-memory" --out "$env:TEMP\ghscan\scan"`（指标+交集机械化，判定留人；**必须给 `--out`**，口径见 `references/gh-scan-playbook.md`；不设 `GITHUB_TOKEN` 走匿名限流） |
| 要让经验跟着人换机器 | `am.ps1 cards export/import` |
| 要给新会话准备环境事实 | `am.ps1 inject` |

**不要**用它回答"当前代码是什么样"——那是 `read`/`grep` 的活。它只回答"过去怎么看这件事"。

## 执行步骤

1. **先确认本机有哪些实体**（每台机器不同，不能猜）：
   ```
   <skill>\scripts\am.ps1 topics --top 20
   ```
   输出里的「实体 TOP」就是本机可用的话题名。**换台机器名字可能完全不同。**
2. **检索（升级式：docs 层 → 会话层）**。
   先给编译过的结论文档一次机会 —— 一条结论在 docs 里通常是一句话 + 日期 + 数字，
   在原始会话里却横跨几十条消息：
   ```
   <skill>\scripts\am.ps1 search "向量切块" --docs-first
   ```
   - 命中时输出 `文件:行号` + 小节 + 摘录，**并说明"要看原始会话请加 --with-sessions"**；
   - docs 层**按段落匹配**：同一段里分两句提到两个词也算命中（跨段不算，保住精度）；
   - docs 层没命中会明说「回落到会话检索」，然后照旧查会话层；
   - **没配置 docs 根时这一层自动关闭**（`am.ps1 docs` 可看配置与文件数，接法写在输出里）。

   然后才是查历史会话（中文必须加 `--trigram`）：
   ```
   <skill>\scripts\am.ps1 search "<关键词>" --trigram --limit 8
   ```
   - 索引命中 2~15 ms。**1~2 字的中文词**（`质保`/`预算`）trigram 命不中，会自动回退 `LIKE` 扫描
     （输出标 `索引=like`，约 100~150 ms）——**回退结果要如实说明**，不要说成"索引命中"。
   - 查询里含 `+ - * ^ : " ( )`（如 `C++`）会**报错退 2**（FTS5 语法字符），不是"没有历史记录"。
   - **多词中文查询**（`超时 回落`）走"按词交集"的精确 AND（输出标 `索引=like-and`）：
     两个词只要在同一条消息里出现就算命中，不要求相邻。
   - 若两个词分属**不同消息**，默认返回 0 并提示——要看"同一会话相邻 ≤3 条消息"里的共现，
     加 `--cross-message`（结果会标注这是跨消息命中，不是单条消息命中）。
3. **看演变**：
   ```
   <skill>\scripts\am.ps1 timeline "<实体>" ["<意图>"] --per 3
   ```
   意图标签是通用的（不随机器变）：检索与排序 / 评测与金标准 / 文档解析 / OCR与扫描件 /
   分块与切分 / 大纲与目录树 / 字段抽取 / 投标数值 / 上下文压缩 / 会话记录 /
   架构与选型 / 部署与性能 / 服务端接口 / Web前端 / 多agent委派 / 外部复核 /
   数据与库 / 合规与风险 / 投标业务
4. **引用旧结论里的路径/命令前先验**：
   ```
   <skill>\scripts\am.ps1 premises "<实体>" --top 5
   ```
4b. **想"这类事我以前是怎么做的/怎么修好的"**（procedure 挖掘，只读）：
   ```
   <skill>\scripts\am.ps1 procedure --kind all --top 10
   ```
   - `repair`：失败 → 改正 → 成功的形态对（"怎么修"）。实测 979 组，**基本是当次可查**，
     跨会话复用率极低（跨 ≥2 会话仅 1 组）——别指望它教新东西，它的价值是"同一个坑别再踩"。
   - `ritual`：同目录跨 ≥3 会话重复的命令（"每次都要做的仪式"，如 `cd <项目>` +
     `$env:PYTHONIOENCODING='utf-8'` + 项目 venv 的 python）。
   - 只读，不写库、不生成文件；`--kind repair|ritual` 单看一类。
   - **加 `--judge` 挂前提判定**（照做之前必须看）：🟢 活 / 🟡 项目已迁移（命令里的
     解释器/脚本路径已不在原位，照旧抄会失败）/ 🔴 前提已失效 / ⚪ 无可验证。
     每条还带**认证四件套**：认证时刻 ｜ 依据（如「2 条存在；1 条已迁移」）｜ 失效条件。
     实测价值：一条 22 个会话都在用的 `<旧项目>\venv\Scripts\python.exe` 仪式
     被判为 🟡 项目已迁移（→ `<新项目>`），照抄必挂。
   - **人类诊断**（WASP-Mem：一轮人诊断 > 四轮自判 7~11%，agent 组织、人诊断）：
     ```
     <skill>\scripts\am.ps1 diagnose list            # 待诊断候选（带稳定 id）
     <skill>\scripts\am.ps1 diagnose add rp_xxxxxxxx "当时是因为…应该…"
     <skill>\scripts\am.ps1 diagnose show
     ```
     诊断存 `index/procedure-diagnoses.json`（本机数据，不进版本库）。
     **已诊断的修复对 = 强证据**，不需要跨会话重复也会进注入块，并标注「人已诊断」。
   - **注入到 context.md**：`inject <实体> --procedures`（默认最多 3 条 / 500 字符，
     按项目作用域挑；🔴 前提失效的**不注入**，只报数量）。日更脚本已带此参数。
     注入块头会写**本块 token 成本**（各段分列）与展开命令——块是索引层，细节按需看。
   - **跨宿主（Codex / Claude Code / Cursor）**：两条互补路径
     ① `am install-host codex|claude` 装一行指令（推：告诉宿主开工调一次）；
     ② **MCP server**（拉）：`python mcp_server.py`，暴露 `memory_context` /
        `memory_search` / `memory_timeline` 三个**只读**工具，配置见
        `references/mcp.md`。MCP 不会被宿主主动调用——所以两者要一起用。
5. **生成环境事实块 / 相关结论**：
   ```
   <skill>\scripts\am.ps1 inject "<实体>" --write <项目>\.agent-memory\context.md
   <skill>\scripts\am.ps1 inject --cards --topic "<话题>" --max-cards 5
   ```
   - `--cards` 注入**相关结论卡**（有界）：按 `成立 > 未验证 > 前提已变` 排序，同一状态内
     证据多的先上；**截断会明说是"条数上限"还是"字符上限"**，便于你调对参数。
   - 默认不注入「已被推翻」的卡；要它们作为警示一起带，加 `--include-dead`（会标 `[已被推翻]`）。
   - **已确认过的卡默认不推**：`cards ack <卡id>` 记下确认后，内容指纹未变且未过 30 天就不再注入
     （`--include-acked` 忽略账本；`--ack-ttl-days` 改有效期）。跳过多少条在统计行里如实报出 ——
     "少推"必须可解释，不能变成静默丢条目。
   - **每条卡行尾带 `c-xxxxxxxx` id**（`--no-card-ids` 可关）：这是"记忆影响披露"的锚点 ——
     模型实质采用了某条结论时，回答开头要写「受 agent-memory 卡片 c-xxxx 影响」；
     `am.ps1 cards influence` 用同一串 id 机械统计"哪条真被用上了"。
     写答案时**自己也要遵守这条**：用了就写那一行，没用就不写、绝不编 id。
   - 可与实体同时给：卡片块在前、环境事实在后，写进同一个 context.md。
   - **先查过期**：`am.ps1 inject --check` —— 报 `STALE` 就说明日更任务断了，
     先 `scripts\refresh.ps1` 刷新，再按里面的"环境事实"工作（过期事实比没有事实更危险）。
   - 日更任务（每天 06:30）注册一次即可：
     `<skill>\scripts\register-refresh-task.ps1`；查状态加 `-Status`。
6. **会话收尾时写结论卡**（这一步只有你能做——你上下文里有全部证据，事后谁都无法重建）：
   ```
   <skill>\scripts\am.ps1 cards add --claim "<一句话可证伪断言>" --topic "<意图标签>" `
       --scope "<适用语料/版本/条件>" --status alive `
       --ev "<agent>|<YYYY-MM-DD>|<原文摘录≤200字>[|<session_id>]"
   ```
   - **一条断言一张卡**，不是一段总结；`--claim` 超过 300 字就该拆；
   - `--ev` 的**原文摘录是必填**：`session_id` 只在写卡这台机器有效，换机后回查不了原文，
     摘录才是证据的下限；
   - **能拿到消息 id 就带上第 5 段 `|<msg_id>`**（`messages.id`，来自 `search` 输出）：
     `cards open <卡id>` 会用它在**本机**打回原文全文（摘录只有 200 字，下钻才看得到上下文）；
     跨机时这个锚点会失效，`cards open` 会**如实说明"本机库里没有这条消息"**，不会伪造；
   - **标明作用域**：`--scopes "machine"`（全机通用）或 `--scopes "project:F:\demo-base"`
     （只对这个项目成立）。不写默认 `machine`；`cards list --scope-filter project:F:\demo-base`
     可按项目筛（子目录自动命中父项目）。
   - **写完自己核一遍**：`am.ps1 cards validate --verify` —— 它把每条 evidence 的 quote
     回到原文去对（不符会报出来并退 1）。可验证率低于 100% 就说明有引用不在原文里，别留着。
   - 新结论推翻了旧的，不要改旧卡内容，用 `cards add ... --also-supersede <旧卡id>`
     或 `cards supersede --old-id <旧卡> --new-id <新卡>` —— **被推翻的卡保留**，
     "过去这么认为、后来不成立"本身是最有用的经验；
   - 只写**有决策 / 有数字 / 有否定**的结论，不要给每轮对话都写卡。

## 报告纪律（很重要）

- **区分「过去这么认为」和「现在仍成立」**：引用旧结论必须说明时间与状态，不要讲成当前事实。
- **`premises` 三种结果含义不同**：
  🟢 前提通过 ｜ 🟡 **项目已改名**（旧路径换算成新路径，结论可能仍有效）｜ 🔴 **真失效**（勿照做）。
  **不要把 🟡 说成"失效"**——那会把仍有效的经验误杀。
- **97% 的结论没有可验证前提**，无法机械判定是否过时；引用时标注"未验证"。
- 说"上次结论说 X"必须能给**会话标题与日期**，不能只给一句转述。

详见 `@references/cli.md`（命令与参数）与 `@references/verdicts.md`（判定语义与局限）；
**版本与变更**见 `@references/changelog.md`（每次发版先写条目再升 `version`）。

## 失败处理

- **先跑 `am.ps1 doctor`**：它一次报全（工程目录 / Python / 依赖 / 索引库 / 数据 schema 版本 /
  会话消息数 / peer 覆盖 / 最近入库 / 技能版本），退出码 0=能用、1=有阻塞问题。
- 索引不存在/报错 → 跑一次 `am.ps1 ingest`（约 6 个来源合计 90 秒；**只读各 agent 原始数据**）。
- 查询无结果 → 换同义词/英文术语再试；仍无结果就如实说"没有历史记录"，**不要编**。
- 实体名不确定 → 跑 `am.ps1 topics --top 20` 看本机实际有哪些。
- **报 "agent-memory is NOT initialised" 且退出码 3** → 这台机器还没装工程（技能只是壳，库与 venv 在工程目录里）。
  按报错里的 fix A/B/C 三选一：① `$env:AGENT_MEMORY_HOME = '<含 cli.py 的目录>'`
  （或在 `%USERPROFILE%\agent-memory` 建 junction 指向工程）；② git 工程跑工程根 `install.ps1`；
  ③ 平台包跑技能里的 `scripts\bootstrap.ps1`。
- **报 "python not found"（退出码也是 3）** → 同上，先跑一次 `install.ps1` / `bootstrap.ps1` 建 venv。
- 报 "dependency install failed" → 首次装依赖要联网；配好源后重跑同一条命令即可（已建好的 venv 会复用）。
