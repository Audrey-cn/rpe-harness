from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
import traceback
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / 'snapshot'
REPO = Path('/Users/audrey/项目/rpe-harness')
PYTHON = sys.executable
ENV = dict(os.environ, PYTHONPATH=str(SNAPSHOT / 'src'), PYTHONDONTWRITEBYTECODE='1')


def run(name: str, args: list[str]) -> dict:
    result = subprocess.run(args, cwd=SNAPSHOT, env=ENV, text=True, capture_output=True)
    (ROOT / (name + '.stdout.log')).write_text(result.stdout)
    (ROOT / (name + '.stderr.log')).write_text(result.stderr)
    record = {'command': args, 'cwd': str(SNAPSHOT), 'returncode': result.returncode,
              'stdout': str(ROOT / (name + '.stdout.log')),
              'stderr': str(ROOT / (name + '.stderr.log'))}
    return record


def test_functions() -> dict:
    records = []
    for filename in ('test_capability.py', 'test_mcsi.py'):
        name = filename.removesuffix('.py')
        spec = importlib.util.spec_from_file_location(name, SNAPSHOT / 'tests' / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        for key, value in vars(module).items():
            if key.startswith('test_') and callable(value):
                record = {'test': name + '.' + key}
                try:
                    value()
                    record['status'] = 'pass'
                except Exception:
                    record['status'] = 'fail'
                    record['traceback'] = traceback.format_exc()
                records.append(record)
    return {'framework': 'direct function runner (not pytest)', 'total': len(records),
            'passed': sum(r['status'] == 'pass' for r in records),
            'failed': sum(r['status'] == 'fail' for r in records), 'results': records}


def compact_cold(report: dict) -> dict:
    return {key: report[key] for key in (
        'config', 'recovery_rate', 'recovery', 'false_positive_rate',
        'false_positives', 'routing', 'latency_ms', 'passed')}


def main() -> None:
    sys.path.insert(0, str(SNAPSHOT / 'src'))
    commands = [
        run('unittest', [PYTHON, '-m', 'unittest', 'discover', '-s', 'tests', '-v']),
        run('baseline_default', [PYTHON, '-m', 'rpe_harness.experiments.baseline']),
        run('cold_original', [PYTHON, '-m', 'rpe_harness.experiments.cold_skill_test',
                              '--report', str(ROOT / 'cold-reproduced.json')]),
        run('stress_original', [PYTHON, '-c',
            'import json; from pathlib import Path; '
            'from rpe_harness.experiments.stress_test import run_stress_test; '
            'report=run_stress_test(); '
            f'Path({str(ROOT / "stress-reproduced.json")!r}).write_text(json.dumps(report,indent=2)); '
            'print(json.dumps(report,indent=2))']),
    ]
    (ROOT / 'commands.json').write_text(json.dumps(commands, indent=2))
    function_results = test_functions()
    (ROOT / 'function-tests.json').write_text(json.dumps(function_results, indent=2))

    from rpe_harness.capability import CapabilityRouter
    from rpe_harness.causal_graph import CausalGraph
    from rpe_harness.experiments import cold_skill_test as cold
    from rpe_harness.mcsi import CausalState, TaskType

    original = compact_cold(cold.run_experiment())
    with patch.object(cold, 'prepare_graph', lambda *args: None):
        historical_only = compact_cold(cold.run_experiment())

    original_load = CausalGraph.load

    def load_without_edges(path: Path) -> CausalGraph:
        graph = original_load(path)
        graph.edges = []
        graph._adjacency = {}
        return graph

    with patch.object(CausalGraph, 'load', load_without_edges):
        no_edges = compact_cold(cold.run_experiment())

    def remove_contexts(graph: CausalGraph, *args) -> None:
        for profile in graph.profiles.values():
            profile.usage_contexts = []

    with patch.object(cold, 'prepare_graph', remove_contexts):
        graph_only = compact_cold(cold.run_experiment())

    variants = {'original': original, 'original_graph_no_synthetic_contexts': historical_only,
                'no_graph_edges_with_synthetic_contexts': no_edges,
                'graph_edges_only_no_usage_contexts': graph_only}
    (ROOT / 'cold-ablations.json').write_text(json.dumps(variants, indent=2, ensure_ascii=False))

    seeds = []
    for seed in range(20):
        report = cold.run_experiment(seed=seed)
        seeds.append({'seed': seed, 'recovery_rate': report['recovery_rate'],
                      'FDR_labelled_false_positive_rate': report['false_positive_rate'],
                      'route_top1_accuracy': report['routing']['top1_accuracy'],
                      'passed': report['passed']})
    (ROOT / 'cold-20-seeds.json').write_text(json.dumps(seeds, indent=2))

    router = CapabilityRouter(SNAPSHOT / 'data' / 'skill_index.json')
    ids = sorted(router.skills)
    dormant = sorted(random.Random(cold.DEFAULT_SEED).sample(ids, cold.NUM_DORMANT))
    graph = CausalGraph.load(SNAPSHOT / 'data' / 'causal_graph.json')
    cold.prepare_graph(graph, router.skills, cold.build_usage_contexts(router.skills))
    independent_goals = {
        'apple/imessage': '给家人发一条短信，说明我今晚会晚点到家',
        'creative/baoyu-infographic': '把这份年度经营数据整理成一张适合分享的可视化信息图',
        'creative/design-md': '从现有网页提取字体颜色间距和布局规范，写成可复用设计指南',
        'creative/humanizer': '这段文字读起来很像机器写的，请改成自然可信的表达',
        'creative/popular-web-designs': '为新的软件服务首页找几种常见且成熟的网页布局参考',
        'productivity/document-to-action-items': '把会议纪要中的决定、责任人和截止日期整理成待办清单',
        'productivity/maps': '帮我比较从家到机场的开车和公共交通路线',
        'productivity/powerpoint': '把产品方案整理成十页演示文稿，供周会汇报使用',
        'root/uaek-memory-management': '把我这周形成的项目习惯保存到长期记忆，下次继续沿用',
        'software-development/node-inspect-debugger': '给正在运行的服务进程加断点，检查异常请求中的变量值',
    }
    probes = []
    for sid, goal in independent_goals.items():
        skill = router.skills[sid]
        state = CausalState(task_type=skill.task_types[0], goal=goal,
                            input_format=skill.input_formats[0] if skill.input_formats else None,
                            required_capability=skill.tags[0] if skill.tags else None,
                            constraints=skill.preconditions[:2])
        found = graph.discover_cold_skills(state, ids, set(dormant))
        probes.append({'target': sid, 'goal': goal, 'found': found,
                       'target_found': sid in [item['skill_id'] for item in found]})
    independent = {'design': '10 independently written Chinese paraphrases; exploratory probes, not a representative benchmark',
                   'total': len(probes), 'target_found': sum(p['target_found'] for p in probes), 'probes': probes}
    (ROOT / 'cold-independent-paraphrases.json').write_text(json.dumps(independent, indent=2, ensure_ascii=False))

    index = json.loads((SNAPSHOT / 'data' / 'skill_index.json').read_text())
    annotations = json.loads((SNAPSHOT / 'data' / 'skill_annotations.json').read_text())['annotations']
    raw_graph = json.loads((SNAPSHOT / 'data' / 'causal_graph.json').read_text())
    original_report = json.loads((SNAPSHOT / 'reports' / 'cold_skill_test.json').read_text())
    skills = index['skills']
    skill_ids = set(s['skill_id'] for s in skills)
    counters = {'skills': len(skills), 'annotations': len(annotations),
                'annotations_with_index_matches': len(set(annotations) & skill_ids),
                'unannotated_skills': len(skill_ids - set(annotations)),
                'indexed_task_types': dict(Counter(t for s in skills for t in s['task_types'])),
                'unknown_only_skills': sum(s['task_types'] == ['unknown'] for s in skills),
                'all_zero_use_count': all(s['use_count'] == 0 for s in skills),
                'all_zero_last_used': all(s['last_used'] == 0 for s in skills),
                'all_success_rate_1': all(s['success_rate'] == 1 for s in skills),
                'dormant_in_index': sum(s['is_dormant'] for s in skills),
                'indexed_skill_path_missing': [s['path'] for s in skills if not Path(s['path']).exists()],
                'graph_profiles': len(raw_graph['profiles']), 'graph_edges': len(raw_graph['edges']),
                'graph_profile_ids_missing_from_index': sorted(set(raw_graph['profiles']) - skill_ids),
                'graph_edge_endpoints_missing_from_index': sorted({e[k] for e in raw_graph['edges'] for k in ('source','target')} - skill_ids),
                'graph_evidence_counts': dict(Counter(e['evidence_count'] for e in raw_graph['edges'])),
                'report_task_types': dict(Counter(t['task_type'] for t in original_report['tasks'])),
                'report_target_id_in_goal': sum(t['target_skill'].split('/')[-1].lower() in t['goal'].lower() for t in original_report['tasks']),
                'report_FDR': original_report['false_positives']['false_positives']/original_report['false_positives']['total_discoveries']}
    (ROOT / 'data-inventory.json').write_text(json.dumps(counters, indent=2, ensure_ascii=False))

    manifest = json.loads((ROOT / 'original-manifest.json').read_text())
    changed = [name for name, digest in manifest.items()
               if not (REPO / name).exists() or hashlib.sha256((REPO / name).read_bytes()).hexdigest() != digest]
    summary = {'python': PYTHON, 'python_version': sys.version, 'audit_dir': str(ROOT),
               'commands': commands, 'direct_function_tests': function_results,
               'cold_ablation_metrics': {name: {'recovery_rate': report['recovery_rate'],
                                               'FDR': report['false_positive_rate'],
                                               'routing': report['routing'],
                                               'passed': report['passed']}
                                         for name, report in variants.items()},
               'independent_probes': {'total': independent['total'], 'target_found': independent['target_found']},
               '20_seed_recovery_range': [min(s['recovery_rate'] for s in seeds), max(s['recovery_rate'] for s in seeds)],
               '20_seed_FDR_range': [min(s['FDR_labelled_false_positive_rate'] for s in seeds), max(s['FDR_labelled_false_positive_rate'] for s in seeds)],
               'inventory': counters, 'original_repo_changed_files': changed}
    (ROOT / 'audit-summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
