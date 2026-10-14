"""Preregistered CSV learning comparison. Importing never invokes a model.

Pilot runs stop after training, generation, independent qualification and freeze.
Production requires an explicit flag; reserved attempts are never silently rerun.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .cost_ledger import PhaseLedger, summarize_phases

ARMS = ('native', 'rpe_no_experience', 'rpe_learned')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)


def _save(path, value):
    """Create immutable evidence, never replace a reservation or result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(_json(value))
        stream.flush()
        os.fsync(stream.fileno())


def _sealed(value):
    return {**value, 'sha256': _sha(_json(value).encode())}


def _read_sealed(path):
    value = json.loads(path.read_text())
    saved = value.pop('sha256')
    if saved != _sha(_json(value).encode()):
        raise ValueError('registration or frozen evidence changed')
    return {**value, 'sha256': saved}


def _case(name, source, expected, keys, delimiter=',', encoding='utf-8'):
    return {'case_id': name, 'source_text': source, 'expected_rows': expected,
            'parameters': {'key_columns': keys, 'delimiter': delimiter, 'encoding': encoding, 'keep': 'first'},
            'input_sha256': _sha(source.encode(encoding))}


def _fixtures():
    # Truth rows are authored literally, never obtained by invoking the product.
    training = [
        _case('train-order', 'order,amount\n001,10\n002,20\n001,99\n',
              [['order','amount'],['001','10'],['002','20']], ['order']),
        _case('train-compound', 'region,sku,qty\nN,A,1\nS,A,2\nN,A,8\nN,B,3\n',
              [['region','sku','qty'],['N','A','1'],['S','A','2'],['N','B','3']], ['region','sku']),
        _case('train-chinese', '编号,名称\n甲,茶\n乙,米\n甲,糖\n',
              [['编号','名称'],['甲','茶'],['乙','米']], ['编号'], encoding='utf-8-sig'),
        _case('train-delimiter', 'code;note\nA;"x;y"\nA;later\nB;end\n',
              [['code','note'],['A','x;y'],['B','end']], ['code'], ';'),
        _case('train-quoted', 'id,note\na,"two\nlines"\nb,"x,y"\na,later\n',
              [['id','note'],['a','two\nlines'],['b','x,y']], ['id']),
        _case('train-exact', 'token,val\n01,first\n1,number\n 01,space\nA,upper\na,lower\n01,later\n',
              [['token','val'],['01','first'],['1','number'],[' 01','space'],['A','upper'],['a','lower']], ['token']),
    ]
    holdout = [
        _case('eval-exact', 'value,ref\nfirst,001\nnumeric,1\nspace, 001\nlater,001\n',
              [['value','ref'],['first','001'],['numeric','1'],['space',' 001']], ['ref']),
        _case('eval-composite', 'memo,zone,product\nfirst,east,007\nnext,west,007\ndup,east,007\nthird,east,7\n',
              [['memo','zone','product'],['first','east','007'],['next','west','007'],['third','east','7']], ['product','zone']),
        _case('eval-unicode', '描述|索引\n"蓝|绿"|丙\n白|丁\n红|丙\n',
              [['描述','索引'],['蓝|绿','丙'],['白','丁']], ['索引'], '|', 'utf-8-sig'),
        _case('eval-multiline-key', 'payload,identity\nfirst,"line\nbreak"\nsecond,"line break"\nlater,"line\nbreak"\n',
              [['payload','identity'],['first','line\nbreak'],['second','line break']], ['identity']),
        _case('eval-quote-key', 'body;reference;batch\nfirst;"q""r";K\nsecond;"q""r";L\nlater;"q""r";K\n',
              [['body','reference','batch'],['first','q"r','K'],['second','q"r','L']], ['reference','batch'], ';'),
        _case('eval-tab-case', 'item\tlabel\tpayload\nUP\tX\tfirst\nup\tX\tsecond\nUP\tX\tlater\nUP\tx\tthird\n',
              [['item','label','payload'],['UP','X','first'],['up','X','second'],['UP','x','third']], ['label','item'], '\t'),
        _case('eval-empty-values', 'detail,record\n,z9\n"",z8\nlater,z9\n"comma,here",z7\n',
              [['detail','record'],['','z9'],['','z8'],['comma,here','z7']], ['record']),
        _case('eval-crlf', 'message,group,sequence\r\n"one\r\ntwo",G,09\r\nnext,G,9\r\nlater,G,09\r\n',
              [['message','group','sequence'],['one\r\ntwo','G','09'],['next','G','9']], ['sequence','group']),
    ]
    return training, holdout


