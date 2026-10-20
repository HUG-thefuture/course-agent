"""课程分组共用逻辑：分组规则、内容聚类、整合复习生成、增量自动归组。

从 tools/merge_courses.py 抽出，供 CLI 与 webui 自动更新共用，保证两条
路径的分组语义一致：

- collect_courses / collect_groups：产物目录枚举（含 webui/ 子目录一层）；
- content_clusters：全量内容聚类（手动重建用）；
- place_new_courses：增量归组——新课程嵌入自己的知识块，与现有组逐成员算
  「双向 top-k 块相似度均值」（与贪心聚类入组判据相同），过阈值入最佳组；
  否则按课号/关键词种子规则归组或新建组。组成员的块向量按课程目录缓存到
  data/group_vecs/（signature 校验），新课程归组只需嵌入新课程自己；
- gen_integration：每组整合复习文档生成（大组分批）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .utils import truncate

# 编号段 -> 课程组（按用户课表的系列顺序）
SERIES = [
    (1, 13, "Web 前端基础：HTML 与 CSS"),
    (14, 17, "JavaScript 语言基础"),
    (18, 24, "Node/Express 后端与 Ajax"),
    (25, 39, "Python FastAPI 后端开发"),
    (40, 48, "爬虫与数据采集"),
    (49, 74, "数据分析与可视化（Pandas 栈）"),
    (75, 88, "Vue3 前端框架"),
]
DEFAULT_GROUP = "人工智能产训特训营"
KEYWORDS = [
    (("人工智能产训", "星星的个人会议室", "ACA考前集训"), DEFAULT_GROUP),
    (("vue", "pinia", "vue-router"), "Vue3 前端框架"),
    (("pandas", "numpy", "matplotlib", "seaborn", "数据分析"), "数据分析与可视化（Pandas 栈）"),
    (("selenium", "爬虫", "cookie", "xpath", "request"), "爬虫与数据采集"),
    (("fastapi", "sqlalchemy", "新闻app", "接口"), "Python FastAPI 后端开发"),
    (("express", "mysql", "node"), "Node/Express 后端与 Ajax"),
    (("html", "css", "flex", "盒模型"), "Web 前端基础：HTML 与 CSS"),
]

GROUPS_SUBDIR = "课程整合"
CONTENT_THRESHOLD = 0.68


# ------------------------------------------------------------------ 目录枚举
def course_number(title: str) -> int | None:
    """从标题提取课程编号（对应 SERIES 编号空间，非录制目录序号）。

    - "072-35-新闻APP…"：首段是录制序号，第二段才是课号 → 35；
    - "45、request…"：首段即课号 → 45；
    - "001-人工智能产训特训-…"：无第二编号段 → None（走关键词归类）。
    """
    s = title.strip()
    m = re.match(r"^\d{1,3}[-–](\d{1,3})(?:[、.．-]|$)", s)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d{1,3})[、.．]", s)
    if m:
        return int(m.group(1))
    return None


def group_of(title: str) -> str:
    num = course_number(title)
    if num is not None:
        for lo, hi, name in SERIES:
            if lo <= num <= hi:
                return name
    low = title.lower()
    for kws, name in KEYWORDS:
        if any(k in low for k in kws):
            return name
    return DEFAULT_GROUP


def group_dir_name(name: str) -> str:
    return re.sub(r"[^\w（）()-]+", "-", name)


def rel_id(out_root: Path, d: Path) -> str:
    return d.relative_to(out_root).as_posix()


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def course_title(d: Path) -> str:
    s = read_json(d / "session.json")
    return s.get("title", "") or d.name


def collect_courses(out_root: Path) -> list[Path]:
    """产物目录下的全部课程（output 顶层 + 一批子目录一层，与 webui 枚举一致）。"""
    out_root = Path(out_root)
    found: list[Path] = []
    if not out_root.is_dir():
        return found
    for d in sorted(out_root.iterdir()):
        if d.name == GROUPS_SUBDIR:
            continue
        if d.is_dir() and (d / "sections.json").is_file():
            found.append(d)
        elif d.is_dir():
            for sub in sorted(d.iterdir()):
                if (sub / "sections.json").is_file():
                    found.append(sub)
    return found


def passed_courses(out_root: Path) -> list[tuple[str, Path]]:
    """质检通过的课程 [(相对id, 目录)]——未通过的不入列表也不归组。"""
    out_root = Path(out_root)
    return [(rel_id(out_root, d), d) for d in collect_courses(out_root)
            if read_json(d / "session.json").get("passed") is True]


def group_dest(out_root: Path) -> Path:
    return Path(out_root) / GROUPS_SUBDIR


def _norm_member(out_root: Path, raw: str) -> str:
    """组.json 成员归一化为相对 out_root 的 id。

    历史格式存目录名（绝对课程顶层时与相对 id 相同）；webui 子目录课程
    的 id 带 `webui/` 前缀，按目录名反查补齐。
    """
    if (out_root / raw).is_dir():
        return raw
    # 旧格式：按目录名直接命中顶层目录
    d = out_root / raw
    if (d / "sections.json").is_file():
        return rel_id(out_root, d)
    # 一层子目录反查（如 webui/<name>）
    for sub in sorted(out_root.iterdir()):
        if sub.is_dir() and sub.name != GROUPS_SUBDIR:
            cand = sub / raw
            if (cand / "sections.json").is_file():
                return rel_id(out_root, cand)
    return raw


def load_groups(out_root: Path) -> dict[str, dict]:
    """读现有组：{组名: {"dir": Path, "ids": [成员相对id]}}（组.json 为准）。"""
    out_root = Path(out_root)
    groups: dict[str, dict] = {}
    dest = group_dest(out_root)
    if not dest.is_dir():
        return groups
    for d in sorted(dest.iterdir()):
        gp = d / "组.json"
        if not d.is_dir() or not gp.is_file():
            continue
        data = read_json(gp)
        name = data.get("name") or d.name
        groups[name] = {"dir": d, "ids": [_norm_member(out_root, c)
                                          for c in data.get("courses", [])]}
    return groups


def grouped_ids(out_root: Path) -> set[str]:
    ids: set[str] = set()
    for g in load_groups(out_root).values():
        ids.update(g["ids"])
    return ids


def collect_groups(out_root: Path) -> dict[str, list[Path]]:
    """扫描产物目录，按组聚合（目录名/标题双重判断）。"""
    groups: dict[str, list[tuple[Path, int]]] = {}
    for d in collect_courses(Path(out_root)):
        title = course_title(d)
        name = group_of(title or d.name)
        # 排序键：课程编号（优先第二编号段），无编号排最后按日期
        order = course_number(title or "")
        if order is None:
            m = re.search(r"(\d{1,3})", title or d.name)
            order = int(m.group(1)) if m else 9999
        groups.setdefault(name, []).append((d, order))
    for name in groups:
        groups[name].sort(key=lambda x: x[1])
    return {k: [d for d, _ in v] for k, v in groups.items()}


# ------------------------------------------------------------------ 内容聚类
def _course_blocks(d: Path, limit: int = 60) -> list[str]:
    """每门课的块级文本：逐知识点（标题+概念摘要），最多 limit 块。"""
    secs = read_json(d / "sections.json")
    if not isinstance(secs, list):
        return []
    out: list[str] = []
    for sec in secs:
        for kp in sec.get("knowledge_points", []):
            t = (kp.get("title", "") + " " +
                 (kp.get("concept") or kp.get("detail", "")))[:400]
            if t.strip():
                out.append(t)
            if len(out) >= limit:
                return out
    return out


def content_clusters(courses: list[Path], embedder, threshold: float = CONTENT_THRESHOLD,
                     top_k: int = 5) -> list[list[Path]]:
    """按课程内容的块级相似度贪心聚类（平均连接）。

    度量：每门课逐知识点嵌入为块向量集合；两课相似度 = 双向 top-k 块相似度
    均值（A 的每块取与 B 的最相似块、取最高的 k 个平均，B→A 同理再平均）。
    块级度量避免整课单向量把局部强相关信号平均掉。

    无知识块的课程不参与聚类（嵌入字面量会彼此高度相似而错误抱团），单独成组。
    """
    import numpy as np

    blocks = {d: _course_blocks(d) for d in courses}
    vecs: dict[Path, "np.ndarray"] = {}
    todo = [d for d in courses if blocks[d]]
    for d in todo:
        e = np.asarray(embedder.embed(blocks[d]), dtype=np.float32)
        e /= (np.linalg.norm(e, axis=1, keepdims=True) + 1e-9)
        vecs[d] = e
    print(f"  嵌入 {len(vecs)} 门课的知识块完成", flush=True)

    def sim(a: Path, b: Path) -> float:
        M = vecs[a] @ vecs[b].T
        k = min(top_k, M.shape[0], M.shape[1])
        top_a = np.sort(M.max(axis=1))[-k:].mean()
        top_b = np.sort(M.max(axis=0))[-k:].mean()
        return float((top_a + top_b) / 2)

    clusters: list[list[Path]] = []
    for d in courses:
        if d not in vecs:                # 无知识块：单独成组
            clusters.append([d])
            continue
        best, best_s = None, threshold
        for cl in clusters:
            members = [m for m in cl if m in vecs]
            if not members:
                continue
            s = sum(sim(d, m) for m in members) / len(members)
            if s > best_s:
                best, best_s = cl, s
        if best is not None:
            best.append(d)
        else:
            clusters.append([d])
    return clusters


def cluster_name(courses: list[Path], seed_groups: dict[str, list[Path]]) -> str:
    """组名：取组内多数课程所属的种子组名，否则用首个课程标题。"""
    from collections import Counter
    names = []
    for d in courses:
        for g, ds in seed_groups.items():
            if d in ds:
                names.append(g)
    if names:
        return Counter(names).most_common(1)[0][0]
    return course_title(courses[0])[:24] if courses else "课程组"


def group_context(courses: list[Path], per_course: int = 3) -> str:
    """每组上下文：每门课取前 N 个知识点标题+概念摘要。"""
    parts = []
    for d in courses:
        secs = read_json(d / "sections.json")
        if not isinstance(secs, list):
            continue
        title = course_title(d)
        lines = []
        for sec in secs:
            for kp in sec.get("knowledge_points", [])[:per_course]:
                concept = truncate(kp.get("concept") or kp.get("detail", ""), 90)
                lines.append(f"  - {kp.get('title', '')}：{concept}")
        parts.append(f"《{truncate(title, 40)}》\n" + "\n".join(lines[:12]))
    return "\n\n".join(parts)


# ------------------------------------------------------------------ 整合复习生成
# 分段生成：组内课程超过该数量就分批，每批单独生成再拼接。
# 单次调用输出上限（max_tokens）有限，大组（45 门）一次性写完必被截断——
# 实测 3 组曾断在自检清单中途，故按批次拆分。批大小同时受本地 16k 上下文约束。
_CHUNK_COURSES = 8
_CTX_CHARS = 12000

_SECTIONS_FULL = (
    "## 课程构成\n(列出组内课程及各自定位, 一句话)\n"
    "## 知识体系全景图\n(文字树状图, 跨课程组织)\n"
    "## 学习主线\n(3-6 步主线, 说明先后依赖)\n"
    "## 核心知识点速览\n(按主题分组列出要点, 标注来自哪门课)\n"
    "## 易混淆点对照表\n| 概念A | 概念B | 区别 | 记忆方法 |\n"
    "## 跨课程学习顺序建议\n(编号路径, 标注可并行项)\n"
    "## 综合自检清单\n(逐知识点检查项)\n"
)

_CHUNK_RULE = (
    "纪律：只使用【课程素材】中出现的内容；补充标注「（补充，非课程内容）」。\n"
    "**输出必须完整**：自检清单要写完最后一项，不要中途停止或省略。\n"
    "{part}\n\n"
)


def _gen_once(llm, group_name: str, ctx: str, part: str = "",
              max_tokens: int = 8192) -> str:
    hint = (f"本次只覆盖素材中的部分课程，标题写「{group_name} · 整合复习"
            f"（{part}）」。\n" if part else "")
    system = (
        "你是双擎学习助手的跨课程整合模块。以下是同一课程体系下的多门相关课程"
        "的知识点摘要。生成一份**整合复习文档**（Markdown），结构：\n"
        f"# {group_name} · 整合复习\n"
        + _SECTIONS_FULL
        + _CHUNK_RULE.format(part=hint)
        + f"【课程素材】\n{ctx}")
    return llm.chat(system, "请生成整合复习文档。", max_tokens=max_tokens)


def gen_integration(llm, group_name: str, courses: list[Path]) -> str:
    """生成整合复习文档；大组分批生成后拼接，避免单次输出被截断。"""
    if len(courses) <= _CHUNK_COURSES:
        return _gen_once(llm, group_name, truncate(group_context(courses), _CTX_CHARS))
    batches = [courses[i:i + _CHUNK_COURSES]
               for i in range(0, len(courses), _CHUNK_COURSES)]
    docs: list[str] = []
    for i, batch in enumerate(batches, 1):
        ctx = truncate(group_context(batch), _CTX_CHARS)
        docs.append(_gen_once(llm, group_name, ctx,
                              part=f"{i}/{len(batches)}", max_tokens=8192))
        print(f"  分段 {i}/{len(batches)} 完成（{len(batch)} 门课）", flush=True)
    # 拼接：首段保留大标题与「课程构成」，后续段各自成节
    merged = [docs[0].rstrip()]
    for d in docs[1:]:
        body = d.strip()
        if body.startswith("# "):                       # 去掉重复大标题
            body = body.split("\n", 1)[1].lstrip() if "\n" in body else ""
        merged.append("\n\n---\n\n" + body)
    full = "\n".join(merged) + "\n"
    # 分段提示只服务于生成过程，成品标题不带「（i/n）」
    lines = full.split("\n", 1)
    if lines[0].lstrip("# ").strip() != f"{group_name} · 整合复习":
        lines[0] = f"# {group_name} · 整合复习"
        full = "\n".join(lines)
    return full


def placeholder_doc(group_name: str, courses: list[tuple[str, str]]) -> str:
    """整合文档重生成失败/未跑时的占位（组目录有 整合复习.md 才会被 webui 识别）。"""
    lines = [f"# {group_name} · 整合复习", "",
             "> 占位文档：课程已归组，跨课程整合内容尚未生成。", "",
             "## 成员课程", ""]
    for cid, title in courses:
        lines.append(f"- {title}（`{cid}`）")
    return "\n".join(lines) + "\n"


def write_atomic(p: Path, text: str) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


# ------------------------------------------------------------------ 增量自动归组
def _sig(d: Path) -> str:
    """课程内容签名：sections.json 的 mtime+size（改了自动重新嵌入）。"""
    st = (d / "sections.json").stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def course_block_vecs(d: Path, cid: str, embedder, cache_dir: Path):
    """课程块向量（归一化 float32，npy 缓存）。无知识块返回 None。

    缓存失效按 sections.json 签名判断；返回 None 表示该课无可嵌入内容。
    """
    import numpy as np

    blocks = _course_blocks(d)
    if not blocks:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(cid.encode("utf-8")).hexdigest()[:16]
    npy = cache_dir / f"{key}.npy"
    meta = cache_dir / f"{key}.json"
    sig = _sig(d)
    if npy.is_file() and meta.is_file() and read_json(meta).get("sig") == sig:
        return np.load(npy)
    e = np.asarray(embedder.embed(blocks), dtype=np.float32)
    e /= (np.linalg.norm(e, axis=1, keepdims=True) + 1e-9)
    np.save(npy, e)
    write_atomic(meta, json.dumps({"sig": sig, "id": cid}))
    return e


def block_sim(a, b, top_k: int = 5) -> float:
    """双向 top-k 块相似度均值（与 content_clusters 的 sim 相同）。"""
    import numpy as np

    M = a @ b.T
    k = min(top_k, M.shape[0], M.shape[1])
    top_a = np.sort(M.max(axis=1))[-k:].mean()
    top_b = np.sort(M.max(axis=0))[-k:].mean()
    return float((top_a + top_b) / 2)


def place_new_courses(items: list[tuple[str, Path]], cfg, llm=None,
                      log=None, threshold: float = CONTENT_THRESHOLD) -> dict:
    """把新课程增量归入课程组并更新受影响组的整合复习文档。

    items: [(课程相对id, 课程目录)]；cfg 需有 output_dir / data_dir。
    判组优先级：块向量相似度≥threshold 的最佳组 → 课号/关键词种子组 → 新建组。
    嵌入服务不可用时退化为种子规则（结果照常落盘，embedding 恢复后可用
    tools/merge_courses.py --force 全量重建修正）。
    返回 {"assigned": [...], "groups": [...], "embed_error": str|None}。
    """
    from .kb_vector import OllamaEmbedder

    say = log or (lambda m: None)
    out_root = Path(cfg.output_dir)
    dest = group_dest(out_root)
    cache_dir = Path(cfg.data_dir) / "group_vecs"
    courses = {rel_id(out_root, d): d for d in collect_courses(out_root)}
    courses.update(dict(items))            # 新课程可能尚未被枚举到（防呆）
    groups = load_groups(out_root)

    emb = OllamaEmbedder()
    embed_ok = True
    embed_error = None
    vec_cache: dict[str, object] = {}

    def member_vec(cid: str):
        if cid not in vec_cache:
            d = courses.get(cid)
            vec_cache[cid] = course_block_vecs(d, cid, emb, cache_dir) if d else None
        return vec_cache[cid]

    assigned: list[dict] = []
    touched: list[str] = []
    for cid, cdir in items:
        title = course_title(cdir)
        group, method = None, ""
        if embed_ok:
            try:
                nv = course_block_vecs(cdir, cid, emb, cache_dir)
            except Exception as e:
                embed_ok = False
                embed_error = truncate(str(e), 160)
                say(f"嵌入服务不可用，改用标题规则归组：{embed_error}")
                nv = None
            if nv is not None and nv.size:
                best, best_s = None, threshold
                try:
                    for name, g in groups.items():
                        sims = []
                        for mid in g["ids"]:
                            if mid == cid:
                                continue
                            mv = member_vec(mid)
                            if mv is not None and mv.size:
                                sims.append(block_sim(nv, mv))
                        if sims:
                            s = sum(sims) / len(sims)
                            if s > best_s:
                                best, best_s = name, s
                except Exception as e:
                    embed_ok = False
                    embed_error = truncate(str(e), 160)
                    say(f"嵌入服务不可用，改用标题规则归组：{embed_error}")
                if best is not None:
                    group, method = best, "content"
        if group is None:
            group = group_of(title or cid)
            method = "seed" if group in groups else "new"
        if group not in groups:
            gdir = dest / group_dir_name(group)
            gdir.mkdir(parents=True, exist_ok=True)
            groups[group] = {"dir": gdir, "ids": []}
        g = groups[group]
        if cid not in g["ids"]:
            g["ids"].append(cid)
            write_atomic(g["dir"] / "组.json", json.dumps(
                {"name": group, "courses": g["ids"]}, ensure_ascii=False, indent=1))
        assigned.append({"course_id": cid, "title": title, "group": group, "method": method})
        say(f"「{truncate(title, 30)}」→ 课程组「{group}」（{method}）")
        if group not in touched:
            touched.append(group)

    # 受影响组：重新生成整合复习（LLM 不可用/失败则保证占位文档存在）
    for name in touched:
        g = groups[name]
        members = [(cid, course_title(courses[cid])) for cid in g["ids"] if cid in courses]
        dirs = [courses[cid] for cid, _ in members]
        md = g["dir"] / "整合复习.md"
        if llm is not None and dirs:
            try:
                say(f"正在重新生成「{name}」整合复习（{len(dirs)} 门课）…")
                text = gen_integration(llm, name, dirs)
                write_atomic(md, text)
                continue
            except Exception as e:
                say(f"「{name}」整合复习生成失败：{truncate(str(e), 120)}")
        if not md.is_file():
            write_atomic(md, placeholder_doc(name, members))

    return {"assigned": assigned, "groups": touched, "embed_error": embed_error}


def missing_passed_courses(cfg) -> list[tuple[str, Path]]:
    """质检通过但还没进任何课程组新课程（启动时自动补归组用）。"""
    out_root = Path(cfg.output_dir)
    in_groups = grouped_ids(out_root)
    return [(cid, d) for cid, d in passed_courses(out_root) if cid not in in_groups]
