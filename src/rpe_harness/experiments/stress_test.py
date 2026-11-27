"""性能压测——500 技能规模下的路由延迟 / 吞吐 / 内存。

程序化生成大规模模拟技能库与覆盖全 TaskType 的测试任务，
分别压测 L0 确定性路由与 L2 关系检索（打分排序）路径，
输出 JSON 压测报告。零外部依赖——仅用标准库。

运行: PYTHONPATH=src python3 -m rpe_harness.experiments.stress_test
"""
from __future__ import annotations
import json
import math
import platform
import random
import sys
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import resource
except ImportError:  # 非 Unix 平台
    resource = None  # type: ignore[assignment]

from ..capability import CapabilityRouter, SkillEntry
from ..mcsi import CausalState, CapabilityLevel, TaskType

# ---------------- 配置 ----------------

N_SKILLS = 500
N_TASKS = 200
ROUNDS = 3          # 计时轮数（每轮跑一遍任务集）
TOP_K = 5
SEED = 42

INPUT_FORMATS = [
    "md", "txt", "csv", "xlsx", "docx", "pdf",
    "json", "python", "html", "yaml", "png", "sql",
]
TAGS = [
    "read", "write", "search", "analyze", "deploy", "review",
    "translate", "summarize", "parse", "visualize", "monitor", "transform",
]
PRECONDITIONS = [
    "network", "filesystem", "auth", "gpu", "shell", "browser",
]


@dataclass
class LatencyStats:
    """一组延迟样本的统计。"""

    count: int = 0
    mean_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0

    @classmethod
    def from_samples(cls, samples: list) -> "LatencyStats":
        if not samples:
            return cls()
        ordered = sorted(samples)
        return cls(
            count=len(ordered),
            mean_ms=round(sum(ordered) / len(ordered), 4),
            p50_ms=round(_percentile(ordered, 50), 4),
            p95_ms=round(_percentile(ordered, 95), 4),
            p99_ms=round(_percentile(ordered, 99), 4),
            max_ms=round(ordered[-1], 4),
        )

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "mean_ms": self.mean_ms,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "max_ms": self.max_ms,
        }


def _percentile(ordered: list, pct: float) -> float:
    """最近秩百分位（输入必须已排序）。"""
    if not ordered:
        return 0.0
    idx = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
    return float(ordered[idx])


def _rss_mb() -> float:
    """进程常驻内存（macOS ru_maxrss 单位为字节，Linux 为 KB）。"""
    if resource is None:
        return 0.0
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return raw / (1024 * 1024)
    return raw / 1024


# ---------------- 模拟数据生成 ----------------

def generate_skills(n: int, rng: random.Random) -> list:
    """生成 n 个模拟 SkillEntry，task_types/input_formats/tags/priority 组合各异。"""
    skills = []
    task_types = [t for t in TaskType]
    for i in range(n):
        n_tt = rng.randint(1, 3)
        n_fmt = rng.randint(1, 2)   # 至少 1 个 → 不产生 "tt:*" 通配规则
        n_tags = rng.randint(1, 3)
        skills.append(SkillEntry(
            skill_id=f"skill-{i:04d}",
            name=f"模拟技能 {i:04d}",
            description=f"压测模拟技能 #{i}，覆盖 {n_tt} 类任务、{n_fmt} 种输入格式",
            task_types=rng.sample(task_types, n_tt),
            input_formats=rng.sample(INPUT_FORMATS, n_fmt),
            preconditions=rng.sample(PRECONDITIONS, rng.randint(0, 2)),
            tags=rng.sample(TAGS, n_tags),
            path=f"/mock/skills/skill-{i:04d}/SKILL.md",
            last_used=time.time() - rng.random() * 86400 * 60,
            use_count=rng.randint(0, 500),
            success_rate=round(rng.uniform(0.5, 1.0), 2),
            is_dormant=(rng.random() < 0.1),
            priority=rng.randint(1, 10),
        ))
    return skills


