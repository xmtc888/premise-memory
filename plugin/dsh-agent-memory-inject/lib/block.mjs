/**
 * agent-memory 注入插件的纯逻辑（可单测，不碰宿主）。
 *
 * 为什么单独一层：宿主 hook 写错会**让每轮对话失败**（DSH 的 waterfall 对 arity、
 * decision 形状、消息 source 都敏感）。纯函数层把「要不要注入、注入什么」钉死，
 * 宿主侧只负责返回一个合法的 decision。
 */

/** 注入块的起止标记：幂等性靠它判断「这一轮已经注入过」。 */
export const MARK_BEGIN = "<!-- agent-memory:cards-injected -->";
export const MARK_END = "<!-- /agent-memory:cards-injected -->";

/**
 * 披露要求：把「这次受哪条记忆影响」变成一句可核验的话（2026-11-19）。
 *
 * 关键设计：**要求带卡片 id**。注入块行尾已经是 `c-<sha8>` 固定格式，
 * 于是「模型自称用了哪条」与「`cards influence` 统计到哪条」是同一串字符 ——
 * 披露不靠自觉，而是可对账。没用到时明确要求它**不写**，避免为凑格式编 id。
 */
export const DISCLOSE_HINT = [
	"披露要求：若本次回答**实质**采用了上面某条结论（不是顺带提到），",
	"在回答开头用一行写「受 agent-memory 卡片 c-xxxxxxxx 影响」并说明它改变了什么；",
	"没用到就不写这一行，也不要为凑格式编造 id。",
].join("\n");

/**
 * 判断消息数组里是否已包含注入块（防止每步重复注入把上下文撑爆）。
 * @param {Array<{role?: string, content?: unknown}>} messages
 */
export function hasBlock(messages) {
	if (!Array.isArray(messages)) return false;
	for (const m of messages) {
		const c = typeof m === "string" ? m : m && m.content;
		if (typeof c === "string" && c.includes(MARK_BEGIN)) return true;
	}
	return false;
}

/**
 * 从 agent-memory 的输出里裁出卡片块正文。
 * CLI 输出形如：
 *   ### 相关结论（agent-memory 卡片；按 status→证据数→更新时间排序）
 *   - [成立] 断言（machine；证据 1 条）
 *   --- 统计: 卡片：候选 2 ｜ 注入 2 ｜ 截断 0 ｜ 81 字符
 * 只保留标题与条目行，丢掉统计行与空段。
 * @param {string} stdout
 */
export function extractBlock(stdout) {
	const lines = String(stdout || "").split(/\r?\n/);
	const keep = [];
	for (const line of lines) {
		if (line.startsWith("### 相关结论") || line.startsWith("- [")) keep.push(line.trimEnd());
	}
	return keep.join("\n").trim();
}

/**
 * 决定这次 pre-step 要不要注入、注入什么。
 *
 * 三条硬性拒绝（宁可不注入，也不污染上下文）：
 *   1. 本轮已有注入块（幂等）；
 *   2. 冷却时间未到（默认 120s，避免每步都跑 CLI）；
 *   3. 抽不出正文（CLI 失败/没卡）。
 *
 * @param {object} p
 * @param {Array} p.messages
 * @param {string} p.stdout        CLI 输出
 * @param {number} p.now           当前毫秒时间戳
 * @param {number} p.lastAt        上次注入的毫秒时间戳（0=从未）
 * @param {number} [p.cooldownMs]  冷却
 * @returns {{action: "skip"|"inject", reason?: string, block?: string}}
 */
export function decide({ messages, stdout, now, lastAt, cooldownMs = 120000 }) {
	if (hasBlock(messages)) return { action: "skip", reason: "already-injected" };
	if (lastAt && now - lastAt < cooldownMs) return { action: "skip", reason: "cooldown" };
	const block = extractBlock(stdout);
	if (!block) return { action: "skip", reason: "empty" };
	return { action: "inject", block };
}

/**
 * 把注入块包装成一条 user 消息追加在末尾。
 *
 * ⚠️ `source` 不能省：DSH 内核 `dsh-agent-loop/lib/index.js:1067` 与内置
 * `dsh-agent-instructions/lib/index.js:1198/1201/1205` 都无保护地读
 * `message.source.kind`。2026-11-10 的 `Cannot read properties of undefined
 * (reading 'kind')` 事故，缺 source 就是两条触发路径之一。
 * `kind:"user"` 与会话里真实用户消息一致，不引入新角色/新形状。
 *
 * @param {Array} messages
 * @param {string} block
 */
export function appendBlock(messages, block) {
	const text = `${MARK_BEGIN}\n${block}\n${MARK_END}`;
	const message = {
		role: "user",
		content: text,
		source: { kind: "user" },
	};
	return [...(Array.isArray(messages) ? messages : []), message];
}