def _prompt(case, label):
    return ('在当前工作目录处理 CSV 去重任务。输入 '+label+'/input.csv，输出 '+label+'/output.csv。'
        '按指定键列的精确字符串元组去重，保留第一条和原顺序、完整表头、全部原始单元格值。'
        '不得裁剪空格、转换数值或大小写；正确处理引号、嵌入分隔符和换行。保留源文件且禁止覆盖输出。'
        '参数：'+_json(case['parameters'])+'。有专用任务工具时按严格工作流执行新的实际任务；'
        '否则使用原生 terminal/file。允许复用本臂工作区已有工具，禁止读取其他臂、源代码、'
        '数据库、实验报告、资格验收或未开放输入。最终仅报告完成状态与输出路径。')


def _environment():
    package = Path(__file__).resolve().parents[1]
    config = Path.home()/'.hermes/config.yaml'
    return {'source_sha256': {str(p.relative_to(package)): _sha(p.read_bytes()) for p in sorted(package.rglob('*.py'))},
            'host_config_sha256': _sha(config.read_bytes()) if config.is_file() else None,
            'python': str(Path(sys.executable).absolute()), 'python_version': sys.version}


def preregister_learning(root, *, timeout=180):
    if type(timeout) is not int or not 1 <= timeout <= 180:
        raise ValueError('timeout must be an integer between 1 and 180')
    root = Path(root).absolute()
    if root.is_symlink():
        raise ValueError('experiment root cannot be a symlink')
    root.mkdir(parents=True, exist_ok=True)
    path = root/'preregistration.json'
    if path.exists():
        plan = _read_sealed(path)
        if plan['root'] != str(root) or plan['timeout'] != timeout:
            raise ValueError('registration differs; use a new experiment root')
        return plan
    training, holdout = _fixtures()
    sessions = []
    for repeat in (1, 2):
        for index, case in enumerate(holdout):
            label = f"{case['case_id']}-r{repeat}"
            offset = (index + repeat-1) % 3
            for arm in ARMS[offset:]+ARMS[:offset]:
                sessions.append({'attempt_id': label+'-'+arm, 'arm': arm,
                    'case_id': case['case_id'], 'repeat': repeat, 'label': label,
                    'input_sha256': case['input_sha256'], 'prompt_sha256': _sha(_prompt(case,label).encode())})
    plan = _sealed({'schema_version': 'rpe.learning-comparison/v1', 'root': str(root),
        'timeout': timeout, 'training': training, 'holdout': holdout, 'sessions': sessions,
        'generation_limit': 3, 'generation_max_turns': 6, 'production_max_task_attempts': 3,
        'analysis_units': 8, 'repeats_independent': False,
        'policy': 'native terminal/file; both RPE arms identical strict task tools and budget; only active executor differs',
        'training_cost_policy': '6 actual native sessions and 6 local CLI runs per RPE arm; all measured once; no shared allocation',
        'diagnostic_policy': 'G0/G1 is diagnostic pre/post, not an isolated feedback causal ablation',
        'resume_policy': 'reserved attempts remain incomplete on interruption; no automatic replacement',
        'registered_environment': _environment()})
    for name in ('arms','records','ledger','evaluator'):
        (root/name).mkdir(exist_ok=True)
    for arm in ARMS:
        (root/'arms'/arm/'workspace').mkdir(parents=True)
    _save(path, plan)
    return plan


def artifact_matches(path, case):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 16777216:
        return False
    try:
        with path.open(encoding=case['parameters']['encoding'], newline='') as stream:
            rows = list(csv.reader(stream, delimiter=case['parameters']['delimiter'], strict=True))
        return rows == case['expected_rows']
    except (OSError, UnicodeError, csv.Error):
        return False


