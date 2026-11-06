/**
 * @dsh-external/dsh-model-router — 跨全部 API key 的可用模型智能调度。
 *  - `model_list`：枚举所有已注册 provider 的模型与识图能力。
 *  - `model_route`：设定本会话路由策略（auto / economy / pin / reset）。
 *  - `agent/pre-step`：在步骤准备**之前**用 `agent.model.select()` 记录目标模型，
 *    从下一步开始生效（内核要求模型必须在 prepareRequest 前固定）。
 *
 * 兼容性说明（重要）：新版 DSH 内核已弃用「在 agent/request 里改 provider/model」。
 * 内核在 prepareRequest 里会用步骤准备阶段固定的 selection 严格校验
 * agent/request 的返回值，发现改模型就抛：
 *   agent/request proposed "X" after step preparation fixed "Y";
 *   use agent.model.select() before step preparation
 * 因此本插件改在 agent/pre-step 里调用官方 API 记录选择，agent/request 不再动模型。
 * 规范：监听与注册挂 ctx.effect / ctx.on（fiber dispose 自动清理）。
 */
import type { Context } from 'cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import z from 'schemastery'

export const name = "@dsh-external/dsh-model-router"
export const inject = ['tools', 'llm']

export interface Config {
  /** 识图模型 id 关键字（小写包含匹配，按顺序尝试）。 */
  visionPreference: string[]
  /** economy 策略下优先的廉价快模型关键字。 */
  economyPreference: string[]
  /** 模型清单缓存时长（毫秒）。 */
  catalogTtlMs: number
}

export const Config = z.object({
  visionPreference: z.array(z.string()).default(['vision', 'omni', '-vl', 'minicpm']),
  economyPreference: z.array(z.string()).default(['flash', 'nano', 'small', 'mini', 'turbo', 'lite', '8b']),
  catalogTtlMs: z.number().default(60000),
})

interface ModelEntry {
  provider: string
  id: string
  vision: boolean
}

type Policy =
  | { kind: 'auto' }
  | { kind: 'economy' }
  | { kind: 'fixed'; provider: string; model: string }

/** 递归判断 content 是否含图片块（含嵌套 tool-result），与 dsh-llm contentHasImage 同语义。 */
function hasImage(content: unknown): boolean {
  if (!Array.isArray(content)) return false
  return content.some((block: any) =>
    block?.type === 'image' || (block?.type === 'tool-result' && hasImage(block?.content)))
}

