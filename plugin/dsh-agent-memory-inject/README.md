# dsh-agent-memory-inject（2026-11-10 起在本机激活）

把 agent-memory 的「相关结论卡」作为 **system prompt section** 注入 DSH 上下文。

## 为什么

agent-memory 原本是**查询期**记忆：会话结束才 ingest，要查才查。
市场调研（`docs/design/2026-11-10_agent-memory改进清单_借鉴市场机制.md` 第 9 条）显示
66 个记忆类插件里有 12 个在做「before the model speaks 的自动注入」——
**我们有材料，缺的正是"在模型开口前把相关结论递上去"这一步。**

## 架构（2026-11-10 事故后重构）

```
apply()                 →  ctx.systemPrompt.section({ text: () => cachedText })
                           section 文本随缓存刷新，每次 assemble 同步读取
agent/pre-step          →  只异步刷新缓存，decision 原样交还 waterfall
```

旧实现往 `agent/pre-step` 的 `decision.messages` 追加一条无 `source` 的 user 消息。
内核 `dsh-agent-loop/lib/index.js:1067-1072` 会把 decision.messages 逐条 append 成
`user/message`，并在 `:1067` 无保护读 `message.source.kind` ⇒ 每轮报
`Cannot read properties of undefined (reading 'kind')`。
另有一条路径是 waterfall 返回 undefined decision（内核 `:922` /
`dsh-agent-instructions:1412` 读 `decision.kind`）。

源码事实：

- `agent/request` waterfall（`:1191-1195`）payload 只有 `{turn,step,signal}`，
  decision 是 request config，**不含 messages**，不能用来注入内容。
- `ctx.systemPrompt.section`（`dsh-system-prompt` lib:253/388）的 `text(context)`
  是同步函数；sections 变成 prompt 文本，不落 `user/message`、不进会话历史、
  不需要 `source`，也不会被内置 instructions 当成权威消息。
- 结论卡由 CLI 异步刷新并缓存（冷却默认 120s）；pre-step 在 `assemble()` 之后触发，
  所以刷新结果下一轮生效，`apply()` 里预热一次让首轮通常也有卡片。

## 保守策略

| 策略 | 原因 |
|---|---|
| 冷却 120s | 每步 fork 一次 Python 太贵 |
| CLI 超时 3s | 慢就不刷新，不许拖慢对话 |
| 刷新失败只写 trace | 绝不影响 decision / 对话 |
| `next()` 只调一次 | Cordis waterfall 的 next 会 shift 下游 listener |
| 下游无 decision ⇒ `{kind:"enter", messages:claimed}` 兜底 | 不给内核 undefined |

## 契约（来自 app.asar 源码，不是猜的）

`agent/pre-step` 是 **waterfall** `(payload, next)`，arity 两元承重；
decision 必须带 `kind`；`agent/request` 只改 request config。

## 安装（实测可行的那条路，需要重启 DSH）

⚠️ **别用手工 junction + 手工 YAML**：那样能跑起来，但**活不过一次重启**——
宿主启动时会按 `pnpm-lock.yaml` 重整 `node_modules`（没记录的链接被 prune），
并按自己的状态重写 `cordis.patch.yml`（手加条目消失）。实测踩过两轮。

必须同时满足三件事：**代码在 vendor**、**依赖进 pnpm-lock**、**patch 的 name 用 hoisted 后的实际目录名**。

```powershell
$p = "$env:USERPROFILE\.dsh\profiles\desktop"
Copy-Item "F:\agent-memory\plugin\dsh-agent-memory-inject" "$env:USERPROFILE\.dsh\vendor\dsh-agent-memory-inject" -Recurse -Force

# ① 关键：必须用与 node_modules 同版本的 pnpm（查 node_modules\.modules.yaml 的 packageManager）
cd $p
npx --yes pnpm@11.28.5 add -w --save "link:../../vendor/dsh-agent-memory-inject"
```

② 在 `$p\cordis.patch.yml` 末尾加（`name` 用**实际目录名** `dsh-agent-memory-inject`，
不是 `@local/...`：hoisted 模式下 pnpm 按包名首段扁平化，写 `@local/` 会解析不到）：

```yaml
- insert:
    - id: agent-memory-inject
      name: 'dsh-agent-memory-inject'
      config:
        pythonPath: '<…>\dependencies\python\python.exe'
        cliPath: 'F:/agent-memory/cli.py'
        repoPath: 'F:/agent-memory'
        maxCards: 5
        maxChars: 900
        cooldownMs: 120000
        timeoutMs: 3000
        tracePath: '<…>\.dsh\logs\agent-memory-inject.trace.ndjson'
```