def _phase(root, arm, phase, attempt_id, action, *, api_expected, scope='operational'):
    """A reservation is the at-most-once boundary, including process interruption."""
    root = Path(root)
    ledger = PhaseLedger(root/'ledger')
    previous = next((r for r in ledger.records() if r['attempt_id'] == attempt_id), None)
    result_path = root/'records'/(attempt_id+'.json')
    if previous is not None:
        if (previous['arm'], previous['phase'], previous['api_expected'], previous['scope']) != (arm,phase,api_expected,scope):
            raise ValueError('attempt identity collision')
        if previous['status'] == 'incomplete':
            return {'status': 'incomplete', 'attempt_id': attempt_id, 'error_code': 'reserved_attempt_not_replayed'}
        if not result_path.exists():
            raise ValueError('finished phase lacks compact evidence')
        return _read_sealed(result_path)['result']
    ledger.reserve(arm, phase, attempt_id, api_expected=api_expected, scope=scope)
    started = time.monotonic()
    try:
        result = action()
        if not isinstance(result, dict):
            raise TypeError('phase must return structured evidence')
    except Exception as error:
        result = {'status': 'failed', 'error_code': type(error).__name__}
    elapsed = time.monotonic()-started
    result = {'status': 'failed' if result.get('passed') is False else 'succeeded', **result}
    outcome = {'status': 'completed' if result['status'] == 'succeeded' else result['status'], 'elapsed_seconds': elapsed, 'usage': result.get('usage')}
    for key in ('model_identity','artifact_correct','workflow_verified','fallback','candidate_sha256','version_id','error_code'):
        if key in result:
            outcome[key] = result[key]
    if 'candidate_direct_success' in result:
        outcome['candidate_reused'] = result['candidate_direct_success']
    _save(result_path, _sealed({'attempt_id': attempt_id, 'result': result}))
    ledger.finish(attempt_id, outcome)
    return result


def _materialize(root, arm, case, label):
    workspace = root/'arms'/arm/'workspace'
    directory = workspace/label
    directory.mkdir()
    source = directory/'input.csv'
    source.write_bytes(case['source_text'].encode(case['parameters']['encoding']))
    request = {'schema_version': 1, 'event_id': label+'-'+arm,
        'state': {'task_type': 'data.processing', 'goal': '保留首条', 'input_format':'csv',
                  'required_capability':'tabular.deduplicate', 'constraints':['preserve_original','preserve_order']},
        'execution': {'input_path':str(source), 'output_path':str(directory/'output.csv'), **case['parameters']}}
    return workspace, request


def seed_training(root, plan, arm):
    """Real subprocess CLI runs, each independently checked against literal truth."""
    from ..evolution.training_feedback import build_training_packet
    if arm not in ARMS[1:]:
        raise ValueError('CLI training is only for RPE arms')
    root = Path(root)
    database = root/'arms'/arm/'runs.sqlite'
    identifiers = []
    for case in plan['training']:
        label = case['case_id']
        def action(case=case, label=label):
            workspace, request = _materialize(root, arm, case, label)
            process = subprocess.run([sys.executable, '-B', '-m', 'rpe_harness', 'run', '--request','-', '--db',str(database)],
                input=_json(request), capture_output=True, text=True, timeout=plan['timeout'])
            run = json.loads(process.stdout)
            correct = artifact_matches(request['execution']['output_path'], case)
            okay = process.returncode == 0 and run.get('status') == 'succeeded' and (run.get('verification') or {}).get('passed') is True and correct
            return {'status':'succeeded' if okay else 'failed', 'run_id':run.get('run_id'),
                    'artifact_correct':correct, 'workflow_verified':okay,
                    'input_sha256':case['input_sha256'], 'output_sha256':(run.get('artifact') or {}).get('sha256')}
        result = _phase(root,arm,'training',label+'-'+arm, action,api_expected=False)
        if result['status'] != 'succeeded':
            raise ValueError('training attempt failed or incomplete; preserved without replay')
        identifiers.append(result['run_id'])
    packet = build_training_packet(database, identifiers, authorize_data_export=True)
    # Product-derived export must also match independently authored training truths.
    by_id = {identifier:case for identifier,case in zip(identifiers,plan['training'])}
    for example in packet['examples']:
        if example['expected_rows'] != by_id[example['run_id']]['expected_rows']:
            raise ValueError('training export disagrees with independent truth')
    return packet


