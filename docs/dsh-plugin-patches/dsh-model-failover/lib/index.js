import { defineTool } from '@deepseek-ai/dsh-tools';
import { contentHasImage } from '@deepseek-ai/dsh-llm';
import z from 'schemastery';
export const name = '@dsh-external/dsh-model-failover';
export const inject = ['tools', 'llm'];
export const Config = z.object({
    maxFailoversPerStep: z.number().step(1).min(1).default(3),
    catalogTtlMs: z.number().step(1).min(0).default(30000),
    cooldownMs: z.object({
        quota: z.number().step(1).min(0).default(30 * 60_000),
        credential: z.number().step(1).min(0).default(60 * 60_000),
        vanished: z.number().step(1).min(0).default(6 * 60 * 60_000),
        server: z.number().step(1).min(0).default(2 * 60_000),
        transport: z.number().step(1).min(0).default(60_000),
        timeout: z.number().step(1).min(0).default(2 * 60_000),
        'rate-limit': z.number().step(1).min(0).default(30_000),
        'no-adapter': z.number().step(1).min(0).default(10 * 60_000),
        ambiguous: z.number().step(1).min(0).default(2 * 60_000),
    }),
    providerWideClasses: z.array(z.string()).default(['quota', 'credential']),
    providerFailureWindowMs: z.number().step(1).min(0).default(5 * 60_000),
    providerFailureThreshold: z.number().step(1).min(1).default(2),
    providerCooldownMs: z.number().step(1).min(0).default(5 * 60_000),
    excludeModels: z.array(z.string()).default([]),
    preferProviders: z.array(z.string()).default([]),
    preferModels: z.array(z.string()).default([]),
    ignoreCodes: z.array(z.string()).default([
        'CONTEXT_WINDOW_EXCEEDED', 'UNSUPPORTED_CONTENT', 'UNSUPPORTED_REASONING_EFFORT',
        'UNSUPPORTED_OPTION', 'ABORTED', 'INVARIANT',
    ]),
    ambiguousCodes: z.array(z.string()).default([
        'INVALID_REQUEST', 'INVALID_ARGS', 'EMPTY_RESPONSE', 'STREAM_CLOSED',
        'MALFORMED_RESPONSE', 'PI_AI_ERROR', 'UNKNOWN',
    ]),
    probeAmbiguous: z.boolean().default(true),
    enforceImageCapability: z.boolean().default(true),
});
/** 稳定失败码到类别的映射；未列出的码走模糊类（先探测、再切换）。 */
const CODE_CLASS = Object.freeze({
    QUOTA: 'quota',
    AUTH: 'credential',
    MISSING_CREDENTIAL: 'credential',
    INVALID_CREDENTIAL: 'credential',
    UNKNOWN_MODEL: 'vanished',
    NO_ADAPTER: 'no-adapter',
    TRANSPORT: 'transport',
    TIMEOUT: 'timeout',
    SERVER: 'server',
    RATE_LIMIT: 'rate-limit',
});
/**
 * 归类一次模型请求失败。只读稳定 code 与 HTTP 状态，不解析 message 文案。
 * @param code - `LlmFailure.code`（缺失时由调用方传 `UNKNOWN`）。
 * @param status - `LlmFailure.status`，无 HTTP 响应时为 undefined。
 * @param config - 配置，提供忽略/模糊码表。
 * @returns 失败类别；`ignore` 表示永不触发切换。
 */
export function classifyFailure(code, status, config) {
    if (config.ignoreCodes.includes(code))
        return 'ignore';
    const known = CODE_CLASS[code];
    if (known !== undefined)
        return known;
    if (config.ambiguousCodes.includes(code))
        return 'ambiguous';
    if (status !== undefined) {
        if (status === 402)
            return 'quota';
        if (status === 401 || status === 403)
            return 'credential';
        if (status === 404 || status === 410)
            return 'vanished';
        if (status === 429)
            return 'rate-limit';
        if (status >= 500)
            return 'server';
    }
    return 'ambiguous';
}
/** 该模型是否被配置排除。 */
function isExcluded(model, excludeModels) {
    return excludeModels.includes(`${model.provider}/${model.id}`) || excludeModels.includes(model.id);
}
/** 关键字在偏好表中的位次；未命中排到最后。 */
function preferenceRank(needle, preferences) {
    const index = preferences.findIndex((keyword) => needle.toLowerCase().includes(keyword.toLowerCase()));
    return index === -1 ? preferences.length : index;
}
/**
 * 从模型清单中挑选故障转移目标。排序依据依次为：provider 偏好 → 同 provider 缓存亲和
 * → 模型关键字偏好 → 连续失败次数少者 → 清单稳定序。被排除、冷却中、provider 整体降级、
 * 不满足图片能力约束的候选一律不选。
 * @param list - `ctx.llm.listModels()` 的全部模型。
 * @param options - 失败事实、能力需求与运行时查询函数。
 * @returns 选中的模型；无可用候选时 undefined（调用方应保持原状，让失败响亮上报）。
 */
