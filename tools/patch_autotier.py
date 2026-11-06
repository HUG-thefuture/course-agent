"""为 DSH 路由插件打「agent/request 模型选择」兼容性补丁（幂等，可重复运行）。

背景
----
新版 DSH 内核在 prepareRequest 里用步骤准备阶段固定的 selection 严格校验
`agent/request` 的返回值。插件若在该钩子改 provider/model，内核抛：

    agent/request proposed "deepseek-official/deepseek-flash" after step
    preparation fixed "workbuddy/hy4free"; use agent.model.select() before
    step preparation

修复策略
--------
- 源码仓库里的两个插件（dsh-model-router / dsh-model-failover）已直接改好：
  路由决策搬到 `agent/pre-step`，用官方 `agent.model.select()` 记录选择。
- 第三方 npm 包 `dsh-autotier` 位于受保护的 `.dsh/` 目录，本脚本在其
  `onRequest` 里加一个兼容性守卫：路由结果若改了模型，就改用官方 API 记录
  （下一步生效），本次请求保持 base 不改模型。

用法
----
    python tools/patch_autotier.py                # 预览（只检查，不写）
    python tools/patch_autotier.py --apply        # 应用补丁
    python tools/patch_autotier.py --apply --path <autotier 目录>

补丁是幂等的：已打过补丁会直接报告「已应用」，不会重复插入。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

DEFAULT_AUTOTIER = (Path.home() / ".dsh" / "profiles" / "desktop" /
                    "node_modules" / "dsh-autotier")

OLD = """\tasync onRequest(agent, turn, step, next) {
\t\tconst base = await next();
\t\ttry {
\t\t\treturn await this.routeRequest(agent, turn, step, base);
\t\t} catch (error) {
\t\t\tthis.ctx.logger.error("dsh-autotier: routing failed, using the session configuration: %o", error);
\t\t\treturn base;
\t\t}
\t}"""

NEW = """\tasync onRequest(agent, turn, step, next) {
\t\tconst base = await next();
\t\ttry {
\t\t\tconst routed = await this.routeRequest(agent, turn, step, base);
\t\t\t// 内核兼容性守卫：新版 DSH 在 prepareRequest 里用步骤准备阶段固定的
\t\t\t// selection 严格校验 agent/request 的返回值，改 provider/model 会抛
\t\t\t//   agent/request proposed "X" after step preparation fixed "Y";
\t\t\t//   use agent.model.select() before step preparation
\t\t\t// 路由结果改了模型时改用官方 API 记录选择（下一步生效），本次返回 base。
\t\t\tif (routed !== base && (routed.provider !== base.provider || routed.model !== base.model)) {
\t\t\t\tif (typeof agent.model?.select === "function") {
\t\t\t\t\tagent.model.select({ provider: routed.provider, model: routed.model,
\t\t\t\t\t\t...(routed.reasoningEffort !== undefined ? { reasoningEffort: routed.reasoningEffort } : {}) });
\t\t\t\t}
\t\t\t\treturn base;
\t\t\t}
\t\t\treturn routed;
\t\t} catch (error) {
\t\t\tthis.ctx.logger.error("dsh-autotier: routing failed, using the session configuration: %o", error);
\t\t\treturn base;
\t\t}
\t}"""

MARKER = "内核兼容性守卫"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="dsh-autotier 模型选择兼容性补丁")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    ap.add_argument("--path", default=str(DEFAULT_AUTOTIER), help="autotier 包目录")
    args = ap.parse_args(argv[1:])

    lib = Path(args.path) / "lib" / "index.js"
    if not lib.is_file():
        print(f"找不到 {lib}；用 --path 指定 autotier 包目录")
        return 1

    text = lib.read_text(encoding="utf-8")
    if MARKER in text:
        print("✓ 补丁已应用过（无需重复）")
        return 0

    if OLD not in text:
        print("✗ 未找到预期的 onRequest 代码段（版本可能不同，请人工核对）")
        print("  期望片段开头：async onRequest(agent, turn, step, next) {")
        return 2

    print(f"目标文件: {lib}")
    print("将把 agent/request 里改模型的结果转为 agent.model.select() + 返回 base")
    if not args.apply:
        print("\n预览模式。执行：python tools/patch_autotier.py --apply")
        return 0

    backup = lib.with_suffix(".js.pre-modelselect")
    shutil.copy2(lib, backup)
    lib.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"✓ 已应用补丁；备份: {backup}")
    print("  重启 DSH（或重载该插件）后生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
