/**
 * 端到端本地验证（不重启宿主）：用假 ctx 模拟 `ctx.systemPrompt.section` + `ctx.on`，
 * 真实调用 `F:\agent-memory\cli.py inject --cards`，检查：
 *   1. 注册了 section，预热后 text 含真实结论卡与起止标记；
 *   2. `agent/pre-step` 原样交还 decision（不追加消息、不丢字段）；
 *   3. CLI 配错 ⇒ section 保持空、decision 原样、不抛错。
 *
 * 与 `contract.test.mjs` 的差别：这里用真实卡片数据，不依赖 stub。
 */
import assert from "node:assert/strict";

import { apply } from "../index.js";

const PY = process.env.AM_PY || "F:\\agent-memory\\.venv\\Scripts\\python.exe";
const CLI = "F:\\agent-memory\\cli.py";
const REPO = "F:\\agent-memory";

function fakeCtx() {
	const handlers = new Map();
	const sections = [];
	return {
		handlers,
		sections,
		systemPrompt: { section: (def) => { sections.push(def); return () => {}; } },
		on(name, fn) { handlers.set(name, fn); },
		logger: { warn: (m) => console.log("[warn]", m) },
	};
}

const nextOf = (messages) => async () => ({ kind: "enter", messages, startsRequestSeries: true });

async function waitFor(predicate, timeoutMs = 5000) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (predicate()) return;
		await new Promise((r) => setTimeout(r, 50));
	}
	throw new Error("waitFor timeout: cached section text never appeared");
}

async function main() {
	const ctx = fakeCtx();
	apply(ctx, { pythonPath: PY, cliPath: CLI, repoPath: REPO, maxCards: 5, cooldownMs: 0 });
	const handler = ctx.handlers.get("agent/pre-step");
	assert.ok(handler, "必须注册 agent/pre-step");
	assert.equal(ctx.sections.length, 1, "必须注册 system prompt section");

	// ── 1) 真实 CLI 的结论卡进入 section ──
	await waitFor(() => ctx.sections[0].text({}).includes("<!-- agent-memory:cards-injected -->"));
	const injected = ctx.sections[0].text({});
	assert.ok(injected.includes("### 相关结论"), "要有卡片标题");
	assert.ok(!injected.includes("统计"), "统计行不该进上下文");
	console.log("section 内容预览:\n" + injected.slice(0, 300));

	// ── 2) pre-step 不改 decision ──
	const msgs = [{ role: "user", content: "打包闸门怎么做的", source: { kind: "user" } }];
	const base = nextOf(msgs);
	const d1 = await handler({ agent: { id: "a1" }, messages: msgs }, base);
	assert.equal(d1.kind, "enter");
	assert.equal(d1.startsRequestSeries, true);
	assert.equal(d1.messages.length, 1, "绝不再往 decision.messages 塞消息");

	// ── 3) CLI 配错：安全降级 ──
	const ctx2 = fakeCtx();
	apply(ctx2, { pythonPath: "C:\\nope\\python.exe", cliPath: CLI, repoPath: REPO, cooldownMs: 0 });
	const h2 = ctx2.handlers.get("agent/pre-step");
	const d2 = await h2({ agent: { id: "a2" }, messages: msgs }, nextOf(msgs));
	assert.equal(d2.messages.length, 1);
	assert.equal(ctx2.sections[0].text({}), "", "CLI 失败时 section 保持空");

	console.log("\nOK: 真实 CLI 注入 / decision 原样 / 失败降级 三项通过（宿主外验证）");
}

main().catch((e) => { console.error("FAIL:", e.message); process.exit(1); });
