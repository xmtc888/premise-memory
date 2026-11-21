/**
 * 宿主契约回归测试：`node --test test/contract.test.mjs`
 *
 * 钉死 2026-11-10 事故（`Cannot read properties of undefined (reading 'kind')`）：
 *   旧实现往 `agent/pre-step` 的 decision.messages 塞一条无 source 的 user 消息，
 *   内核 dsh-agent-loop:1067 读 message.source.kind 直接抛错。
 * 新架构：内容走 systemPrompt.section，pre-step 原样交还 decision。
 * 这里同时保留「下游返回 undefined」「catch 不二次 next」等瀑布契约回归。
 *
 * miniWaterfall 等价于 Cordis 4.0.1 `EventsService.waterfall`（lib/index.js:317-325）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

import { apply, inject, name } from "../index.js";
import { appendBlock, MARK_BEGIN } from "../lib/block.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const STUB = join(HERE, "fixtures", "cli_stub.py");
const PY = process.env.AM_PY
	|| "C:/Users/xmtx/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe";

function makeConfig(overrides = {}) {
	const dir = mkdtempSync(join(tmpdir(), "am-contract-"));
	const tracePath = join(dir, "trace.ndjson");
	const cfg = {
		pythonPath: PY,
		cliPath: STUB,
		repoPath: HERE,
		maxCards: 5,
		maxChars: 900,
		cooldownMs: 0,
		timeoutMs: 3000,
		tracePath,
		...overrides,
	};
	return { cfg, tracePath };
}

function handler(configOverrides) {
	const { cfg, tracePath } = makeConfig(configOverrides);
	const handlers = [];
	const sections = [];
	const ctx = {
		systemPrompt: { section: (def) => { sections.push(def); return () => {}; } },
		on: (event, fn) => { if (event === "agent/pre-step") handlers.push(fn); },
		logger: { warn() {} },
	};
	apply(ctx, cfg);
	assert.equal(handlers.length, 1, "必须注册且只注册一个 agent/pre-step");
	return { run: handlers[0], sections, tracePath };
}

const payload = (messages = [{ role: "user", content: "hi", source: { kind: "user" } }]) => ({
	agent: { id: "test-agent" },
	messages,
	step: 1,
	turn: 1,
	signal: new AbortController().signal,
});

function miniWaterfall(listeners, args, fallback) {
	const cbs = [...listeners];
	const next = () => (cbs.shift() ?? fallback)(...args);
	args.push(next);
	return next();
}

async function waitFor(predicate, timeoutMs = 4000) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (predicate()) return;
		await new Promise((r) => setTimeout(r, 50));
	}
	throw new Error("waitFor timeout");
}

test("插件声明 systemPrompt 服务依赖", () => {
	assert.equal(name, "agent-memory-inject");
	assert.deepEqual(inject, ["systemPrompt"]);
});

test("appendBlock 回归：注入消息必须带 source.kind（事故路径不再使用，但契约钉死）", () => {
	const before = [{ role: "user", content: "hi", source: { kind: "user" } }];
	const after = appendBlock(before, "### 相关结论\n- [成立] x");
	assert.equal(before.length, 1);
	assert.equal(after[1].source?.kind, "user");
});

test("apply 注册 system prompt section：有限 order / 关闭插值 / 同步 text", () => {
	const { sections } = handler();
	assert.equal(sections.length, 1);
	const s = sections[0];
	assert.equal(s.name, "agent-memory:cards");
	assert.ok(Number.isFinite(s.order));
	assert.equal(s.interpolate, false);
	assert.equal(typeof s.text, "function");
	assert.equal(typeof s.text({}), "string");
});

test("预热后 section.text() 带结论卡，且 trace 记 refresh", async () => {
	const { sections, tracePath } = handler();
	await waitFor(() => sections[0].text({}).includes(MARK_BEGIN));
	const text = sections[0].text({});
	assert.ok(text.includes("[成立]"));
	assert.ok(!text.includes("统计"));
	assert.ok(readFileSync(tracePath, "utf8").includes('"action":"refresh"'));
});

test("pre-step 原样交还 decision：不追加消息、不丢字段", async () => {
	const { run } = handler();
	const p = payload();
	const base = { kind: "enter", messages: [...p.messages], startsRequestSeries: true };
	const d = await run(p, async () => base);
	assert.equal(d, base, "必须原样返回，不能改写 decision.messages");
	assert.equal(d.messages.length, 1);
	assert.equal(d.startsRequestSeries, true);
});

test("下游 next() 返回 undefined：兜底为带 kind 的 decision", async () => {
	const { run } = handler();
	const d = await run(payload(), async () => undefined);
	assert.ok(d);
	assert.equal(d.kind, "enter");
	assert.ok(Array.isArray(d.messages));
});

test("真实链：内置插件风格 listener 读 message.source.kind 不抛错", async () => {
	const { run } = handler();
	const builtinLike = async (p, next) => {
		const decision = await next();
		if (decision.kind === "reject") return decision;               // dsh-agent-instructions:1412
		for (const m of decision.messages) {
			if (m.source.kind === "runtime-context") return decision;   // dsh-agent-loop:1067
		}
		return decision;
	};
	const p = payload();
	const d = await miniWaterfall([builtinLike, run], [p],
		() => Promise.resolve({ kind: "enter", messages: [...p.messages], startsRequestSeries: true }));
	assert.equal(d.kind, "enter");
	assert.equal(d.messages.length, 1, "不再往 decision.messages 塞消息");
});

test("下游抛错：只允许调用一次 next()，并返回安全 decision", async () => {
	const { run } = handler();
	let calls = 0;
	const next = () => { calls += 1; throw new Error("downstream boom"); };
	const d = await run(payload(), next);
	assert.equal(calls, 1, "二次调用 next() 会重复消费 waterfall 链");
	assert.equal(d.kind, "enter");
});

test("next 不是函数（arity 事故）：返回安全 decision，不抛", async () => {
	const { run } = handler();
	const d = await run(payload(), undefined);
	assert.equal(d.kind, "enter");
});

test("CLI 失败：pre-step 仍原样放行，section 文本保持为空", async () => {
	const { run, sections } = handler({ pythonPath: "C:/definitely-missing/python.exe" });
	const p = payload();
	const base = { kind: "enter", messages: [...p.messages] };
	const d = await run(p, async () => base);
	assert.equal(d, base);
	assert.equal(sections[0].text({}), "");
});

test("多 listener 链（双实例）：decision 合法且消息数不增长", async () => {
	const a = handler();
	const b = handler();
	const p = payload();
	const d = await miniWaterfall([a.run, b.run], [p],
		() => Promise.resolve({ kind: "enter", messages: [...p.messages], startsRequestSeries: true }));
	assert.equal(d.kind, "enter");
	assert.equal(d.messages.length, 1);
	assert.equal(a.sections.length, 1);
	assert.equal(b.sections.length, 1);
});