def summarize_learning(records, phases):
    records, phases = list(records), list(phases)
    if len({r['attempt_id'] for r in records}) != len(records):
        raise ValueError('duplicate production attempt')
    arms = {}
    for arm in ARMS:
        rows = [r for r in records if r['arm'] == arm]
        arms[arm] = {'sessions':len(rows), 'artifact_successes':sum(r.get('artifact_correct') is True for r in rows),
            'workflow_successes':sum(r.get('workflow_verified') is True for r in rows),
            'candidate_direct_successes':sum(r.get('candidate_direct_success') is True for r in rows),
            'fallbacks':sum(r.get('fallback') is True for r in rows),
            'cost':summarize_phases(p for p in phases if p['arm']==arm),
            'production_cost':summarize_phases(p for p in phases if p['arm']==arm and p['phase']=='production')}
    units = []
    for case in sorted({r['case_id'] for r in records}):
        units.append({'case_id':case, **{arm:sum(r.get('artifact_correct') is True for r in records if r['arm']==arm and r['case_id']==case)/2 for arm in ARMS}})
    identities = {_json(r['model_identity']) for r in records if r.get('model_identity') is not None}
    comparisons = {}
    for reference in ('native','rpe_no_experience'):
        learned, baseline = arms['rpe_learned'], arms[reference]
        complete = all(arms[arm]['sessions']==16 for arm in ARMS)
        a, b = learned['cost']['total_tokens'], baseline['cost']['total_tokens']
        comparisons['learned_minus_'+('fixed' if reference=='rpe_no_experience' else 'native')] = {
            'artifact_success_delta':learned['artifact_successes']-baseline['artifact_successes'],
            'total_tokens_delta':a-b if complete and a is not None and b is not None else None,
            'complete_design':complete}
    return {'planned_sessions':48, 'completed_sessions':sum(r.get('status')!='incomplete' for r in records),
        'reserved_sessions':len(records), 'analysis_units':8, 'comparisons':comparisons,
        'repeats_independent':False, 'arms':arms, 'per_input':units,
        'model_identity_coverage':sum(r.get('model_identity') is not None for r in records),
        'model_identities_consistent':len(identities)==1 if records else None,
        'cost':summarize_phases(phases), 'learning_benefit_established':False,
        'interpretation':'Mechanism success and learning benefit are separate; paired input outcomes and complete cumulative costs require review.',
        'records':records}


def _identity(value):
    if not isinstance(value, dict):
        return None
    return {key:value.get(key) for key in ('provider','requested_model','response_models')}


def executor_score(actual, fallback, frozen):
    return bool(frozen and not fallback and actual.get('kind')=='candidate' and
        actual.get('candidate_version_id')==frozen['version_id'] and
        actual.get('code_sha256')==frozen['candidate_sha256'])


def _native_links(result, database, label):
    """Separate observed API/tool linkage from independently checked business truth."""
    from collections import Counter
    from ..integrations.hermes_gate import _persisted_events, digest_json
    events = result.get('events') or []
    if not events or events != _persisted_events(database,result.get('session_id')):
        return False
    fields = ('session_id','task_id','turn_id','api_request_id')
    def key(event):
        return tuple(event.get(field) for field in fields)
    requests = [e for e in events if e.get('kind')=='pre_api_request']
    outcomes = [e for e in events if e.get('kind') in ('post_api_request','api_request_error')]
    if not requests or Counter(map(key,requests)) != Counter(map(key,outcomes)):
        return False
    if any(any(not part for part in key(e)) for e in requests) or any(e.get('kind')=='api_request_error' for e in outcomes):
        return False
    pre = [e for e in events if e.get('kind')=='pre_tool_call']
    post = [e for e in events if e.get('kind')=='post_tool_call']
    def call(e):
        return (*key(e),e.get('tool_call_id'),e.get('tool_name'))
    observed = Counter(map(call,pre))
    emitted = Counter((*key(e),c.get('tool_call_id'),c.get('tool_name')) for e in outcomes for c in e.get('model_tool_calls',[]))
    if not observed or observed!=Counter(map(call,post)) or observed!=emitted or any(n!=1 for n in observed.values()):
        return False
    if Counter((call(e),e.get('arguments_sha256')) for e in pre)!=Counter((call(e),e.get('arguments_sha256')) for e in post):
        return False
    streams = result.get('stream_tool_calls') or []
    if Counter((e.get('tool_name'),e.get('arguments_sha256')) for e in pre)!=Counter((c.get('name'),digest_json(c.get('input'))) for c in streams):
        return False
    # Only mark a native output invocation when its observed arguments name the output.
    for stream in streams:
        args = stream.get('input')
        if not isinstance(args,dict) or stream.get('name') not in ('terminal','write_file'):
            continue
        if label+'/output.csv' not in _json(args):
            continue
        if any(e.get('tool_name')==stream['name'] and e.get('arguments_sha256')==digest_json(args)
               and e.get('status') in ('succeeded','success','completed') for e in post):
            return True
    return False


