"""课程整合：相关课程聚合为课程组，每组生成一份整合复习文档。

分组策略（默认：按内容相关性）：
内容聚类——每门课的知识点逐块嵌入（bge-m3），课程间相似度用「双向 top-k
块相似度均值」（抗整课平均掉的局部强信号），贪心平均连接聚类；组名取组内
多数课程所属的编号系列名（编号映射/关键词作为命名种子）。

--by-title 退回纯标题编号分组（不跑嵌入，快，但无编号课全归"特训营"）。

每组产出：output/课程整合/<组名>/整合复习.md + 组.json（成员清单，webui 读）
（体系全景图 + 依赖关系 + 核心主线 + 易混淆对照 + 跨课程学习顺序）

分组/聚类/整合生成的逻辑已抽到 course_agent/grouping.py（webui 新课程自动
归组与本片共用同一套规则）；本文件只做全量重建 CLI。

用法（后台运行）：python tools/merge_courses.py [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from course_agent.config import load_config  # noqa: E402
from course_agent.utils import truncate  # noqa: E402
from course_agent.llm.factory import build_client_chains  # noqa: E402
from course_agent.grouping import (  # noqa: E402,F401  re-export：兼容既有测试/调用
    SERIES, DEFAULT_GROUP, KEYWORDS, CONTENT_THRESHOLD,
    course_number, group_of, collect_courses, collect_groups,
    cluster_name, content_clusters, group_context, gen_integration,
    group_dir_name, load_groups, rel_id,
)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="课程整合：相关课程聚合生成整合复习")
    ap.add_argument("--output", default="output",
                    help="课程产物根目录（默认 output，含一层子目录如 webui/）")
    ap.add_argument("--dest", default="output/课程整合")
    ap.add_argument("--only", default="", help="只处理指定组名（调试用）")
    ap.add_argument("--by-title", action="store_true",
                    help="仅按标题编号分组（不用内容聚类）")
    ap.add_argument("--threshold", type=float, default=CONTENT_THRESHOLD,
                    help=f"内容聚类的块级相似度阈值（默认 {CONTENT_THRESHOLD}）")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已存在的整合复习.md（默认跳过）")
    args = ap.parse_args(argv)

    cfg = load_config()
    local_chain, cloud_chain = build_client_chains(cfg)
    from course_agent.llm import Router
    # 本地 GPU 优先：远程链故障窗（DNS/超时）会让每次调用白等数分钟，
    # 连续失败 1 次即跳过该模型；云端只做本地失败兜底（同 supervisor 策略）
    llm = Router(local_chain + cloud_chain, skip_threshold=1)

    all_courses = collect_courses(Path(args.output))
    seed = collect_groups(Path(args.output))
    if args.by_title:
        groups = seed
    else:
        from course_agent.kb_vector import OllamaEmbedder
        emb = OllamaEmbedder()
        print(f"内容聚类 {len(all_courses)} 门课程（阈值 {args.threshold}）…", flush=True)
        clusters = content_clusters(all_courses, emb, args.threshold)
        groups: dict[str, list[Path]] = {}
        for cl in clusters:
            if not cl:
                continue
            gname = cluster_name(cl, seed)
            groups.setdefault(gname, []).extend(cl)  # 同名组合并
        print(f"内容聚类得到 {len(groups)} 组："
              f"{[(k, len(v)) for k, v in groups.items()]}", flush=True)
    if args.only:
        groups = {k: v for k, v in groups.items() if k == args.only}

    for name, courses in groups.items():
        dest = Path(args.dest) / group_dir_name(name)
        dest.mkdir(parents=True, exist_ok=True)
        md = dest / "整合复习.md"
        if md.is_file() and md.stat().st_size > 500 and not args.force:
            print(f"[{name}] 已存在，跳过", flush=True)
            continue
        # 组内课程清单随文档落盘，webui 据此展示成员（存相对 id，含 webui/ 前缀）
        (dest / "组.json").write_text(json.dumps(
            {"name": name, "courses": [rel_id(Path(args.output), d) for d in courses]},
            ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            text = gen_integration(llm, name, courses)
            md.write_text(text, encoding="utf-8")
            print(f"[{name}] 完成（{len(courses)} 门课整合）-> {md}", flush=True)
        except Exception as e:
            print(f"[{name}] 失败: {truncate(str(e), 120)}", flush=True)
        time.sleep(1)
    print("\n课程整合全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