export function rankCandidates(list, options) {
    const { config, failedProvider, failedModel, needImage } = options;
    const providerDegraded = failedProvider.length > 0 && options.isProviderCooling(failedProvider);
    const scored = [];
    for (let index = 0; index < list.length; index++) {
        const model = list[index];
        if (model === undefined)
            continue;
        if (model.provider === failedProvider && model.id === failedModel)
            continue;
        if (providerDegraded && model.provider === failedProvider)
            continue;
        if (isExcluded(model, config.excludeModels))
            continue;
        if (options.isCooling(model.provider, model.id))
            continue;
        if (options.isProviderCooling(model.provider))
            continue;
        if (needImage && config.enforceImageCapability
            && !(model.inputModalities?.includes('image') ?? false))
            continue;
        scored.push({
            model,
            key: [
                preferenceRank(model.provider, config.preferProviders),
                model.provider === failedProvider ? 0 : 1,
                preferenceRank(model.id, config.preferModels),
                options.failuresOf(model.provider, model.id),
                index,
            ],
        });
    }
    scored.sort((a, b) => {
        for (let i = 0; i < a.key.length; i++) {
            const left = a.key[i] ?? 0;
            const right = b.key[i] ?? 0;
            if (left !== right)
                return left - right;
        }
        return 0;
    });
    return scored[0]?.model;
}
/** 会话图片检测：与 dsh-model-router 的 agent/request 判定同语义。 */
function sessionHasImage(session) {
    const events = session?.events;
    if (!Array.isArray(events))
        return false;
    for (let index = events.length - 1; index >= 0; index--) {
        const event = events[index];
        if (event?.type === 'user/message' && contentHasImage((event.data?.content ?? [])))
            return true;
    }
    return false;
}
/** 截断诊断文本，避免工具输出与日志被超长 provider 文案撑爆。 */
function clip(text, max = 200) {
    return text.length > max ? `${text.slice(0, max)}…` : text;
}
export function apply(ctx, config) {
    const models = new Map();
    const providers = new Map();
    const attempts = new Map();
    const probed = new Set();
    const totals = { probes: 0, failovers: 0, rewrites: 0 };
    let catalog = [];
    let catalogAt = 0;
    const modelKey = (provider, model) => `${provider}/${model}`;
    /** 读取模型标记；冷却已过期即视为已恢复（惰性自愈，无需观测成功）并清零连续失败。 */
    function markOf(provider, model, now) {
        const mark = models.get(modelKey(provider, model));
        if (mark === undefined)
            return undefined;
        if (mark.cooldownUntil <= now) {
            mark.consecutiveFailures = 0;
            mark.cooldownUntil = 0;
        }
        return mark;
    }
    function isCooling(provider, model, now) {
        return (markOf(provider, model, now)?.cooldownUntil ?? 0) > now;
    }
    function isProviderCooling(provider, now) {
        const mark = providers.get(provider);
        return mark !== undefined && mark.cooldownUntil > now;
    }
    function failuresOf(provider, model, now) {
        return markOf(provider, model, now)?.consecutiveFailures ?? 0;
    }
    /** 标记模型不可用并记录冷却。 */
    function markUnhealthy(provider, model, failureClass, failure, now) {
        const key = modelKey(provider, model);
        const previous = models.get(key);
        const mark = previous ?? {
            consecutiveFailures: 0,
            cooldownUntil: 0,
            lastClass: failureClass,
            lastCode: '',
            lastMessage: '',
            lastAt: 0,
            failovers: 0,
        };
        mark.consecutiveFailures += 1;
        mark.cooldownUntil = now + config.cooldownMs[failureClass];
        mark.lastClass = failureClass;
        mark.lastCode = typeof failure.code === 'string' ? failure.code : 'UNKNOWN';
        mark.lastStatus = typeof failure.status === 'number' ? failure.status : undefined;
        mark.lastMessage = clip(typeof failure.message === 'string' ? failure.message : '');
        mark.lastAt = now;
        models.set(key, mark);
    }
    /** 记录 provider 级失败；账号级类别或窗口内多种模型失败即整体降级。 */
    function noteProviderFailure(provider, model, failureClass, now) {
        let mark = providers.get(provider);
        if (mark === undefined) {
            mark = { recentModels: new Map(), cooldownUntil: 0, lastClass: failureClass };
            providers.set(provider, mark);
        }
        mark.lastClass = failureClass;
        if (config.providerWideClasses.includes(failureClass)) {
            mark.cooldownUntil = now + config.providerCooldownMs;
            mark.recentModels.clear();
            return;
        }
        for (const [id, at] of mark.recentModels) {
            if (now - at > config.providerFailureWindowMs)
                mark.recentModels.delete(id);
        }
        mark.recentModels.set(model, now);
        if (mark.recentModels.size >= config.providerFailureThreshold) {
            mark.cooldownUntil = now + config.providerCooldownMs;
            mark.recentModels.clear();
        }
    }
    /** 有界清理：按 step 记账的预算表最多保留 512 条，探测集合最多 1024 条。 */
    function prune() {
        while (attempts.size > 512) {
            const oldest = attempts.keys().next().value;
            if (oldest === undefined)
                break;
            attempts.delete(oldest);
        }
        while (probed.size > 1024) {
            const oldest = probed.values().next().value;
            if (oldest === undefined)
                break;
            probed.delete(oldest);
        }
    }
    /** 模型清单（带 TTL 缓存）。 */
    async function loadCatalog(force = false) {
        const now = Date.now();
        if (!force && catalog.length > 0 && now - catalogAt < config.catalogTtlMs)
            return catalog;
        const entries = [];
        for (const provider of ctx.llm.listProviders()) {
            try {
                entries.push(...await ctx.llm.listModels(provider.id));
            }
            catch {
                // 只有「某个 provider 无法枚举」会被吞掉：其余 provider 的候选仍然可用，
                // 因此这里降级为跳过该 provider，而不是让整次故障转移失败。
            }
        }
        catalog = entries;
        catalogAt = now;
        return catalog;
    }
    /** 供失败路径与请求改写共享的候选选择。 */
    async function pick(failedProvider, failedModel, needImage, now) {
        const list = await loadCatalog();
        return rankCandidates(list, {
            failedProvider,
            failedModel,
            needImage,
            config,
            isCooling: (provider, model) => isCooling(provider, model, now),
            isProviderCooling: (provider) => isProviderCooling(provider, now),
            failuresOf: (provider, model) => failuresOf(provider, model, now),
        });
    }
    /** 当前请求配置里的模型（失败事件本身不带 model，从会话请求头取）。 */
    function currentModel(session) {
        try {
            const header = session
                ?.requestHeader?.();
            const model = header?.config?.model;
            return typeof model === 'string' ? model : '';
        }
        catch {
            // 只吞掉「会话实现不提供请求头」这一种情况：失败恢复绝不能因为读不到模型而抛出。
            return '';
        }
    }
    // ── 失败恢复：无人接管时跨模型切换 ─────────────────────────────────────────
    ctx.effect(() => ctx.on('agent/request-error', async (payload, next) => {
        // 每次调用都重读实时状态：await 期间用户可能已取消，绝不能把取消当成故障处理。
        const aborted = () => payload?.signal?.aborted === true;
        if (aborted())
            return next();
        const downstream = await next();
        if (aborted())
            return downstream;
        if (downstream?.kind === 'retry')
            return downstream;
        const failure = payload?.failure ?? {};
        const code = typeof failure.code === 'string' && failure.code.length > 0 ? failure.code : 'UNKNOWN';
        const status = typeof failure.status === 'number' ? failure.status : undefined;
        const failureClass = classifyFailure(code, status, config);
        if (failureClass === 'ignore')
            return downstream;
        const session = payload?.agent?.session;
        const provider = typeof payload?.provider === 'string' ? payload.provider : '';
        const model = currentModel(session);
        const now = Date.now();
        const stepKey = `${session?.id ?? 'unknown'}|${payload?.turn ?? 0}|${payload?.step ?? 0}`;
        const used = attempts.get(stepKey) ?? 0;
        if (used >= config.maxFailoversPerStep) {
            ctx.logger.warn(`dsh-model-failover: ${provider}/${model || '(unknown model)'} failed with ${code} after ${used} recovery attempt(s); budget exhausted, surfacing the failure`);
            return downstream;
        }
        const probeKey = `${stepKey}|${provider}/${model}`;
        if (failureClass === 'ambiguous' && config.probeAmbiguous && !probed.has(probeKey)) {
            probed.add(probeKey);
            attempts.set(stepKey, used + 1);
            totals.probes += 1;
            prune();
            ctx.logger.info(`dsh-model-failover: ${provider}/${model || '(unknown model)'} returned ${code} (${failureClass}); probing the same model once before switching`);
            return { kind: 'retry' };
        }
        markUnhealthy(provider, model, failureClass, failure, now);
        noteProviderFailure(provider, model, failureClass, now);
        const target = await pick(provider, model, sessionHasImage(session), now);
        if (target === undefined) {
            ctx.logger.warn(`dsh-model-failover: ${provider}/${model || '(unknown model)'} unavailable (${failureClass} ${code}) and no healthy candidate is reachable; surfacing the failure`);
            return downstream;
        }
        attempts.set(stepKey, used + 1);
        const mark = models.get(modelKey(provider, model));
        if (mark !== undefined)
            mark.failovers += 1;
        totals.failovers += 1;
        prune();
        ctx.logger.info(`dsh-model-failover: ${provider}/${model || '(unknown model)'} -> ${target.provider}/${target.id} after ${failureClass} ${code}`);
        return { kind: 'retry' };
    }), '@dsh-external/dsh-model-failover: agent/request-error failover');
    // ── 冷却路由纠正：步骤准备之前用官方 API 记录选择 ───────────────────────
    // 新版内核已弃用「在 agent/request 里改 provider/model」：prepareRequest 会用
    // 步骤准备阶段固定的 selection 严格校验，发现改模型就抛
    //   agent/request proposed "X" after step preparation fixed "Y";
    //   use agent.model.select() before step preparation
    // 因此改在 agent/pre-step 里调用 agent.model.select() 记录选择（下一步生效）。
    // agent/request-error 的 {kind:'retry'} 重试仍然合法（不改当前请求 config）。
    ctx.effect(() => ctx.on('agent/pre-step', async (payload, next) => {
        const decision = await next();
        try {
            const agent = payload?.agent;
            const selection = payload?.modelSelection;
            if (agent === undefined || selection === undefined)
                return decision;
            if (payload?.signal?.aborted === true)
                return decision;
            if (typeof agent.model?.select !== 'function')
                return decision;
            if (typeof selection.provider !== 'string' || typeof selection.model !== 'string')
                return decision;
            const now = Date.now();
            if (!isCooling(selection.provider, selection.model, now)
                && !isProviderCooling(selection.provider, now))
                return decision;
            const session = agent.session;
            const target = await pick(selection.provider, selection.model, sessionHasImage(session), now);
            if (target === undefined) {
                ctx.logger.warn(`dsh-model-failover: ${selection.provider}/${selection.model} is cooling down and no healthy candidate is reachable`);
                return decision;
            }
            totals.rewrites += 1;
            ctx.logger.info(`dsh-model-failover: rerouting ${selection.provider}/${selection.model} -> ${target.provider}/${target.id} (pre-step)`);
            agent.model.select({ provider: target.provider, model: target.id });
        }
        catch { /* 改道失败不能阻断步骤：保持内核默认选择 */ }
        return decision;
    }), '@dsh-external/dsh-model-failover: agent/pre-step reroute');
    // ── model_health：健康度与切换统计 ─────────────────────────────────────────
    ctx.effect(() => ctx.tools.register(defineTool({
        name: 'model_health',
        description: 'Report model-provider health: cooling-down or degraded routes, recent failure class, and failover counts.',
        parameters: {},
        output: {
            schema: {
                type: 'object',
                additionalProperties: false,
                properties: {
                    summary: { type: 'string', required: true },
                    models: {
                        type: 'array',
                        required: true,
                        items: {
                            type: 'object',
                            additionalProperties: false,
                            properties: {
                                route: { type: 'string', required: true },
                                state: { type: 'string', required: true },
                                cooldown_remaining_ms: { type: 'number', required: true },
                                consecutive_failures: { type: 'number', required: true },
                                last_class: { type: 'string', required: true },
                                last_code: { type: 'string', required: true },
                                failovers: { type: 'number', required: true },
                            },
                        },
                    },
                    providers: {
                        type: 'array',
                        required: true,
                        items: {
                            type: 'object',
                            additionalProperties: false,
                            properties: {
                                provider: { type: 'string', required: true },
                                state: { type: 'string', required: true },
                                cooldown_remaining_ms: { type: 'number', required: true },
                            },
                        },
                    },
                },
            },
            render: (_args, value) => {
                const lines = [value.summary];
                for (const provider of value.providers) {
                    if (provider.state !== 'ok')
                        lines.push(`- provider ${provider.provider}: ${provider.state} (${provider.cooldown_remaining_ms}ms left)`);
                }
                for (const model of value.models) {
                    lines.push(`- ${model.route}: ${model.state}, fails=${model.consecutive_failures}, last=${model.last_class}/${model.last_code}, failovers=${model.failovers}${model.cooldown_remaining_ms > 0 ? `, cooldown=${model.cooldown_remaining_ms}ms` : ''}`);
                }
                return [{ type: 'text', text: lines.join('\n') }];
            },
        },
        isConcurrencySafe: () => true,
        async execute() {
            const now = Date.now();
            const coolingModels = [...models.entries()]
                .map(([route, mark]) => ({
                route,
                state: mark.cooldownUntil > now ? 'cooling' : 'ok',
                cooldown_remaining_ms: Math.max(0, mark.cooldownUntil - now),
                consecutive_failures: mark.cooldownUntil > now ? mark.consecutiveFailures : 0,
                last_class: mark.lastClass,
                last_code: mark.lastCode,
                failovers: mark.failovers,
            }))
                .sort((a, b) => b.cooldown_remaining_ms - a.cooldown_remaining_ms || a.route.localeCompare(b.route));
            const providerRows = [...providers.entries()]
                .map(([provider, mark]) => ({
                provider,
                state: mark.cooldownUntil > now ? 'degraded' : 'ok',
                cooldown_remaining_ms: Math.max(0, mark.cooldownUntil - now),
            }))
                .sort((a, b) => b.cooldown_remaining_ms - a.cooldown_remaining_ms || a.provider.localeCompare(b.provider));
            const cooling = coolingModels.filter((entry) => entry.state === 'cooling').length;
            const degraded = providerRows.filter((entry) => entry.state === 'degraded').length;
            return {
                summary: `${coolingModels.length} tracked model(s), ${cooling} cooling; ${degraded} degraded provider(s); failovers=${totals.failovers}, probes=${totals.probes}, reroutes=${totals.rewrites}`,
                models: coolingModels,
                providers: providerRows,
            };
        },
    })), '@dsh-external/dsh-model-failover: model_health tool');
    // ── model_failover_reset：运维逃生口（充值后立即恢复某路由）────────────────
    ctx.effect(() => ctx.tools.register(defineTool({
        name: 'model_failover_reset',
        description: 'Clear failover health marks so cooled-down routes become selectable again. Target: all, provider:<id>, or <provider>/<model>.',
        parameters: {
            target: { type: 'string', required: true, description: 'all | provider:<id> | <provider>/<model>' },
        },
        output: {
            schema: { type: 'string' },
            render: (_args, value) => [{ type: 'text', text: String(value) }],
        },
        isConcurrencySafe: () => true,
        async execute(args) {
            const target = String(args.target ?? '').trim();
            if (target === 'all') {
                const cleared = models.size + providers.size;
                models.clear();
                providers.clear();
                attempts.clear();
                probed.clear();
                return `cleared ${cleared} health mark(s) and all step budgets`;
            }
            if (target.startsWith('provider:')) {
                const provider = target.slice('provider:'.length);
                let cleared = providers.delete(provider) ? 1 : 0;
                for (const route of [...models.keys()]) {
                    if (route.startsWith(`${provider}/`)) {
                        models.delete(route);
                        cleared += 1;
                    }
                }
                return cleared === 0 ? `no health mark for provider "${provider}"` : `cleared ${cleared} mark(s) for provider "${provider}"`;
            }
            if (models.delete(target)) {
                const provider = target.split('/')[0] ?? '';
                providers.delete(provider);
                return `cleared health mark for ${target}`;
            }
            return `no health mark for "${target}"`;
        },
    })), '@dsh-external/dsh-model-failover: model_failover_reset tool');
}
//# sourceMappingURL=index.js.map