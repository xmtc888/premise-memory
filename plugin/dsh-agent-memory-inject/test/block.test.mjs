/**
 * 纯逻辑单测：`node --test test/`
 *
 * 为什么只测这一层：宿主 hook 的行为要重启 DSH 才能验（而且改错会让每次工具调用失败）。
 * 但"要不要注入、注入什么、会不会重复"这三件事是纯函数，可以在这里钉死。
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
	MARK_BEGIN, MARK_END, appendBlock, decide, extractBlock, hasBlock,
} from "../lib/block.mjs";

const CLI_OUT = `### 相关结论（agent-memory 卡片；按 status→证据数→更新时间排序）

- [成立] 标志污染已修：补检前后快照恢复（machine；证据 1 条）
- [已被推翻] 别名补检不得改写主查询的如实现报标志（machine；证据 1 条）

--- 统计: 卡片：候选 2 ｜ 注入 2 ｜ 截断 0 ｜ 81 字符
`;

test("extractBlock 只留标题与条目，丢掉统计行", () => {
	const b = extractBlock(CLI_OUT);
	assert.ok(b.startsWith("### 相关结论"));
	assert.ok(b.includes("[成立]"));
	assert.ok(b.includes("[已被推翻]"));
	assert.ok(!b.includes("统计"), "统计行不该进上下文");
});

test("extractBlock 对空输出返回空串", () => {
	assert.equal(extractBlock(""), "");
	assert.equal(extractBlock("--- 统计: 卡片：候选 0"), "");
});

test("hasBlock 能识别已注入", () => {
	const msgs = [{ role: "user", content: "hi" }];
	assert.equal(hasBlock(msgs), false);
	const after = appendBlock(msgs, "### 相关结论\n- [成立] x");
	assert.equal(hasBlock(after), true);
});

test("decide：本轮已注入过就跳过（幂等）", () => {
	const msgs = appendBlock([], "### 相关结论\n- [成立] x");
	const d = decide({ messages: msgs, stdout: CLI_OUT, now: 1e12, lastAt: 0 });
	assert.equal(d.action, "skip");
	assert.equal(d.reason, "already-injected");
});

test("decide：冷却期内跳过（避免每步跑 CLI）", () => {
	const d = decide({ messages: [], stdout: CLI_OUT, now: 1000, lastAt: 900, cooldownMs: 500 });
	assert.equal(d.action, "skip");
	assert.equal(d.reason, "cooldown");
});

test("decide：冷却过后注入，且块里没有统计行", () => {
	const d = decide({ messages: [], stdout: CLI_OUT, now: 1e6, lastAt: 1000, cooldownMs: 500 });
	assert.equal(d.action, "inject");
	assert.ok(d.block.includes("[成立]"));
	assert.ok(!d.block.includes("统计"));
});

test("decide：CLI 没输出（失败/无卡）就不注入", () => {
	const d = decide({ messages: [], stdout: "", now: 1e6, lastAt: 0 });
	assert.equal(d.action, "skip");
	assert.equal(d.reason, "empty");
});

test("appendBlock 不修改原数组，且带起止标记", () => {
	const orig = [{ role: "user", content: "hi" }];
	const next = appendBlock(orig, "### 相关结论\n- [成立] x");
	assert.equal(orig.length, 1, "不许原地改宿主的 messages");
	assert.equal(next.length, 2);
	assert.equal(next[1].role, "user");
	assert.ok(next[1].content.includes(MARK_BEGIN));
	assert.ok(next[1].content.includes(MARK_END));
});
