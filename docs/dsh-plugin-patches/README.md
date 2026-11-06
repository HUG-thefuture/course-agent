# DSH 路由插件修复补丁包

## 用途

修复 DSH 内核报错：

```
agent/request proposed "deepseek-official/deepseek-flash" after step
preparation fixed "workbuddy/hy4free"; use agent.model.select() before
step preparation
```

根因与完整分析见 [dsh-model-selection-fix.md](../dsh-model-selection-fix.md)。

## 为什么在这里

插件源码在 `C:\UGit\test\dsh-routing-suite\plugins\`，其远端是
`yjh051108/dsh-routing-suite`（**第三方仓库，当前账号 `HUG-thefuture` 无 push 权限，
推送返回 403**）。本地 commit `88fd8c2` 已生成但无法推送。

为保证修复**不丢失、可分发、可重建**，把补丁导出到本仓库。

## 内容

| 文件 | 说明 |
|---|---|
| `001-plugin-model-selection.patch` | 完整的 git format-patch（含全部插件源码与编译产物） |
| `dsh-model-router/src/index.ts` + `lib/index.js` | 修复后的路由插件（可直接替换） |
| `dsh-model-failover/src/index.ts` + `lib/index.js` | 修复后的故障转移插件（可直接替换） |

> `lib/index.js` 是 DSH 运行时 junction 实际加载的文件，必须一起替换；
> 只改 `src/` 不会生效。

## 应用方式

### 方式 A：直接替换（最快）

把两个插件目录下的 `src/index.ts` 与 `lib/index.js` 覆盖到：

```
C:\UGit\test\dsh-routing-suite\plugins\dsh-model-router\
C:\UGit\test\dsh-routing-suite\plugins\dsh-model-failover\
```

运行时若通过 junction 挂载（`~\.dsh\profiles\web\node_modules\@dsh-external\`
指向上述目录），改完即生效，**重启 DSH 或重载插件**。

### 方式 B：应用 git 补丁

```bash
cd C:\UGit\test\dsh-routing-suite
git am docs/dsh-plugin-patches/001-plugin-model-selection.patch
# 或只应用改动，不生成 commit：
git apply docs/dsh-plugin-patches/001-plugin-model-selection.patch
```

### 方式 C：推到自己的 fork（可选）

若有该仓库的 fork / 有权限的远端：

```bash
cd C:\UGit\test\dsh-routing-suite
git remote add mine https://github.com/<你的账号>/dsh-routing-suite.git
git push mine 88fd8c2:main
```

## 第三方 npm 包 dsh-autotier

不在本补丁包内（位于受保护的 `~\.dsh\` 目录），用本仓库的
`tools/patch_autotier.py` 打幂等兼容守卫：

```bash
python tools/patch_autotier.py --apply
```

## 验证

- `node --check <插件>/lib/index.js` → 语法通过
- 行为测试：只注册 `agent/pre-step`，economy 策略调用
  `agent.model.select({provider, model})`，不再注册 `agent/request`
- 模型切换由「当步生效」变为「下一步生效」，这是内核新语义