def _denied(root, arm):
    return [root/'evaluator',root/'records',root/'ledger',root/'preregistration.json',root/'freeze.json',
            *(root/'arms'/other for other in ARMS if other!=arm)]


def _host_session(root, plan, arm, case, label, frozen=None):
    from ..integrations.hermes_session import run_hermes_session
    from ..integrations.hermes_gate import evaluate_session, read_run
    from ..tasks import plan_from_request, verify_plan
    workspace, request = _materialize(root,arm,case,label)
    database = root/'arms'/arm/'runs.sqlite'
    # Anchor the product plan before the model creates its output.
    product_plan = plan_from_request(request)
    prompt = _prompt(case,label)
    result = run_hermes_session(prompt,workspace,database,tool_enabled=arm!='native',
        owner_root=root/'arms'/arm/'sessions', timeout=plan['timeout'],
        entry_mode='optional' if arm=='native' else 'strict', max_task_attempts=plan['production_max_task_attempts'],
        context_sources=[], read_isolation_root=root, deny_read_roots=_denied(root,arm))
    record = {'status':'failed','session_id':result.get('session_id'),
        'usage':result.get('usage'),'model_identity':_identity(result.get('model_identity')),
        'original_prompt_sha256':result.get('original_prompt_sha256'),
        'actual_prompt_sha256':result.get('prompt_sha256'), 'input_sha256':case['input_sha256'],
        'artifact_correct':False,'workflow_verified':False,'candidate_direct_success':False,'fallback':False,
        'product_oracle_passed':False,'invocation_evidence':False,'run_id':None,
        'process_cleanup':result.get('process_cleanup'), 'session_root':result.get('session_root')}
    try:
        output = Path(request['execution']['output_path'])
        source = Path(request['execution']['input_path'])
        record['artifact_correct'] = artifact_matches(output,case)
        record['source_preserved'] = not source.is_symlink() and _sha(source.read_bytes())==case['input_sha256']
        if output.is_file() and not output.is_symlink():
            record['output_sha256'] = _sha(output.read_bytes())
            verified = verify_plan({**product_plan.verification_parameters(),'staging_path':str(output)},product_plan)
            record['product_oracle_passed'] = verified.get('passed') is True
        host_ok = (result.get('completed') is True and result.get('exit_code')==0 and not result.get('timed_out')
                   and not result.get('observer_errors') and (result.get('process_cleanup') or {}).get('cleanup_complete') is True)
        identity = record['model_identity']
        comparable = (result.get('original_prompt_sha256')==_sha(prompt.encode()) and identity is not None
                      and bool(identity.get('response_models')) and (frozen is None or identity==frozen['model_identity']))
        record['comparable'] = comparable
        if arm=='native':
            record['invocation_evidence'] = _native_links(result,database,label)
            record['workflow_verified'] = record['invocation_evidence']
        else:
            gate = evaluate_session(result,database,workspace,required_request=request,require_policy=True)
            record['gate'] = gate
            record['workflow_verified'] = gate.get('verified') is True
            record['invocation_evidence'] = record['workflow_verified']
            record['run_id'] = gate.get('run_id')
            if gate.get('run_id'):
                run = read_run(database,gate['run_id'])
                actual = (run.get('artifact') or {}).get('actual_executor') or {}
                record['actual_executor'] = actual
                record['fallback'] = any(step['stage']=='candidate_fallback' for step in run.get('steps',[]))
                record['candidate_direct_success'] = bool(record['workflow_verified'] and record['artifact_correct']
                    and record['product_oracle_passed'] and executor_score(actual,record['fallback'],frozen))
                record['candidate_version_id'] = actual.get('candidate_version_id')
                record['candidate_sha256'] = actual.get('code_sha256')
        success = host_ok and comparable and record['artifact_correct'] and record['product_oracle_passed'] and record['source_preserved']
        record['status'] = 'succeeded' if success else ('timed_out' if result.get('timed_out') else 'failed')
        # A missing tool receipt never upgrades to a candidate success via business truth.
        record['candidate_direct_success'] &= bool(success)
    except Exception as error:
        record['error_code'] = type(error).__name__
    # Compact actual observations survive cleanup; raw provider prose stays in owned sessions.
    record['event_digest'] = _sha(_json(result.get('events') or []).encode())
    record['tool_calls'] = [{key:e.get(key) for key in ('kind','session_id','task_id','turn_id','api_request_id',
        'tool_call_id','tool_name','arguments_sha256','status','run_id','receipt_id')}
        for e in result.get('events',[]) if e.get('kind') in ('pre_tool_call','post_tool_call','rpe_task_received','rpe_task_returned')]
    record['api_calls'] = [{key:e.get(key) for key in ('kind','session_id','task_id','turn_id','api_request_id',
        'response_model','request_tools_sha256','request_tool_names','request_tool_choice','model_tool_calls')}
        for e in result.get('events',[]) if e.get('kind') in ('pre_api_request','post_api_request','api_request_error')]
    return record