export function apply(ctx: Context, config: Config): void {
  const policies = new Map<string, Policy>()
  const lastRoute = new Map<string, string>()
  let catalog: ModelEntry[] = []
  let catalogAt = 0

  async function loadCatalog(force = false): Promise<ModelEntry[]> {
    const now = Date.now()
    if (!force && catalog.length > 0 && now - catalogAt < config.catalogTtlMs) return catalog
    const entries: ModelEntry[] = []
    for (const provider of ctx.llm.listProviders()) {
      try {
        for (const model of await ctx.llm.listModels(provider.id)) {
          const declared = model.inputModalities?.includes('image') ?? false
          const heuristic = config.visionPreference.some((k) =>
            `${model.provider}/${model.id} ${model.name ?? ''}`.toLowerCase().includes(k.toLowerCase()))
          entries.push({ provider: provider.id, id: model.id, vision: declared || heuristic })
        }
      } catch { /* provider 不可枚举：跳过该 key */ }
    }
    catalog = entries
    catalogAt = now
    return catalog
  }

  function pickVision(list: ModelEntry[]): ModelEntry | undefined {
    for (const key of config.visionPreference) {
      const hit = list.find((m) => m.vision && `${m.provider}/${m.id}`.toLowerCase().includes(key.toLowerCase()))
      if (hit !== undefined) return hit
    }
    return list.find((m) => m.vision)
  }

  function pickEconomy(list: ModelEntry[]): ModelEntry | undefined {
    for (const key of config.economyPreference) {
      const hit = list.find((m) => !m.vision && `${m.provider}/${m.id}`.toLowerCase().includes(key.toLowerCase()))
      if (hit !== undefined) return hit
    }
    return list.find((m) => !m.vision)
  }

  function sessionHasImage(session: any): boolean {
    const events: any[] = session?.events ?? []
    for (let i = events.length - 1; i >= 0; i--) {
      const event = events[i]
      if (event?.type === 'user/message' && hasImage(event?.data?.content)) return true
    }
    return false
  }

  /**
   * 计算本会话此刻应该用的目标模型（不改当前请求，只返回选择）。
   * 返回 undefined 表示保持内核默认选择。
   */
  async function desiredModel(session: any, current: { provider: string; model: string }):
    Promise<{ provider: string; model: string; label: string } | undefined> {
    const policy: Policy = policies.get(session.id) ?? { kind: 'auto' }
    if (policy.kind === 'fixed') {
      return { provider: policy.provider, model: policy.model,
               label: `fixed ${policy.provider}/${policy.model}` }
    }
    const list = await loadCatalog()
    if (list.length === 0) return undefined
    if (policy.kind === 'economy') {
      const chosen = pickEconomy(list)
      if (chosen === undefined) return undefined
      return { provider: chosen.provider, model: chosen.id,
               label: `economy ${chosen.provider}/${chosen.id}` }
    }
    // auto：仅在会话真的带图时才换模型，避免无谓的前缀缓存抖动。
    if (!sessionHasImage(session)) return undefined
    const cur = list.find((m) => m.provider === current.provider && m.id === current.model)
    if (cur?.vision === true) return undefined
    const vision = pickVision(list)
    if (vision === undefined) return undefined
    if (vision.provider === current.provider && vision.id === current.model) return undefined
    return { provider: vision.provider, model: vision.id,
             label: `vision→ ${vision.provider}/${vision.id}` }
  }

  // ── agent/pre-step：步骤准备之前用官方 API 记录模型选择 ─────────────────
  // 内核在 prepareRequest 之前捕获 selection 并严格校验，模型必须在这里固定。
  // select() 记录的是「pending」选择，从**下一个步骤**开始生效（符合内核语义）。
  ctx.effect(() => ctx.on('agent/pre-step', async (payload: any, next: () => Promise<any>) => {
    const decision = await next()
    try {
      const agent = payload?.agent
      const selection = payload?.modelSelection
      if (agent === undefined || selection === undefined) return decision
      if (typeof agent.model?.select !== 'function') return decision
      const session = agent.session
      if (session === undefined) return decision
      const target = await desiredModel(session, selection)
      if (target === undefined) return decision
      if (target.provider === selection.provider && target.model === selection.model) {
        return decision
      }
      lastRoute.set(session.id, target.label)
      agent.model.select({ provider: target.provider, model: target.model })
    } catch { /* 路由失败不能阻断步骤：保持内核默认选择 */ }
    return decision
  }), '@dsh-external/dsh-model-router: agent/pre-step model select')
  // 注意：不再监听 agent/request 改模型。该钩子只用于采样等其它控制参数，
  // 改 provider/model 会被内核拒绝并抛错。

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
      render: (_args, value: any) => {
        const lines = value.models.map((m: any) => `- ${m.provider}/${m.model}${m.vision ? ' [vision]' : ''}`)
        return [{ type: 'text', text: `${value.models.length} models across all API keys${value.vision_available ? '' : ' (no vision model found)'}:\n${lines.join('\n')}` }]
      },
    },
    isConcurrencySafe: () => true,
    async execute(args) {
      const list = await loadCatalog(Boolean((args as any).refresh))
      return {
        models: list.map((m) => ({ provider: m.provider, model: m.id, vision: m.vision })),
        vision_available: list.some((m) => m.vision),
      }
    },
  })), '@dsh-external/dsh-model-router: model_list tool')

  // ── model_route ──────────────────────────────────────────────────────────
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'model_route',
    description: 'Set this session\'s routing policy across all API keys: auto (vision-aware default), economy (cheapest text model), pin (explicit provider+model), reset (harness default). Applies on the next request.',
    parameters: {
      policy: { type: 'string', required: true, enum: ['auto', 'economy', 'pin', 'reset'], description: 'Routing policy.' },
      provider: { type: 'string', description: 'Provider id when policy=pin.' },
      model: { type: 'string', description: 'Model id when policy=pin.' },
    },
    output: { schema: { type: 'string' }, render: (_a, v: any) => [{ type: 'text', text: String(v) }] },
    async execute(args, exec) {
      const session = exec.agent?.session
      if (session === undefined) return 'no agent session'
      const a = args as any
      if (a.policy === 'reset') {
        policies.delete(session.id)
        return 'routing reset to the harness default (next request)'
      }
      if (a.policy === 'auto') {
        policies.set(session.id, { kind: 'auto' })
        return 'routing = auto (vision-aware image dispatch; next request)'
      }
      if (a.policy === 'economy') {
        policies.set(session.id, { kind: 'economy' })
        return 'routing = economy (cheapest available text model; next request)'
      }
      if (typeof a.provider !== 'string' || typeof a.model !== 'string') return 'policy=pin requires both provider and model'
      policies.set(session.id, { kind: 'fixed', provider: a.provider, model: a.model })
      return `routing pinned to ${a.provider}/${a.model} (next request)`
    },
  })), '@dsh-external/dsh-model-router: model_route tool')
}