③ 校验 patch（10 条断言，能防手误）：`node <troubleshooting>\work\validate_patch.mjs` → `RESULT: ALL_OK`。

④ **重启 DSH**（插件条目不热加载）。

## 验证（三步，缺一不可）

1. **trace 有新行**：`%USERPROFILE%\.dsh\logs\agent-memory-inject.trace.ndjson` 出现
   `{"action":"refresh","chars":N}`（`inject/skip/error` 是旧版动作名，`refresh` 才是当前实现）；
2. **system prompt 里有块**：模型上下文出现 `<!-- agent-memory:cards-injected -->` 包裹的结论卡
   —— 最直接的证据是问模型「你上下文里有没有这段标记」；
3. **会话日志里没有**：`user/message` 事件**不应**含该标记（这是与旧实现的关键区别；
   旧实现会污染对话历史，还因缺 `source.kind` 让宿主每轮报错）。

## 回滚

```powershell
cd "$env:USERPROFILE\.dsh\profiles\desktop"
npx --yes pnpm@11.28.5 remove -w dsh-agent-memory-inject
# 再从 cordis.patch.yml 删掉上面那段 - insert: 块，重启
```

删完可顺手删 `vendor\dsh-agent-memory-inject`（不删也不影响：没有依赖引用就不会被加载）。

## 版本纪律（2026-11-11 补，踩过）

插件自己的 `package.json` 的 `version` **就是市场页面上显示的那个号**。
2026-11-10 首版是 `0.1.0`，当晚做了架构级重构（追加 user 消息 → system prompt section +
缓存/后台刷新/trace），却**忘了 bump** —— 结果市场里一直显示 `v0.1.0`，
而 `0.1.0` 恰恰是"缺 `source` 会让宿主每轮报错"的那一版，用户无法区分。

规矩：

- **行为变更就 bump**：改注入方式/改策略/改默认值 = `minor`；只改文案/注释 = `patch`；
- **两边一起改**：仓库 `plugin/dsh-agent-memory-inject/package.json` 与
  `~/.dsh/vendor/dsh-agent-memory-inject/package.json` 必须同版本、同内容
  （vendor 是宿主实际加载的那份，仓库是唯一真源）；
- 改完 `node --check index.js` + 从 `node_modules` 位置 `import` 一次，确认还能加载。

当前版本：**0.3.0**（`inject: ["systemPrompt"]`，system prompt section 注入 + 披露要求）。

## 记忆影响披露（0.3.0，2026-11-19）

注入块尾部拼一行固定的披露要求（`lib/block.mjs` 的 `DISCLOSE_HINT`）：模型**实质采用**了
某条卡时，回答开头写「受 agent-memory 卡片 c-xxxxxxxx 影响」。

id 由 CLI 侧写在每条卡行尾（`inject --cards` 默认带，`--no-card-ids` 可关），
于是「模型自称用了哪条」与 `am cards influence` 的统计是**同一串字符** —— 披露可对账，
不是自觉。要求里明写"没用到就不写、不要编 id"，避免为凑格式产生假披露。

- 成本：多约 90 字符，进 system prompt，不进对话历史；
- 插件侧改动（index.js / lib/block.mjs）**需要重启 DSH** 才生效（插件条目不热加载）。

## 局限（据实记录）

- **只在 hoisted 布局、pnpm 11.28.5 的这套 profile 上验证过**。换 pnpm 版本会报
  `ERR_PNPM_VIRTUAL_STORE_DIR_MAX_LENGTH_DIFF`，那是在要求**重建整个 node_modules**，
  不要贸然执行（会动整个 DSH 的依赖树）。
- `systemPrompt.section` 的 `order` 是相对排序（默认 9500），改了只影响它在 system prompt 里的位置。
- 注入内容**每 120s 才可能刷新一次**，且刷新在后台；同一轮内看到的是缓存。

## 测试

```powershell
$node = 'C:\Users\demo\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe'
& $node --test test/block.test.mjs test/contract.test.mjs   # 19 条
& $node test/e2e_local.mjs                                  # 真实 CLI 端到端
```

`contract.test.mjs` 钉死事故两条路径（undefined decision / 缺 source）、
catch 不二次 next、section 注册与刷新；宿主内的真实行为仍需重启验证。
