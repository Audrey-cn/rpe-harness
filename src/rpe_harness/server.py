"""RPE-Harness HTTP API — 独立能力寻址服务。

零外部依赖（Python stdlib http.server）。
任何 Agent 宿主可通过 HTTP 调用路由能力。

端点：
  GET  /health              健康检查
  GET  /stats               路由器 + 因果图统计
  POST /route               路由请求 → 技能推荐
  GET  /skills              技能列表
  GET  /skills/<id>         技能详情
  POST /causal/discover     冷门技能发现
  POST /causal/chain        技能链推荐
  POST /usage               回报技能使用结果
"""

from __future__ import annotations

import json
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from .capability import CapabilityRouter, SkillEntry
from .causal_graph import CausalGraph
from .mcsi import CausalState, TaskType

DATA_DIR = Path(__file__).parent.parent.parent / "data"


class HarnessState:
    """全局状态——路由器 + 因果图。"""

    def __init__(self) -> None:
        self.router = CapabilityRouter()
        self.graph = CausalGraph()
        self._load()

    def _load(self) -> None:
        index_path = DATA_DIR / "skill_index.json"
        if index_path.exists():
            self.router = CapabilityRouter(index_path)
        graph_path = DATA_DIR / "causal_graph.json"
        if graph_path.exists():
            self.graph = CausalGraph.load(graph_path)
        # 加载经验标注
        annotations_path = DATA_DIR / "skill_annotations.json"
        if annotations_path.exists():
            self._apply_annotations(annotations_path)

    def _apply_annotations(self, path: Path) -> None:
        """应用经验标注——任务类型/格式/优先级。"""
        from .mcsi import TaskType as TT
        data = json.loads(path.read_text())
        for sid, ann in data.get("annotations", {}).items():
            if sid not in self.router.skills:
                continue
            skill = self.router.skills[sid]
            skill.task_types = [TT.from_legacy(t) for t in ann.get("task_types", [])]
            skill.input_formats = ann.get("input_formats", skill.input_formats)
            skill.priority = ann.get("priority", skill.priority)
            skill.tags = ann.get("tags", skill.tags)
            # 注册确定性规则（高优先才覆盖）
            for tt in skill.task_types:
                for fmt in skill.input_formats or ["*"]:
                    key = f"{tt.value}:{fmt}"
                    existing = self.router._deterministic_rules.get(key)
                    if existing is None or (
                        existing in self.router.skills
                        and skill.priority > self.router.skills[existing].priority
                    ):
                        self.router._deterministic_rules[key] = sid

    def reload(self) -> None:
        self._load()


STATE = HarnessState()


class HarnessHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。"""

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok", "version": "0.1.0"})
        elif self.path == "/stats":
            self._json(200, {
                "router": STATE.router.get_stats(),
                "graph": STATE.graph.get_stats(),
            })
        elif self.path == "/skills":
            skills = [
                {"id": s.skill_id, "name": s.name, "description": s.description,
                 "task_types": [t.value for t in s.task_types], "priority": s.priority,
                 "is_dormant": s.is_dormant}
                for s in STATE.router.skills.values()
            ]
            self._json(200, {"skills": skills, "total": len(skills)})
        elif self.path.startswith("/skills/"):
            skill_id = self.path[8:]
            skill = STATE.router.skills.get(skill_id)
            if skill:
                profile = STATE.graph.profiles.get(skill_id)
                self._json(200, {
                    "skill": {"id": skill.skill_id, "name": skill.name,
                              "description": skill.description,
                              "task_types": [t.value for t in skill.task_types],
                              "priority": skill.priority},
                    "profile": {
                        "preconditions": profile.preconditions if profile else [],
                        "failure_modes": profile.failure_modes if profile else [],
                        "composed_with": STATE.graph.get_companions(skill_id),
                        "fallbacks": STATE.graph.get_fallbacks(skill_id),
                    } if profile else None,
                })
            else:
                self._json(404, {"error": f"skill not found: {skill_id}"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        body = self._read_body()
        if self.path == "/route":
            self._handle_route(body)
        elif self.path == "/causal/discover":
            self._handle_discover(body)
        elif self.path == "/causal/chain":
            self._handle_chain(body)
        elif self.path == "/usage":
            self._handle_usage(body)
        elif self.path == "/reload":
            STATE.reload()
            self._json(200, {"status": "reloaded"})
        else:
            self._json(404, {"error": "not found"})

    def _handle_route(self, body: dict) -> None:
        try:
            state = CausalState(
                task_type=TaskType.from_legacy(body.get("task_type", "unknown")),
                goal=body.get("goal", ""),
                constraints=body.get("constraints", []),
                required_capability=body.get("required_capability"),
                input_format=body.get("input_format"),
                priority=body.get("priority", 5),
            )
        except (ValueError, KeyError) as e:
            self._json(400, {"error": f"invalid request: {e}"})
            return

        top_k = body.get("top_k", 5)
        result = STATE.router.route(state, top_k=top_k)

        # 附加因果图信息
        candidates = []
        for skill in result.candidates:
            companions = STATE.graph.get_companions(skill.skill_id)
            fallbacks = STATE.graph.get_fallbacks(skill.skill_id)
            ok, missing = STATE.graph.check_preconditions(skill.skill_id, state)
            candidates.append({
                "skill_id": skill.skill_id,
                "name": skill.name,
                "score": round(skill.matches_state(state), 3),
                "priority": skill.priority,
                "preconditions_met": ok,
                "missing_preconditions": missing,
                "companions": companions,
                "fallbacks": fallbacks,
            })

        self._json(200, {
            "level": result.level.name,
            "candidates": candidates,
            "latency_ms": round(result.latency_ms, 3),
            "fallback_to_llm": result.fallback_to_llm,
        })

    def _handle_discover(self, body: dict) -> None:
        state = CausalState(
            task_type=TaskType.from_legacy(body.get("task_type", "unknown")),
            goal=body.get("goal", ""),
        )
        all_skills = list(STATE.router.skills.keys())
        dormant = {sid for sid, s in STATE.router.skills.items() if s.is_dormant}
        results = STATE.graph.discover_cold_skills(state, all_skills, dormant)
        self._json(200, {"discovered": results})

    def _handle_chain(self, body: dict) -> None:
        primary = body.get("primary_skill", "")
        goal = body.get("goal", "")
        state = CausalState(task_type=TaskType.UNKNOWN, goal=goal)
        chain = STATE.graph.recommend_chain(state, primary)
        self._json(200, {"chain": chain})

    def _handle_usage(self, body: dict) -> None:
        skill_id = body.get("skill_id", "")
        success = body.get("success", True)
        STATE.router.update_usage(skill_id, success)
        self._json(200, {"status": "recorded"})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _json(self, code: int, data: dict) -> None:
        resp = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, format: str, *args: object) -> None:
        pass  # 静默日志


def main(host: str = "127.0.0.1", port: int = 18400) -> None:
    server = HTTPServer((host, port), HarnessHandler)
    print(f"RPE-Harness API listening on http://{host}:{port}")
    print(f"  Skills: {len(STATE.router.skills)}")
    print(f"  Causal edges: {len(STATE.graph.edges)}")
    print(f"  Endpoints: /health /stats /route /skills /causal/* /usage")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18400
    main(port=port)
