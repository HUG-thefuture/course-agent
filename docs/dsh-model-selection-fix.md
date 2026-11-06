# DSH 模型选择冲突修复说明

## 问题

DSH 会话报出：

```
agent "session-77fc4aec-...": agent/request proposed
"deepseek-official/deepseek-flash" after step preparation fixed
"workbuddy/hy4free"; use agent.model.select() before step preparation
```

## 根因

新版 DSH 内核把「模型选择」的时机**前移**了：

1. `preStep()` 里调用 `agent.model.current()` 捕获 selection，并触发
   `agent/pre-step` 瀑布流（prompt 组装、工具解析都用这个 selection）；
2. `prepareRequest()` 用这个已固定的 selection 构造 `seedConfig`，然后触发
   `agent/request` 瀑布流；
3. 内核**严格校验** `agent/request` 的返回值：只要改了
   `provider` / `model` / `reasoningEffort`，就抛上面那个错误并拒绝该步骤。

内核文档原话：

> The deprecated `agent/request` hook remains available to existing listeners
> and may replace other controls such as sampling. Changing provider or model,
> or adding, changing, or clearing reasoning effort rejects the step before
> input is committed.

也就是说 `agent/request` **已弃用**于改模型；正确做法是在步骤准备**之前**
调用官方 `agent.model.select()` 记录选择（从下一步开始生效）。

而机器上的三个路由插件仍在 `agent/request` 里改模型：

| 插件 | 位置 | 行为 |
|---|---|---|
| `dsh-model-router` | `C:\UGit\test\dsh-routing-suite\plugins\` | economy/vision/pin 策略改写 provider/model |
| `dsh-model-failover` | 同上 | 冷却路由纠正时改 provider/model |
| `dsh-autotier` | `~\.dsh\profiles\desktop\node_modules\` | cheap/strong 分档切换模型 |

## 修复

### 1. 两个源码插件（已直接改好）

`dsh-model-router` 与 `dsh-model-failover` 的 `src/index.ts` 和运行时加载的
`lib/index.js` 都改为：

- **移除** `agent/request` 改模型的钩子；
- **新增** `agent/pre-step` 监听器：算出目标模型后调用
  `agent.model.select({ provider, model })` 记录选择（下一步生效）；
- 返回原 `decision`，不改变本步骤的 prompt / 工具集；
- 整个监听包在 `try/catch` 里，路由失败不阻断步骤。

`dsh-model-failover` 的 `agent/request-error` 返回 `{ kind: 'retry' }` 仍然合法
（它不改当前请求 config，只是要求内核重试），保持不变。

验证：
```
node --check 两个 lib/index.js        → 通过
注册的钩子: [ 'agent/pre-step' ]      → 不再有 agent/request
economy 策略 → model.select({provider:'p1', model:'m-flash'})  → 正确
```

### 2. 第三方 npm 包 dsh-autotier（补丁脚本）

位于受保护的 `.dsh/` 目录，用 `tools/patch_autotier.py` 打**幂等兼容守卫**：

```bash
python tools/patch_autotier.py              # 预览
python tools/patch_autotier.py --apply      # 应用（自动备份 .pre-modelselect）
```

守卫逻辑：`routeRequest` 结果若改了模型 → 调用
`agent.model.select()` 记录选择，**本次返回 `base`**（不改当前请求）。
这样既不触发内核错误，又保留分档意图（下一步生效）。

已应用并验证：`node --check` 通过、重复运行报告「已应用过」。

> `tier_status` 当前为 `delegated` 模式（配置主动退让），所以现在不报错；
> 但一旦切回 `auto` 就会复发。补丁保证两种模式都安全。

### 3. 生效方式

- 源码插件通过 junction 挂载（`~\.dsh\profiles\web\node_modules\@dsh-external\`
  → `C:\UGit\test\dsh-routing-suite\plugins\`），改 `lib/index.js` 后
  **重启 DSH 或重载插件**即生效。
- autotier 补丁同样需要重启 DSH。

## 注意

- 模型切换从「当步立即生效」变成「**下一步生效**」，这是内核新语义，
  不是 bug。所有插件都必须遵守。
- 若以后升级这些插件，需重新检查是否又回到 `agent/request` 改模型。
- `tools/patch_autotier.py` 在 npm 升级覆盖 `lib/index.js` 后需重新 `--apply`（脚本幂等，会重新识别未打补丁状态）。