def _original_code(root, report):
    from ..path_binding import read_regular_file
    path = report.get('code_artifact_path')
    if not path:
        raise ValueError('generation lacks retained original code')
    path = Path(path)
    if not path.resolve().is_relative_to(root/'arms'/'rpe_learned'):
        raise ValueError('generated original code leaves owned root')
    raw = read_regular_file(path,max_bytes=65536)
    if _sha(raw)!=report.get('candidate_sha256'):
        raise ValueError('generated original code hash mismatch')
    return raw.decode('utf-8')


def _ast_hash(code):
    try:
        return _sha(ast.dump(ast.parse(code),include_attributes=False).encode())
    except SyntaxError:
        return None


def _check_freeze(root, plan, frozen):
    from ..evolution import EvolutionStore
    from ..path_binding import read_regular_file
    if frozen['registration_sha256']!=plan['sha256'] or frozen['environment']!=_environment():
        raise ValueError('frozen environment or registration changed')
    if _sha(read_regular_file(Path(frozen['code_artifact_path']),max_bytes=65536))!=frozen['candidate_sha256']:
        raise ValueError('frozen original generation artifact changed')
    store = EvolutionStore(root/'arms/rpe_learned/runs.sqlite',root/'arms/rpe_learned')
    version = store.get_version(frozen['version_id'])
    if _sha(store.read_artifact(version))!=frozen['candidate_sha256']:
        raise ValueError('frozen candidate code changed')
    active = store.get_active('executor','csv_executor')
    # Production quarantine is a measured failure, not permission to install another candidate.
    if active is None:
        if version['state']!='quarantine':
            raise ValueError('frozen candidate unexpectedly deactivated')
    elif active['version_id']!=frozen['version_id']:
        raise ValueError('frozen candidate replaced')
    fixed = EvolutionStore(root/'arms/rpe_no_experience/runs.sqlite',root/'arms/rpe_no_experience')
    if fixed.get_active('executor','csv_executor') is not None:
        raise ValueError('fixed builtin arm has an active candidate')