def generate_tasks(router: CapabilityRouter, n: int, rng: random.Random) -> list:
    """生成 n 个测试任务：覆盖全部 TaskType，L0 命中与 L2 检索各半。

    任务按实际路由级别验证后入选：L0 任务必须走 DETERMINISTIC，
    L2 任务必须走打分路径（CLASSIFICATION/RELATIONAL）。
    """
    task_types = [t for t in TaskType]
    l0_target = n // 2
    tasks: list = []
    l0_count = 0
    l2_count = 0
    attempt = 0
    while len(tasks) < n and attempt < n * 100:
        attempt += 1
        tt = task_types[len(tasks) % len(task_types)]  # 轮转保证全 TaskType 覆盖
        want_l0 = l0_count < l0_target and (len(tasks) % 2 == 0 or l2_count >= l0_target)

        if want_l0:
            # 用已注册的确定性规则构造精确命中
            keys = [k for k in router._deterministic_rules if k.startswith(tt.value + ":")]
            if not keys:
                continue
            key = rng.choice(keys)
            target = router.skills[router._deterministic_rules[key]]
            if target.is_dormant:
                continue  # 规则指向休眠技能会降级到 L2
            fmt = key.split(":", 1)[1]
            state = _make_state(tt, f"确定性任务 {len(tasks)}", fmt, None)
        else:
            # required_capability 使 routing_key 为三段式 → 必然错过确定性规则
            pool = [s for s in router.skills.values() if tt in s.task_types]
            if not pool and tt != TaskType.UNKNOWN:
                continue
            fmt = rng.choice(INPUT_FORMATS)
            cap = rng.choice(TAGS)
            state = _make_state(tt, f"关系检索任务 {len(tasks)}", fmt, cap)

        result = router.route(state, top_k=TOP_K)
        if want_l0 and result.level != CapabilityLevel.DETERMINISTIC:
            continue
        if not want_l0 and result.level not in (
            CapabilityLevel.CLASSIFICATION, CapabilityLevel.RELATIONAL
        ):
            continue

        tasks.append({
            "id": f"task-{len(tasks):03d}",
            "group": "l0" if want_l0 else "l2",
            "state": state,
        })
        if want_l0:
            l0_count += 1
        else:
            l2_count += 1
    return tasks


def _make_state(tt: TaskType, goal: str, fmt: Optional[str], cap: Optional[str]) -> CausalState:
    return CausalState(
        task_type=tt,
        goal=goal,
        constraints=["filesystem"],
        input_format=fmt,
        required_capability=cap,
    )


# ---------------- 压测执行 ----------------

def _run_phase(router: CapabilityRouter, tasks: list, rounds: int) -> tuple:
    """跑若干轮任务，返回（各级别延迟样本, 墙钟总秒数）。"""
    latencies = {"l0": [], "l2": []}
    start = time.perf_counter()
    for _ in range(rounds):
        for task in tasks:
            result = router.route(task["state"], top_k=TOP_K)
            latencies[task["group"]].append(result.latency_ms)
    elapsed = time.perf_counter() - start
    return latencies, elapsed


