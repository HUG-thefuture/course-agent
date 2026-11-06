import { defineTool } from '@deepseek-ai/dsh-tools';
import z from 'schemastery';
export const name = "@dsh-external/dsh-model-router";
export const inject = ['tools', 'llm'];
export const Config = z.object({
    visionPreference: z.array(z.string()).default(['vision', 'omni', '-vl', 'minicpm']),
    economyPreference: z.array(z.string()).default(['flash', 'nano', 'small', 'mini', 'turbo', 'lite', '8b']),
    catalogTtlMs: z.number().default(60000),
});
/** 递归判断 content 是否含图片块（含嵌套 tool-result），与 dsh-llm contentHasImage 同语义。 */
function hasImage(content) {
    if (!Array.isArray(content))
        return false;
    return content.some((block) => block?.type === 'image' || (block?.type === 'tool-result' && hasImage(block?.content)));
}
export function apply(ctx, config) {
    const policies = new Map();
    const lastRoute = new Map();
    let catalog = [];
    let catalogAt = 0;
    async function loadCatalog(force = false) {
        const now = Date.now();
        if (!force && catalog.length > 0 && now - catalogAt < config.catalogTtlMs)
            return catalog;
        const entries = [];
        for (const provider of ctx.llm.listProviders()) {
            try {
                for (const model of await ctx.llm.listModels(provider.id)) {
                    const declared = model.inputModalities?.includes('image') ?? false;
                    const heuristic = config.visionPreference.some((k) => `${model.provider}/${model.id} ${model.name ?? ''}`.toLowerCase().includes(k.toLowerCase()));
                    entries.push({ provider: provider.id, id: model.id, vision: declared || heuristic });
                }
            }
            catch { /* provider 不可枚举：跳过该 key */ }
        }
        catalog = entries;
        catalogAt = now;
        return catalog;
    }
    function pickVision(list) {
        for (const key of config.visionPreference) {
            const hit = list.find((m) => m.vision && `${m.provider}/${m.id}`.toLowerCase().includes(key.toLowerCase()));
            if (hit !== undefined)
                return hit;
        }
        return list.find((m) => m.vision);
    }
    function pickEconomy(list) {
        for (const key of config.economyPreference) {
            const hit = list.find((m) => !m.vision && `${m.provider}/${m.id}`.toLowerCase().includes(key.toLowerCase()));
            if (hit !== undefined)
                return hit;
        }
        return list.find((m) => !m.vision);
    }
    function sessionHasImage(session) {
        const events = session?.events ?? [];
        for (let i = events.length - 1; i >= 0; i--) {
            const event = events[i];
            if (event?.type === 'user/message' && hasImage(event?.data?.content))
                return true;
        }
        return false;
    }
    /**
     * 计算本会话此刻应该用的目标模型（不改当前请求，只返回选择）。
     * 返回 undefined 表示保持内核默认选择。
     */
    async function desiredModel(session, current) {
        const policy = policies.get(session.id) ?? { kind: 'auto' };
        if (policy.kind === 'fixed') {
            return { provider: policy.provider, model: policy.model,
                label: `fixed ${policy.provider}/${policy.model}` };
        }
        const list = await loadCatalog();
        if (list.length === 0)
            return undefined;
        if (policy.kind === 'economy') {
            const chosen = pickEconomy(list);
            if (chosen === undefined)
                return undefined;
            return { provider: chosen.provider, model: chosen.id,
                label: `economy ${chosen.provider}/${chosen.id}` };
        }
        // auto：仅在会话真的带图时才换模型，避免无谓的前缀缓存抖动。
        if (!sessionHasImage(session))
            return undefined;
        const cur = list.find((m) => m.provider === current.provider && m.id === current.model);
        if (cur?.vision === true)
            return undefined;
        const vision = pickVision(list);
        if (vision === undefined)
            return undefined;
        if (vision.provider === current.provider && vision.id === current.model)
            return undefined;
        return { provider: vision.provider, model: vision.id,
            label: `vision→ ${vision.provider}/${vision.id}` };
    }
    // ── agent/pre-step：步骤准备之前用官方 API 记录模型选择 ─────────────────
    // 新版内核已弃用「在 agent/request 里改 provider/model」：prepareRequest 会用
    // 步骤准备阶段固定的 selection 严格校验，发现改模型就抛
    //   agent/request proposed "X" after step preparation fixed "Y";
    //   use agent.model.select() before step preparation
    // 因此改在 agent/pre-step 里调用 agent.model.select() 记录选择（下一步生效），
    // agent/request 不再动模型（只保留采样等其它控制参数）。
    ctx.effect(() => ctx.on('agent/pre-step', async (payload, next) => {
        const decision = await next();
        try {
            const agent = payload?.agent;
            const selection = payload?.modelSelection;
            if (agent === undefined || selection === undefined)
                return decision;
            if (typeof agent.model?.select !== 'function')
                return decision;
            const session = agent.session;
            if (session === undefined)
                return decision;
            const target = await desiredModel(session, selection);
            if (target === undefined)
                return decision;
            if (target.provider === selection.provider && target.model === selection.model)
                return decision;
            lastRoute.set(session.id, target.label);
            agent.model.select({ provider: target.provider, model: target.model });
        }
        catch { /* 路由失败不能阻断步骤：保持内核默认选择 */ }
        return decision;
    }), '@dsh-external/dsh-model-router: agent/pre-step model select');
    // ── model_list ───────────────────────────────────────────────────────────
    ctx.effect(() => ctx.tools.register(defineTool({
        name: 'model_list',
        description: 'List every model reachable from all configured API keys, with vision capability. Call before model_route to choose a target.',
        parameters: {
            refresh: { type: 'boolean', description: 'Bypass the catalog cache.' },
        },
        output: {
            schema: {
                type: 'object', additionalProperties: false,
                properties: {
                    models: {
                        type: 'array', required: true,
                        items: {
                            type: 'object', additionalProperties: false,
                            properties: {
                                provider: { type: 'string', required: true },
                                model: { type: 'string', required: true },
                                vision: { type: 'boolean', required: true },
                            },
                        },
                    },
                    vision_available: { type: 'boolean', required: true },
                },
            },
            render: (_args, value) => {
                const lines = value.models.map((m) => `- ${m.provider}/${m.model}${m.vision ? ' [vision]' : ''}`);
                return [{ type: 'text', text: `${value.models.length} models across all API keys${value.vision_available ? '' : ' (no vision model found)'}:\n${lines.join('\n')}` }];
            },
        },
        isConcurrencySafe: () => true,
        async execute(args) {
            const list = await loadCatalog(Boolean(args.refresh));
            return {
                models: list.map((m) => ({ provider: m.provider, model: m.id, vision: m.vision })),
                vision_available: list.some((m) => m.vision),
            };
        },
    })), '@dsh-external/dsh-model-router: model_list tool');
    // ── model_route ──────────────────────────────────────────────────────────
    ctx.effect(() => ctx.tools.register(defineTool({
        name: 'model_route',
        description: 'Set this session\'s routing policy across all API keys: auto (vision-aware default), economy (cheapest text model), pin (explicit provider+model), reset (harness default). Applies on the next request.',
        parameters: {
            policy: { type: 'string', required: true, enum: ['auto', 'economy', 'pin', 'reset'], description: 'Routing policy.' },
            provider: { type: 'string', description: 'Provider id when policy=pin.' },
            model: { type: 'string', description: 'Model id when policy=pin.' },
        },
        output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: String(v) }] },
        async execute(args, exec) {
            const session = exec.agent?.session;
            if (session === undefined)
                return 'no agent session';
            const a = args;
            if (a.policy === 'reset') {
                policies.delete(session.id);
                return 'routing reset to the harness default (next request)';
            }
            if (a.policy === 'auto') {
                policies.set(session.id, { kind: 'auto' });
                return 'routing = auto (vision-aware image dispatch; next request)';
            }
            if (a.policy === 'economy') {
                policies.set(session.id, { kind: 'economy' });
                return 'routing = economy (cheapest available text model; next request)';
            }
            if (typeof a.provider !== 'string' || typeof a.model !== 'string')
                return 'policy=pin requires both provider and model';
            policies.set(session.id, { kind: 'fixed', provider: a.provider, model: a.model });
            return `routing pinned to ${a.provider}/${a.model} (next request)`;
        },
    })), '@dsh-external/dsh-model-router: model_route tool');
}
//# sourceMappingURL=index.js.map