def _pilot(root, plan):
    from ..evolution.worker import run_proposal_worker
    from ..evolution.training_feedback import validate_training_packet, probe_training_candidate
    from ..evolution import validate_candidate, apply_candidate
    if plan['registered_environment']!=_environment():
        raise ValueError('registered environment changed before pilot; register a new root')
    packets = {arm:seed_training(root,plan,arm) for arm in ARMS[1:]}
    for case in plan['training']:
        result = _phase(root,'native','training',case['case_id']+'-native',
            lambda case=case:_host_session(root,plan,'native',case,case['case_id']),api_expected=True)
        if result['status']=='incomplete':
            return {'stage':'incomplete','reason':'native_training_interrupted'}
    learned = root/'arms/rpe_learned'
    database = learned/'runs.sqlite'
    packet = packets['rpe_learned']
    previous, g0_code, g0_probe = None,None,None
    for index in range(plan['generation_limit']+1):
        phase_id = f'generation-G{index}'
        def generation():
            if plan['registered_environment']!=_environment():
                raise ValueError('registered environment changed before generation')
            validate_training_packet(database,packet)
            report = run_proposal_worker(database,learned,kind='csv_executor',timeout=plan['timeout'],
                training_packet=packet if index else None,previous_candidate=previous if index else None,
                max_turns=plan['generation_max_turns'],deny_read_roots=_denied(root,'rpe_learned'))
            validate_training_packet(database,packet)
            return {**report,'status':'succeeded' if report.get('completed') else 'failed'}
        report = _phase(root,'rpe_learned','generation',phase_id,generation,api_expected=True,
                        scope='operational')
        if report['status']=='incomplete':
            return {'stage':'incomplete','reason':'generation_interrupted','generation':index}
        try:
            code = _original_code(root,report)
        except (ValueError,OSError,UnicodeError):
            if index==0:
                return {'stage':'failed','reason':'G0_original_code_missing'}
            continue
        # Worker independently probes previous_candidate; this probe measures the new candidate.
        probe = _phase(root,'rpe_learned','training_probe',f'probe-G{index}',
            lambda:probe_training_candidate(code,packet,root/'evaluator'/'probes'), api_expected=False,
            scope='operational')
        if probe['status']!='succeeded':
            return {'stage':'incomplete','reason':'training_probe_failed_or_interrupted'}
        previous = code
        if index==0:
            g0_code,g0_probe = code,probe
            continue
        if report['status']!='succeeded' or not report.get('proposal_id'):
            continue
        if report.get('training_packet_sha256')!=packet['packet_sha256']:
            return {'stage':'failed','reason':'generation_training_packet_binding_missing'}
        # Acceptance feedback stays evaluator-only; subsequent revisions see training only.
        qualified = _phase(root,'rpe_learned','qualification',f'qualify-G{index}',
            lambda:validate_candidate(report['proposal_id'],database,learned),api_expected=False)
        if qualified['status']=='incomplete':
            return {'stage':'incomplete','reason':'qualification_interrupted'}
        if not qualified.get('passed'):
            continue
        applied = _phase(root,'rpe_learned','activation_replay',f'activate-G{index}',
            lambda:apply_candidate(report['proposal_id'],database,learned),api_expected=False)
        if not applied.get('applied'):
            return {'stage':applied['status'],'reason':'activation_not_completed'}
        phase_records = PhaseLedger(root/'ledger').records()
        identities = [_identity(p.get('model_identity')) for p in phase_records if p['api_expected']]
        observed = [i for i in identities if i and i.get('response_models')]
        if not observed or any(identity!=observed[0] for identity in observed):
            return {'stage':'failed','reason':'generation_or_training_model_identity_mismatch'}
        original = _original_code(root,report)
        if _sha(original.encode())!=applied['content_hash']:
            return {'stage':'failed','reason':'activated_code_differs_from_original'}
        before = {o['run_id']:o['correct'] for o in g0_probe['outcomes']}
        after = {o['run_id']:o['correct'] for o in probe['outcomes']}
        frozen = _sealed({'schema_version':'rpe.learning-freeze/v1','registration_sha256':plan['sha256'],
            'version_id':applied['version_id'],'candidate_sha256':applied['content_hash'],
            'code_artifact_path':report['code_artifact_path'],'proposal_id':report['proposal_id'],
            'packet_sha256':packet['packet_sha256'],'model_identity':observed[0],
            'model_identity_coverage':len(observed),'model_session_count':len(identities),
            'generation':index,'generation_link':report.get('generation_link'),
            'environment':_environment(),'g0_sha256':_sha(g0_code.encode()),
            'g0_ast_sha256':_ast_hash(g0_code),'candidate_ast_sha256':_ast_hash(code),
            'ast_changed':_ast_hash(g0_code)!=_ast_hash(code),
            'training_behavior_changed':before!=after,
            'training_behavior_improved':sum(after.values())>sum(before.values()),
            'g0_training_correct':sum(before.values()),'learned_training_correct':sum(after.values()),
            'diagnostic_policy':plan['diagnostic_policy']})
        _save(root/'freeze.json',frozen)
        return {'stage':'frozen','freeze':frozen}
    return {'stage':'failed','reason':'no_qualified_feedback_candidate','generation_attempts':plan['generation_limit']+1}


