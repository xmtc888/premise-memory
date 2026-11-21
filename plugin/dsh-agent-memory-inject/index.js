/**
 * DSH 插件：把 agent-memory 的「相关结论卡」注入 system prompt。
 *
 * 架构（2026-11-10 事故后重构，全部有 app.asar 源码证据）：
 *   · 结论卡走 `ctx.systemPrompt.section(...)`（dsh-system-prompt lib:253、:388）。
 *     README: "Sections become prompt text" —— 不落 user/message、不进会话历史、
 *     不需要 `source`，每次 assemble 由同步 `text()` 读缓存，零 I/O。
 *   · `agent/pre-step` 只负责**异步刷新缓存**，绝不改写 decision。
 *     内核 `dsh-agent-loop/lib/index.js:1067-1072` 会把 `decision.messages`
 *     逐条 append 成 `user/message`，且 :1067 无保护读 `message.source.kind`；
 *     旧实现往这里塞一条无 source 的 user 消息，就是反复报
 *     `Cannot read properties of undefined (reading 'kind')` 的根因。
 *   · `agent/request` waterfall（:1191-1195）payload 只有 {turn,step,signal}，
 *     decision 是 request config（provider/model/...），**不含 messages**，
 *     所以「迁移到 agent/request」不可行，section 才是正确扩展点。
 *
 * 契约（waterfall 侧，保留 ② 的硬化）：
 *   · decision 必须带 `kind`（内核 :922、dsh-agent-instructions :1412 会读）；
 *   · `next()` 每调一次会 shift 掉一个下游 listener，**只能调用一次**；
 *   · 任何异常 / 下游返回 undefined ⇒ 用 `{kind:"enter", messages:claimed}` 兜底。
 *
 * 时序：pre-step 发生在 `systemPrompt.assemble()` **之后**，且刷新是 fire-and-forget，
 * 所以本轮刷新结果下一轮生效；apply 时预热一次，首轮通常就有卡片。
 */
import { execFile } from "node:child_process";
import { appendFileSync } from "node:fs";
import { DISCLOSE_HINT, extractBlock, MARK_BEGIN, MARK_END } from "./lib/block.mjs";

export const name = "agent-memory-inject";

/** systemPrompt 服务由 @deepseek-ai/dsh-system-prompt 提供（lib:226 `super(ctx,"systemPrompt")`）。 */
export const inject = ["systemPrompt"];

const BRAND = "agent-memory-inject";

/** 落一行 trace（配置了 tracePath 才写）。DSH 不持久化插件日志，这是唯一可靠的证据。 */
function trace(cfg, rec) {
	if (!cfg.tracePath) return;
	try {
		appendFileSync(cfg.tracePath, JSON.stringify({ at: new Date().toISOString(), ...rec }) + "\n");
	} catch { /* ignore */ }
}

/** 跑一次 `cli.py inject --cards`，拿到卡片块正文。 */
function runCli(cfg) {
	return new Promise((resolve) => {
		const args = [
			cfg.cliPath,
			"inject", "--cards",
			"--max-cards", String(cfg.maxCards ?? 5),
			"--card-chars", String(cfg.maxChars ?? 900),
		];
		if (cfg.topic) args.push("--topic", cfg.topic);
		if (cfg.scope) args.push("--scope-filter", cfg.scope);
		execFile(cfg.pythonPath, args, {
			cwd: cfg.repoPath,
			timeout: cfg.timeoutMs ?? 3000,
			windowsHide: true,
		}, (err, stdout) => resolve(err ? "" : String(stdout || "")));
	});
}

/**
 * @param {import("@deepseek-ai/cordis").Context} ctx
 * @param {object} config
 */
export function apply(ctx, config = {}) {
	const cfg = {
		pythonPath: config.pythonPath || "",
		cliPath: config.cliPath || "",
		repoPath: config.repoPath || "",
		maxCards: config.maxCards ?? 5,
		maxChars: config.maxChars ?? 900,
		cooldownMs: config.cooldownMs ?? 120000,
		timeoutMs: config.timeoutMs ?? 3000,
		tracePath: config.tracePath || "",
		topic: config.topic || "",
		scope: config.scope || "",
		sectionOrder: Number.isFinite(config.sectionOrder) ? config.sectionOrder : 9500,
	};
	if (!cfg.pythonPath || !cfg.cliPath) return;   // 配置不全：不挂任何东西

	let cachedText = "";
	let lastRefreshAt = 0;

	async function refresh() {
		const now = Date.now();
		if (lastRefreshAt && now - lastRefreshAt < cfg.cooldownMs) {
			trace(cfg, { action: "skip", reason: "cooldown" });
			return;
		}
		const out = await runCli(cfg);
		const block = extractBlock(out);
		if (!block) {
			trace(cfg, { action: "skip", reason: "empty", cliChars: out.length });
			return;
		}
		cachedText = `${MARK_BEGIN}\n${block}\n\n${DISCLOSE_HINT}\n${MARK_END}`;
		lastRefreshAt = Date.now();
		trace(cfg, { action: "refresh", chars: cachedText.length });
	}

	function refreshInBackground() {
		try {
			void refresh().catch((err) => trace(cfg, { action: "error", error: String(err?.message || err) }));
		} catch (err) {
			trace(cfg, { action: "error", error: String(err?.message || err) });
		}
	}

	// ① 内容投递：system prompt section（同步读缓存，不产生任何会话消息）
	try {
		if (typeof ctx?.systemPrompt?.section !== "function") {
			trace(cfg, { action: "error", error: "systemPrompt service unavailable" });
		} else {
			ctx.systemPrompt.section({
				name: "agent-memory:cards",
				order: cfg.sectionOrder,
				interpolate: false,
				text: () => cachedText,
			});
		}
	} catch (err) {
		trace(cfg, { action: "error", error: `section registration: ${String(err?.message || err)}` });
	}

	// 预热：首轮 assemble 前尽量填好缓存（失败也只影响内容，不影响宿主）
	refreshInBackground();

	// ② 只刷新缓存，原样交还 decision
	ctx.on("agent/pre-step", async (payload, next) => {
		const claimed = Array.isArray(payload?.messages) ? payload.messages : [];
		const safeDecision = () => ({ kind: "enter", messages: claimed });
		if (typeof next !== "function") {
			trace(cfg, { action: "error", error: "next is not a function (bad waterfall arity)" });
			return safeDecision();
		}
		let decision;
		try {
			decision = await next();                    // 唯一一次 next()
		} catch (err) {
			trace(cfg, { action: "error", error: String(err?.message || err) });
			try { ctx.logger?.warn?.(`${BRAND}: ${err?.message || err}`); } catch { /* ignore */ }
			return safeDecision();
		}
		refreshInBackground();                         // 不阻塞、不改 decision
		return decision ?? safeDecision();
	});
}

export default { name, inject, apply };
