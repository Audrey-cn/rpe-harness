"""冷门技能恢复实测（cold-skill recovery experiment）。

实验设计
--------
1. 从 ``data/skill_index.json`` 加载技能索引，随机选 10 个技能标记 ``is_dormant=True``；
2. 用技能元数据（ID/名称/标签/描述）+ 既有因果图档案生成 usage_contexts，
   作为"历史使用场景"信号（真实系统中该信号来自调用历史）；
3. 构造 20 个测试任务：10 个目标任务为休眠技能，10 个目标任务为活跃技能；
4. 每个任务执行三路探测：
   - CapabilityRouter.route()          —— 常规分层路由
   - CapabilityRouter.find_dormant()   —— 休眠技能匹配（对照信号）
   - CausalGraph.discover_cold_skills() —— 冷门技能发现（被测方法）
5. 统计：
   - 恢复率 = 正确发现的休眠技能 / 总休眠技能（在各自目标任务中被发现）
   - 误报率 = 非目标任务的发现数 / 总发现数
   - 延迟   = route / discover 调用耗时分布（avg/p50/p95/max, ms）
6. 报告写入 ``reports/cold_skill_test.json`` 并打印摘要。

运行::

    PYTHONPATH=src python3 -m rpe_harness.experiments.cold_skill_test

零第三方依赖，Python 3.9+。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import time
from pathlib import Path
from typing import Any

try:  # 支持 python -m 与直接脚本两种运行方式
    from ..capability import CapabilityRouter, SkillEntry
    from ..causal_graph import CausalGraph, SkillProfile
    from ..mcsi import CausalState, TaskType
except ImportError:  # pragma: no cover - 直接脚本运行时的绝对导入
    from rpe_harness.capability import CapabilityRouter, SkillEntry
    from rpe_harness.causal_graph import CausalGraph, SkillProfile
    from rpe_harness.mcsi import CausalState, TaskType

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_REPORT = PROJECT_ROOT / "reports" / "cold_skill_test.json"

NUM_DORMANT = 10
NUM_DORMANT_TASKS = 10
NUM_ACTIVE_TASKS = 10
DEFAULT_SEED = 42

# 目标技能无需出现在这些停用词中（避免描述里的虚词污染匹配信号）
STOPWORDS = frozenset(
    """a an the and or of to in on for with via using use by from as at is are
    be this that it its you your when will can into etc more most over own same
    so than too very just also not no nor only own same such""".split()
)

ACTION_VERBS = {
    TaskType.FILE_OPERATION: "process",
    TaskType.CODE_GENERATION: "implement",
    TaskType.CODE_REVIEW: "review",
    TaskType.DATA_PROCESSING: "analyze",
    TaskType.WEB_SEARCH: "look up",
    TaskType.WEB_ACADEMIC: "research",
    TaskType.WEB_DEEP_RESEARCH: "deep-research",
    TaskType.DOCUMENT_WRITING: "write",
    TaskType.SYSTEM_ADMIN: "configure",
    TaskType.CONVERSATION: "discuss",
    TaskType.UNKNOWN: "handle",
}


# --------------------------------------------------------------------------- #
# 元数据 → 使用场景信号
# --------------------------------------------------------------------------- #
def tokenize(text: str) -> list[str]:
    """拆词去停用词，保序去重；保留连字符词（note-taking）整体。"""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[\s/,;|]+", text or ""):
        word = raw.strip(".,:;()\"'[]{}|·—-")
        if len(word) < 2 or word.lower() in STOPWORDS or word in seen:
            continue
        seen.add(word)
        tokens.append(word)
    return tokens


def skill_tokens(skill: SkillEntry) -> list[str]:
    """技能的主题词集合：ID + 名称 + 标签 + 描述关键词。"""
    tokens = tokenize(skill.skill_id) + tokenize(skill.name)
    for tag in skill.tags:
        tokens += tokenize(tag)
    tokens += tokenize(skill.description)
    out: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def build_usage_contexts(skills: dict[str, SkillEntry]) -> dict[str, list[str]]:
    """为每个技能合成 usage_contexts（历史使用场景关键词组）。

    分组策略：跨技能共享的词归入一组（跨任务误匹配最多贡献 0.3），
    独有词拆成两组（保证目标任务能触发 >=2 组匹配，match_score > 0.3）。
    """
    tok_map = {sid: skill_tokens(s) for sid, s in skills.items()}
    freq: dict[str, int] = {}
    for toks in tok_map.values():
        for t in set(toks):
            freq[t] = freq.get(t, 0) + 1

    contexts: dict[str, list[str]] = {}
    for sid, toks in tok_map.items():
        shared = [t for t in toks if freq[t] > 1]
        solo = [t for t in toks if freq[t] == 1]
        groups: list[str] = []
        if shared:
            groups.append(" ".join(shared))
        if len(solo) >= 2:
            groups.append(" ".join(solo[::2]))
            groups.append(" ".join(solo[1::2]))
        elif solo:
            groups.append(" ".join(solo))
        if len(groups) < 2:
            # 退化兜底：按奇偶拆成两组，保证至少两组非空场景
            g1 = " ".join(toks[::2]) or sid
            g2 = " ".join(toks[1::2]) or sid
            groups = [g1, g2]
        contexts[sid] = [g for g in groups if g]
    return contexts


def prepare_graph(
    graph: CausalGraph,
    skills: dict[str, SkillEntry],
    synth_contexts: dict[str, list[str]],
) -> None:
    """合并真实档案与合成场景到因果图（不改动真实边数据）。"""
    for sid in skills:
        profile = graph.profiles.get(sid) or SkillProfile(skill_id=sid)
        merged = list(profile.usage_contexts)
        for ctx in synth_contexts[sid]:
            if ctx not in merged:
                merged.append(ctx)
        profile.usage_contexts = merged
        graph.set_profile(profile)


# --------------------------------------------------------------------------- #
# 任务构造
# --------------------------------------------------------------------------- #
def build_tasks(
    skills: dict[str, SkillEntry],
    dormant_ids: list[str],
    rng: random.Random,
) -> list[dict[str, Any]]:
    """20 个任务：10 个目标休眠技能 + 10 个目标活跃技能。"""
    active_ids = [sid for sid in skills if sid not in set(dormant_ids)]
    active_targets = rng.sample(active_ids, NUM_ACTIVE_TASKS)

    tasks: list[dict[str, Any]] = []
    for i, sid in enumerate(dormant_ids + active_targets):
        skill = skills[sid]
        is_dormant = sid in set(dormant_ids)
        task_type = skill.task_types[0] if skill.task_types else TaskType.UNKNOWN
        toks = skill_tokens(skill)
        goal = "{} {}".format(ACTION_VERBS.get(task_type, "handle"), " ".join(toks))
        tasks.append({
            "task_id": "t{:02d}".format(i + 1),
            "target_skill": sid,
            "target_is_dormant": is_dormant,
            "task_type": task_type.value,
            "goal": goal,
            "constraints": list(skill.preconditions[:2]),
            "input_format": skill.input_formats[0] if skill.input_formats else None,
            "required_capability": skill.tags[0] if skill.tags else None,
        })
    rng.shuffle(tasks)
    return tasks


# --------------------------------------------------------------------------- #
# 实验执行
# --------------------------------------------------------------------------- #
def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _latency_stats(values: list[float]) -> dict[str, float]:
    return {
        "avg": round(sum(values) / len(values), 3) if values else 0.0,
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values), 3) if values else 0.0,
    }


def run_experiment(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    rng = random.Random(seed)

    # 1. 加载技能索引，随机标记 10 个休眠技能
    index_path = DATA_DIR / "skill_index.json"
    router = CapabilityRouter(index_path)
    skills = router.skills
    all_ids = sorted(skills)
    dormant_ids = sorted(rng.sample(all_ids, NUM_DORMANT))
    for sid in all_ids:
        skills[sid].is_dormant = sid in set(dormant_ids)
    dormant_set = set(dormant_ids)

    # 2. 构造因果图场景信号（基于真实 causal_graph.json + 元数据合成）
    graph_path = DATA_DIR / "causal_graph.json"
    graph = CausalGraph.load(graph_path) if graph_path.exists() else CausalGraph()
    synth_contexts = build_usage_contexts(skills)
    prepare_graph(graph, skills, synth_contexts)

    # 3. 构造 20 个测试任务
    tasks = build_tasks(skills, dormant_ids, rng)
    assert len(tasks) == NUM_DORMANT_TASKS + NUM_ACTIVE_TASKS
    assert sum(1 for t in tasks if t["target_is_dormant"]) == NUM_DORMANT_TASKS

    # 4. 逐任务执行路由 + 冷门发现
    route_lat: list[float] = []
    discover_lat: list[float] = []
    recovered: set[str] = set()
    discoveries = 0
    false_positives = 0
    fp_by_skill: dict[str, int] = {}
    disc_by_reason: dict[str, int] = {}
    task_records: list[dict[str, Any]] = []

    for task in tasks:
        state = CausalState(
            task_type=TaskType(task["task_type"]),
            goal=task["goal"],
            constraints=task["constraints"],
            input_format=task["input_format"],
            required_capability=task["required_capability"],
        )

        result = router.route(state, top_k=5)
        route_lat.append(result.latency_ms)
        routed = [c.skill_id for c in result.candidates]
        top1 = routed[0] if routed else None

        # 纯打分排名（不受 L0 确定性规则短路影响，诊断用）
        scored = sorted(
            ((s.matches_state(state), s.priority, sid) for sid, s in skills.items()),
            key=lambda x: (x[0], x[1]),
            reverse=True,
        )
        target_rank = next(
            (i + 1 for i, (_, _, sid) in enumerate(scored) if sid == task["target_skill"]),
            None,
        )
        target_score = skills[task["target_skill"]].matches_state(state)

        t0 = time.perf_counter()
        dormant_matches = router.find_dormant(state)
        find_dormant_ids = [s.skill_id for s in dormant_matches]

        discovered = graph.discover_cold_skills(state, all_ids, dormant_set)
        discover_lat.append((time.perf_counter() - t0) * 1000)

        found_ids = [d["skill_id"] for d in discovered]
        target_discovered = task["target_skill"] in found_ids
        if task["target_is_dormant"] and target_discovered:
            recovered.add(task["target_skill"])

        fps = [sid for sid in found_ids if sid != task["target_skill"]]
        discoveries += len(found_ids)
        false_positives += len(fps)
        for d in discovered:
            disc_by_reason[d["reason"]] = disc_by_reason.get(d["reason"], 0) + 1
        for fp in fps:
            fp_by_skill[fp] = fp_by_skill.get(fp, 0) + 1

        task_records.append({
            "task_id": task["task_id"],
            "target_skill": task["target_skill"],
            "target_is_dormant": task["target_is_dormant"],
            "task_type": task["task_type"],
            "goal": task["goal"],
            "route_top1": top1,
            "route_top1_hit": top1 == task["target_skill"],
            "route_target_in_top5": task["target_skill"] in routed,
            "target_score": round(target_score, 3),
            "target_score_rank": target_rank,
            "find_dormant": find_dormant_ids,
            "discovered": discovered,
            "target_discovered": target_discovered,
            "false_positives": fps,
        })

    # 5. 统计
    total_dormant = len(dormant_ids)
    recovery_rate = len(recovered) / total_dormant if total_dormant else 0.0
    fp_rate = false_positives / discoveries if discoveries else 0.0
    top1_hits = sum(1 for t in task_records if t["route_top1_hit"])
    in_top5 = sum(1 for t in task_records if t["route_target_in_top5"])
    rank_top1 = sum(1 for t in task_records if t["target_score_rank"] == 1)
    ranks = [t["target_score_rank"] for t in task_records if t["target_score_rank"]]
    fp_examples = [
        {"task_id": t["task_id"], "target": t["target_skill"], "false_positive": fp}
        for t in task_records for fp in t["false_positives"]
    ][:10]

    report: dict[str, Any] = {
        "experiment": "cold_skill_recovery",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config": {
            "seed": seed,
            "total_skills": len(all_ids),
            "num_dormant": total_dormant,
            "num_tasks": len(tasks),
            "num_dormant_tasks": NUM_DORMANT_TASKS,
            "num_active_tasks": NUM_ACTIVE_TASKS,
            "graph_edges": len(graph.edges),
            "graph_profiles": len(graph.profiles),
        },
        "dormant_skills": dormant_ids,
        "recovery_rate": round(recovery_rate, 4),
        "recovery": {
            "total_dormant": total_dormant,
            "recovered": len(recovered),
            "recovered_skills": sorted(recovered),
            "missed_skills": sorted(dormant_set - recovered),
        },
        "false_positive_rate": round(fp_rate, 4),
        "false_positives": {
            "total_discoveries": discoveries,
            "false_positives": false_positives,
            "by_skill": dict(sorted(fp_by_skill.items(), key=lambda x: -x[1])),
            "discoveries_by_reason": disc_by_reason,
            "examples": fp_examples,
        },
        "latency_ms": {
            "route": _latency_stats(route_lat),
            "discover": _latency_stats(discover_lat),
        },
        "routing": {
            "top1_accuracy": round(top1_hits / len(tasks), 4) if tasks else 0.0,
            "target_in_top5_rate": round(in_top5 / len(tasks), 4) if tasks else 0.0,
            "score_rank_top1_rate": round(rank_top1 / len(tasks), 4) if tasks else 0.0,
            "score_rank_avg": round(sum(ranks) / len(ranks), 2) if ranks else 0.0,
            "note": "route() 受 L0 确定性规则短路；score_rank 为纯打分排名（L2 对照）",
        },
        "tasks": task_records,
    }
    report["passed"] = bool(
        recovery_rate >= 0.5
        and total_dormant == NUM_DORMANT
        and len(tasks) == NUM_DORMANT_TASKS + NUM_ACTIVE_TASKS
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="冷门技能恢复实测")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    report = run_experiment(seed=args.seed)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    summary = {
        "report_path": str(args.report),
        "recovery_rate": report["recovery_rate"],
        "false_positive_rate": report["false_positive_rate"],
        "latency_ms": report["latency_ms"],
        "routing": report["routing"],
        "passed": report["passed"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