def run_learning_comparison(root, *, timeout=180, formal=False, max_sessions=48):
    """Run/resume pilot, or explicitly run the frozen 48-session production design."""
    if type(max_sessions) is not int or not 0<=max_sessions<=48:
        raise ValueError('max_sessions must be between 0 and 48')
    root = Path(root).absolute()
    plan = preregister_learning(root,timeout=timeout)
    freeze_path = root/'freeze.json'
    if formal and not freeze_path.exists():
        raise ValueError('formal production requires a completed pilot freeze')
    if freeze_path.exists():
        frozen = _read_sealed(freeze_path)
        _check_freeze(root,plan,frozen)
        result = {'stage':'frozen','freeze':frozen}
    else:
        result = _pilot(root,plan)
    if formal:
        frozen = result['freeze']
        by_id = {c['case_id']:c for c in plan['holdout']}
        previous = {p['attempt_id'] for p in PhaseLedger(root/'ledger').records()}
        launched=0
        for trial in plan['sessions']:
            if trial['attempt_id'] not in previous:
                if launched>=max_sessions:
                    continue
                _check_freeze(root,plan,frozen)
                launched+=1
            case = by_id[trial['case_id']]
            def action(trial=trial,case=case):
                record = _host_session(root,plan,trial['arm'],case,trial['label'],frozen)
                prior = [_read_sealed(p)['result'] for p in (root/'records').glob('*.json')]
                record['fresh_session'] = bool(record.get('session_id') and
                    all(r.get('session_id')!=record['session_id'] for r in prior))
                record['fresh_run'] = (None if trial['arm']=='native' else bool(record.get('run_id') and
                    all(r.get('run_id')!=record['run_id'] for r in prior)))
                if not record['fresh_session'] or record['fresh_run'] is False:
                    record.update(status='failed',workflow_verified=False,candidate_direct_success=False,
                                  error_code='fresh_session_or_execution_missing')
                try:
                    _check_freeze(root,plan,frozen)
                except (ValueError,OSError,KeyError):
                    record.update(status='failed',workflow_verified=False,candidate_direct_success=False,
                                  error_code='frozen_state_changed_during_session')
                return {**trial,**record}
            _phase(root,trial['arm'],'production',trial['attempt_id'],action,api_expected=True)
        result['stage']='production'
    phases = PhaseLedger(root/'ledger').records()
    records=[]
    by_attempt = {s['attempt_id']:s for s in plan['sessions']}
    for phase in phases:
        if phase['phase']!='production':
            continue
        path = root/'records'/(phase['attempt_id']+'.json')
        record = _read_sealed(path)['result'] if path.exists() else {'status':'incomplete'}
        records.append({**by_attempt[phase['attempt_id']],**record})
    summary = summarize_learning(records,phases)
    freeze = result.get('freeze') or {}
    summary['mechanism_closed_loop_verified'] = bool(
        (freeze.get('generation_link') or {}).get('proven') is True and
        summary['arms']['rpe_learned']['candidate_direct_successes']==16 and
        summary['model_identity_coverage']==48 and summary['model_identities_consistent'])
    return {**result,**summary,'registration_sha256':plan['sha256']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--timeout',type=int,default=180)
    parser.add_argument('--formal',action='store_true',help='explicitly enable frozen production sessions')
    parser.add_argument('--preregister-only',action='store_true')
    parser.add_argument('--max-sessions',type=int,default=48)
    args = parser.parse_args(argv)
    report = (preregister_learning(args.root,timeout=args.timeout) if args.preregister_only else
              run_learning_comparison(args.root,timeout=args.timeout,formal=args.formal,max_sessions=args.max_sessions))
    print(_json(report))
    return 0


if __name__=='__main__':
    sys.exit(main())