def run_stress_test(
    n_skills: int = N_SKILLS,
    n_tasks: int = N_TASKS,
    rounds: int = ROUNDS,
    seed: int = SEED,
) -> dict:
    """执行压测并返回 JSON 报告。"""
    rng = random.Random(seed)
    report: dict[str, Any] = {
        "experiment": "stress_test",
        "config": {
            "n_skills": n_skills,
            "n_tasks": n_tasks,
            "rounds": rounds,
            "seed": seed,
            "top_k": TOP_K,
            "python": platform.python_version(),
        },
    }

    # 1. 生成模拟技能库并建索引
    skills = generate_skills(n_skills, rng)
    router = CapabilityRouter()
    tracemalloc.start()
    reg_start = time.perf_counter()
    for skill in skills:
        router.register_skill(skill)
    registration_ms = (time.perf_counter() - reg_start) * 1000
    index_current, index_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    stats = router.get_stats()
    report["index"] = {
        **stats,
        "registration_ms": round(registration_ms, 3),
        "index_alloc_mb": round(index_current / (1024 * 1024), 3),
        "index_peak_alloc_mb": round(index_peak / (1024 * 1024), 3),
    }

    # 2. 生成测试任务（覆盖全 TaskType，L0/L2 各半，已按实际级别验证）
    tasks = generate_tasks(router, n_tasks, rng)
    covered = sorted({t["state"].task_type.value for t in tasks})
    l0_tasks = [t for t in tasks if t["group"] == "l0"]
    l2_tasks = [t for t in tasks if t["group"] == "l2"]
    report["task_coverage"] = {
        "generated": len(tasks),
        "task_types_covered": covered,
        "task_types_total": len([t for t in TaskType]),
        "l0_tasks": len(l0_tasks),
        "l2_tasks": len(l2_tasks),
    }

    # 3. 预热（顺便稳定 JIT/缓存效应，不计入统计）
    for task in tasks:
        router.route(task["state"],)

    # 4. 计时：整体 + L0 / L2 分组
    all_lat: dict = {"l0": [], "l2": []}
    lat, elapsed_all = _run_phase(router, tasks, rounds)
    all_lat["l0"].extend(lat["l0"])
    all_lat["l2"].extend(lat["l2"])

    lat, elapsed_l0 = _run_phase(router, l0_tasks, rounds)
    l0_samples = lat["l0"] + lat["l2"]
    lat, elapsed_l2 = _run_phase(router, l2_tasks, rounds)
    l2_samples = lat["l0"] + lat["l2"]

    total_calls = (len(tasks) + len(l0_tasks) + len(l2_tasks)) * rounds
    total_time = elapsed_all + elapsed_l0 + elapsed_l2
    report["latency_ms"] = {
        "overall": LatencyStats.from_samples(all_lat["l0"] + all_lat["l2"]).to_dict(),
        "l0_deterministic": LatencyStats.from_samples(l0_samples).to_dict(),
        "l2_relational": LatencyStats.from_samples(l2_samples).to_dict(),
    }
    report["throughput_tps"] = {
        "overall": round(total_calls / total_time, 1) if total_time else 0.0,
        "l0_deterministic": round(len(l0_tasks) * rounds / elapsed_l0, 1) if elapsed_l0 else 0.0,
        "l2_relational": round(len(l2_tasks) * rounds / elapsed_l2, 1) if elapsed_l2 else 0.0,
    }

    # 5. 内存：路由期 Python 分配峰值 + 进程 RSS
    tracemalloc.start()
    for task in tasks:
        router.route(task["state"])
    _, routing_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    l0_p95 = report["latency_ms"]["l0_deterministic"]["p95_ms"]
    l2_p95 = report["latency_ms"]["l2_relational"]["p95_ms"]
    report["memory"] = {
        "peak_rss_mb": round(_rss_mb(), 2),
        "index_alloc_mb": report["index"]["index_alloc_mb"],
        "routing_peak_alloc_mb": round(routing_peak / (1024 * 1024), 3),
    }
    report["comparison"] = {
        "l2_over_l0_p95_ratio": round(l2_p95 / l0_p95, 2) if l0_p95 else None,
        "l2_over_l0_throughput_ratio": round(
            report["throughput_tps"]["l0_deterministic"] / report["throughput_tps"]["l2_relational"], 2
        ) if report["throughput_tps"]["l2_relational"] else None,
    }
    return report


if __name__ == "__main__":
    out_path = Path(__file__).resolve().parents[3] / "data" / "stress_report.json"
    report = run_stress_test()
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if out_path.parent.exists():
        out_path.write_text(text)
        report["report_path"] = str(out_path)
        text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
