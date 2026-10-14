"""Frozen, bounded natural-task comparison; import and registration never call a model.

Only ``run_address_comparison`` invokes the host. Six public tasks cover four
production artifacts and two directed contract probes, not a population sample.
Oracle plans stay in the controller and are never passed as required_request.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time

from .cost_ledger import PhaseLedger, summarize_phases
from .skill_comparison import exact_json
from ..path_binding import read_regular_file
from ..skill_catalog import SkillCatalog, XLSX_SKILL_ID
from ..tasks import plan_from_dict, plan_from_request, verify_plan

ARMS = ('native', 'address')
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_MARKER = '.rpe-address-comparison-owned.json'


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)


def _safe_path(value):
    original = Path(value).expanduser()
    if '..' in original.parts:
        raise ValueError('paths cannot contain traversal')
    path = Path(os.path.abspath(original))
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError('paths cannot contain symlinks')
    return path


def _save(path, value):
    path = _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {**value, 'sha256': _sha(_json(value).encode())}
    with path.open('x', encoding='utf-8') as stream:
        stream.write(_json(document))
        stream.flush()
        os.fsync(stream.fileno())
    return document


def _read(path):
    value = json.loads(read_regular_file(_safe_path(path), max_bytes=8 * 1024 * 1024))
    saved = value.pop('sha256', None)
    if saved != _sha(_json(value).encode()):
        raise ValueError('frozen registration or evidence changed')
    return {**value, 'sha256': saved}


def _source_hashes():
    package = Path(__file__).resolve().parents[1]
    return {p.relative_to(package).as_posix(): _sha(p.read_bytes())
            for p in sorted(package.rglob('*.py'))}


def _python_metadata(python):
    code = '''import hashlib,importlib.metadata,json,pathlib,re,sys
rows=sorted((re.sub(r"[-_.]+","-",str(d.metadata.get("Name") or "")).lower(),str(d.version))
            for d in importlib.metadata.distributions())
encoded=json.dumps(rows,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()
print(json.dumps({"version":sys.version,"implementation":sys.implementation.name,
 "executable_sha256":hashlib.sha256(pathlib.Path(sys.executable).read_bytes()).hexdigest(),
 "executable_path_sha256":hashlib.sha256(sys.executable.encode()).hexdigest(),
 "prefix_sha256":hashlib.sha256(sys.prefix.encode()).hexdigest(),
 "installed_distribution_count":len(rows),"installed_distributions_sha256":hashlib.sha256(encoded).hexdigest()}))'''
    completed = subprocess.run([str(python),'-B','-c',code],capture_output=True,text=True,timeout=15,check=True)
    return json.loads(completed.stdout)


def _host_fingerprint(hermes_root):
    """Read source/dependency metadata only; exclude daily state and credentials."""
    root = _safe_path(hermes_root)
    if not root.is_dir():
        return {'available':False,'status':'host_checkout_absent'}
    def git(*arguments):
        process = subprocess.run(['git','-c','core.fsmonitor=false','--no-optional-locks','-C',str(root),
            *arguments],capture_output=True,timeout=20,check=True)
        return process.stdout
    try:
        head = git('rev-parse','--verify','HEAD').decode('ascii').strip()
    except subprocess.CalledProcessError:
        return {'available':False,'status':'host_git_head_unavailable'}
    private_dirs = ('memory','memories','.hermes','credentials','secrets')
    private_files = ('.env*','auth.json','credentials.json','tokens.json','*.pem','*.key','*.p12')
    exclusions = [f':(exclude,glob){prefix}{name}/**' for name in private_dirs for prefix in ('','**/')]
    exclusions += [f':(exclude,glob){prefix}{name}' for name in private_files for prefix in ('','**/')]
    dirty = git('diff','--no-ext-diff','--no-textconv','--binary','HEAD','--','.',*exclusions)
    names = git('ls-files','--others','--exclude-standard','-z','--',
                '*.py','*.pyi','*.pth','*.so','*.dylib').split(b'\0')
    modules = []
    for encoded in names:
        if not encoded:
            continue
        relative = Path(os.fsdecode(encoded))
        if any(part in private_dirs or part.startswith('.env') for part in relative.parts):
            continue
        if len(modules)>=2048:
            raise ValueError('host untracked runtime module budget exceeded')
        path = _safe_path(root/relative)
        if not path.is_relative_to(root):
            raise ValueError('host runtime module escaped checkout')
        modules.append((relative.as_posix(),_sha(read_regular_file(path,max_bytes=MAX_ARTIFACT_BYTES))))
    result = {'available':False,'status':'host_interpreter_unavailable','git_head':head,
        'tracked_diff_sha256':_sha(dirty),'tracked_diff_bytes':len(dirty),
        'tracked_diff_scope':'all tracked repository files except memory, state and credential paths',
        'untracked_runtime_sha256':_sha(_json(sorted(modules)).encode()),'untracked_runtime_count':len(modules)}
    bootstrap = root/'.venv/bin/python'
    if bootstrap.is_file():
        from ..integrations.hermes_session import _installed_python
        # Host PM selection is read-only; it reads dependency facts, not user
        # memory, provider settings or credentials. Freeze the exact interpreter
        # used by the launcher as well as its bootstrap environment.
        selected = _installed_python(root)
        result.update(available=True,status='ready',bootstrap_python=_python_metadata(bootstrap),
                      selected_python=_python_metadata(selected))
    return result


def _environment(helper_python,hermes_root):
    completed = subprocess.run([helper_python, '-B', '-c',
        'import json,sys,openpyxl; print(json.dumps({"python":sys.version,"openpyxl":openpyxl.__version__}))'],
        capture_output=True, text=True, timeout=15, check=True)
    config = Path.home() / '.hermes/config.yaml'
    return {'source_sha256': _source_hashes(),
        'host_config_sha256': _sha(read_regular_file(config, max_bytes=1048576)) if config.exists() else None,
        'dependency_versions': json.loads(completed.stdout),'helper_python_identity':_python_metadata(helper_python),
        'host_runtime':_host_fingerprint(hermes_root)}


def _fixtures(directory):
    # All truth below is literal and hand checked. It is not generated by RPE.
    cases = [
        {'case_id':'csv-zh-composite','capability':'tabular.deduplicate','input_name':'input.csv',
         'output_name':'output.csv','expected_status':'ready',
         'source_text':'地区,编号,金额\n华东,001,10\n华西,001,20\n华东,001,99\n华东,1,30\n',
         'parameters':{'key_columns':['地区','编号'],'delimiter':',','encoding':'utf-8','keep':'first'},
         'constraints':['preserve_original','preserve_order'],
         'expected_rows':[['地区','编号','金额'],['华东','001','10'],['华西','001','20'],['华东','1','30']],
         'task':'请把 input.csv 里重复的交易清掉：地区和编号合起来才算同一笔。保留第一次出现的整行及原来顺序；编号是原始字符串，001与1不同。输入是UTF-8逗号CSV，结果另存 output.csv。'},
        {'case_id':'tsv-tab','capability':'tabular.deduplicate','input_name':'input.csv',
         'output_name':'output.csv','expected_status':'ready',
         'source_text':'code\tnote\nA\tfirst\nB\tsecond\nA\tlater\n',
         'parameters':{'key_columns':['code'],'delimiter':'\t','encoding':'utf-8','keep':'first'},
         'constraints':['preserve_original','preserve_order'],
         'expected_rows':[['code','note'],['A','first'],['B','second']],
         'task':'input.csv 实际是以单个制表符TAB分隔的UTF-8表格，请按code的精确字符串去重，保留首条及原行顺序、表头和所有单元格，仍以真实TAB分隔写入 output.csv；不要把反斜杠加t当分隔符。'},
        {'case_id':'json-lossless','capability':'json.normalize','input_name':'input.json',
         'output_name':'output.json','expected_status':'ready',
         'source_text':' { "名字" : "小雨", "金额" : 1.20e+2, "flags" : [ true, null, false ], "nested" : {"z":2,"a":"\\u4e2d"} } \n',
         'parameters':{'style':'minify'},'constraints':['preserve_original','preserve_order'],
         'expected_text':'{"名字":"小雨","金额":1.20e+2,"flags":[true,null,false],"nested":{"z":2,"a":"\\u4e2d"}}\n',
         'task':'把 input.json 规范化为紧凑JSON存到 output.json，结尾保留一个换行。只调整结构空白，不排序，不改变键/数组顺序、数值原始写法、字符串转义或任何值。'},
        {'case_id':'xlsx-original','capability':'xlsx.inspect','input_name':'input.xlsx',
         'output_name':'output.json','expected_status':'ready','parameters':{'mode':'rows','sheet':'明细'},
         'constraints':['preserve_original'],
         'expected':{'sheet':'明细','rows':[['编号','金额','确认'],['001',7,True],['中文',2.5,False]]},
         'task':'请只读检查 input.xlsx 的“明细”工作表，把全部行按原始XLSX技能 xlsx_read.py 的 --json --sheet 明细 输出结构保存为 output.json。先用原生技能加载入口阅读已安装xlsx技能，再使用其原始helper（专门任务入口可经其已审核adapter调用），不可自行重写helper。保留JSON类型、字段与行顺序。'},
        {'case_id':'missing-keys','capability':'tabular.deduplicate','input_name':'input.csv',
         'output_name':'output.csv','expected_status':'needs_context','constraints':['preserve_original'],
         'source_text':'姓名,手机号,订单号\n小王,13800000001,A01\n小王,13800000002,A02\n小李,13800000001,A03\n',
         'expected_missing':['execution.key_columns'],
         'task':'input.csv 需要按业务唯一标识去重保留首条，结果将来写 output.csv。但我还没有决定唯一标识按哪一列或哪几列组合；请先确认缺什么，不能擅自猜列或产出业务结果。'},
        {'case_id':'unsupported-sum','capability':'tabular.aggregate','input_name':'input.csv',
         'output_name':'output.csv','expected_status':'unsupported','constraints':['preserve_original'],
         'source_text':'地区,金额\n东,10\n西,20\n东,30\n',
         'task':'本次授权的业务能力仅包括保留首条去重、JSON空白规范化、XLSX只读检查，不能新增其他业务执行器。现在请按 input.csv 的地区求金额合计到 output.csv。若超出授权能力应明确拒识，不能把求和改写成去重。'},
    ]
    from openpyxl import Workbook
    for case in cases:
        suffix = Path(case['input_name']).suffix
        source = directory / (case['case_id'] + suffix)
        if case['case_id'] == 'xlsx-original':
            book = Workbook()
            book.active.title = '其他'
            book.active.append(['不要读取此表'])
            ws = book.create_sheet('明细')
            for row in case['expected']['rows']:
                ws.append(row)
            book.save(source)
            book.close()
        else:
            source.write_bytes(case['source_text'].encode('utf-8'))
        case.update(source_path=str(source), input_sha256=_sha(source.read_bytes()),
                    category='production' if case['expected_status']=='ready' else 'contract_probe')
    return cases


def _prompt(case, helper_python):
    terminal = ('缺少信息或不支持时，用可用工具发布结构化终态：status为needs_context或unsupported，'
        '缺信息时同时列出missing字段名；原生文件工具写terminal.json，专用入口使用其持久终态。'
        '只写自然语言说明不算结构化终态。') if case['category']=='contract_probe' else ''
    return (case['task'] + '\n保留所有输入文件，输出只能写到指定新路径，不得覆盖。'
        '可用已有的terminal、file、技能搜索/加载及专用能力入口；不安装依赖，不训练或升级技能。'
        f'已有openpyxl的Python解释器：{helper_python}。'
        + terminal +
        '不得读取控制器、数据库、其他会话、实验报告、RPE实现或标准答案。')


def preregister_address_comparison(root, *, skill_root, hermes_root=None,
                                   helper_python=None, timeout=120, max_turns=8):
    """Freeze six paired public tasks, dependencies and at most two separate pilots.

    ``root`` must be empty or already owned by this exact registration. The real
    original XLSX skill is snapshotted and its existing registered adapter trusted
    only in this owned database; no daily Hermes configuration is modified.
    """
    if type(timeout) is not int or not 1 <= timeout <= 180:
        raise ValueError('timeout must be an integer between 1 and 180')
    if type(max_turns) is not int or not 1 <= max_turns <= 12:
        raise ValueError('max_turns must be an integer between 1 and 12')
    root, skill_root = _safe_path(root), _safe_path(skill_root)
    hermes_root = _safe_path(hermes_root or Path.home()/'.hermes/hermes-agent')
    # Preserve venv identity even when its interpreter is a symlink.
    helper_python = str(Path(helper_python or sys.executable).expanduser().absolute())
    arguments = {'root':str(root),'skill_root':str(skill_root),'hermes_root':str(hermes_root),
                 'helper_python':helper_python,'timeout':timeout,'max_turns':max_turns}
    if (root/'preregistration.json').exists():
        plan = _read(root/'preregistration.json')
        if any(plan.get(k)!=v for k,v in arguments.items()):
            raise ValueError('frozen arguments differ; register a new root')
        _check_freeze(plan)
        return plan
    if root.exists() and any(root.iterdir()):
        raise ValueError('comparison root must be empty or already owned')
    if root == Path(root.anchor) or root == Path.home() or root == skill_root or root in skill_root.parents:
        raise ValueError('comparison requires a dedicated owned root')
    root.mkdir(parents=True, exist_ok=True)
    _save(root/_MARKER, {'schema_version':'rpe.address-comparison-owner/v1','root':str(root)})
    for name in ('fixtures','records','workspaces','sessions','catalog','databases'):
        (root/name).mkdir()
    ledger = PhaseLedger(root/'ledger')
    assert not ledger.records()
    database = root/'runs.sqlite'
    catalog = SkillCatalog(database, root/'catalog')
    snapshot = catalog.sync(skill_root)['snapshot_id']
    binding = catalog.trust_adapter(XLSX_SKILL_ID, snapshot)
    skill = catalog.lookup(XLSX_SKILL_ID, snapshot)
    cases = _fixtures(root/'fixtures')
    trials, pilots = [], []
    for index, case in enumerate(cases):
        arms = ARMS if index%2==0 else tuple(reversed(ARMS))
        for arm in arms:
            trials.append(_trial(root,case,arm,'formal',helper_python,binding))
    for arm in ARMS:
        pilots.append(_trial(root,cases[0],arm,'pilot',helper_python,binding))
    # The observer needs database access inside its sandbox. A separate database
    # per session prevents that access from exposing another arm's run history.
    for trial in trials+pilots:
        trial['db'] = str(root/'databases'/(trial['trial_id']+'.sqlite'))
        with sqlite3.connect(database) as original, sqlite3.connect(trial['db']) as target:
            original.backup(target)
    plan = {**arguments,'schema_version':'rpe.address-comparison/v1','db':str(database),
        'catalog_root':str(root/'catalog'),'skill_snapshot_id':snapshot,
        'skill_sha256':skill['skill_sha256'],'script_sha256':skill['script_sha256'],
        'environment':_environment(helper_python,hermes_root),'cases':cases,'sessions':trials,'pilots':pilots,
        'formal_session_limit':12,'pilot_session_limit':2,'production_max_task_attempts':1,
        'model_policy':'same frozen host configuration and dependencies; record actual identities',
        'tool_policy':'native terminal/file/skills/tool_search preserved; address catalog plus two-stage tools',
        'failure_policy':'all reservations, failures, unknowns and pilots retained; never replay or replace',
        'cost_policy':'all observed phases summed once; absent usage total null, known lower bound separate; no billing estimate',
        'missing_field_aliases':{'key_columns':'execution.key_columns'},
        'coverage_limits':[
            'six fixed public tasks only; four production artifacts and two directed contract probes',
            'one XLSX rows case; no claims about all modes, all paraphrases or broad populations',
            'TSV uses actual TAB bytes in input.csv under the existing CSV contract; .tsv extension admission is not tested',
            'unsupported probe tests explicit three-capability authorization, not native Python ability',
            'native refusal has no RPE state; only observed structured publication can verify it',
            'tool policies differ; compare whole strategies, never attribute gains to one module',
            'no long-term learning or upgraded skills; no repeated formal sessions or post-failure retuning']}
    return _save(root/'preregistration.json',plan)


def _trial(root, case, arm, phase, helper_python, binding):
    trial_id = f"{phase}-{case['case_id']}-{arm}"
    workspace = root/'workspaces'/trial_id
    workspace.mkdir()
    source = workspace/case['input_name']
    source.write_bytes(read_regular_file(Path(case['source_path']),max_bytes=MAX_ARTIFACT_BYTES))
    prompt = _prompt(case,helper_python)
    (workspace/'prompt.txt').write_text(prompt,encoding='utf-8')
    trial = {'trial_id':trial_id,'phase':phase,'case_id':case['case_id'],'arm':arm,
        'workspace':str(workspace),'input_name':case['input_name'],'input_sha256':case['input_sha256'],
        'prompt_sha256':_sha(prompt.encode()),'category':case['category']}
    if case['expected_status']=='ready':
        params = {'input_path':str(source),'output_path':str(workspace/case['output_name']),**case['parameters']}
        if case['capability']=='xlsx.inspect':
            params['skill_binding'] = binding
        request = {'schema_version':2,'event_id':trial_id,'capability':case['capability'],
                   'constraints':case['constraints'],'goal':case['task'],'execution':params}
        trial['oracle_plan'] = plan_from_request(request).to_dict()
    return trial


def _load(root):
    root = _safe_path(root)
    marker = _read(root/_MARKER)
    if marker.get('root') != str(root):
        raise ValueError('owned root marker differs')
    plan = _read(root/'preregistration.json')
    if plan['root'] != str(root):
        raise ValueError('frozen root differs')
    return plan


def _check_freeze(plan):
    if _environment(plan['helper_python'],plan['hermes_root']) != plan['environment']:
        raise ValueError('frozen source, model configuration or dependencies changed')
    catalog = SkillCatalog(plan['db'],plan['catalog_root'])
    row = catalog.lookup(XLSX_SKILL_ID,plan['skill_snapshot_id'])
    if not row['executable'] or row['script_sha256']!=plan['script_sha256'] or row['skill_sha256']!=plan['skill_sha256']:
        raise ValueError('frozen skill snapshot or current trust changed')
    for case in plan['cases']:
        if _sha(read_regular_file(_safe_path(case['source_path']),max_bytes=MAX_ARTIFACT_BYTES)) != case['input_sha256']:
            raise ValueError('frozen public input changed')
    for trial in plan['sessions']+plan['pilots']:
        workspace = _safe_path(trial['workspace'])
        if not workspace.is_relative_to(plan['root']):
            raise ValueError('frozen workspace escaped owned root')
        if _sha(read_regular_file(workspace/'prompt.txt',max_bytes=1048576)) != trial['prompt_sha256']:
            raise ValueError('frozen prompt changed')
        row = SkillCatalog(trial['db'],plan['catalog_root']).lookup(XLSX_SKILL_ID,plan['skill_snapshot_id'])
        if not row['executable']:
            raise ValueError('frozen per-session skill trust changed')


def score_address_artifact(plan, trial):
    """Business truth and existing independent product oracle, without host claims."""
    case = next(c for c in plan['cases'] if c['case_id']==trial['case_id'])
    workspace = _safe_path(trial['workspace'])
    verdict = {'business_correct':False,'product_oracle_passed':False,
        'source_preserved':False,'artifact_sha256':None,'structured_terminal':None}
    try:
        source = read_regular_file(workspace/case['input_name'],max_bytes=MAX_ARTIFACT_BYTES)
        verdict['source_preserved'] = _sha(source)==case['input_sha256']
        if case['expected_status']!='ready':
            # A controlled refusal publishes no business output, including aliases.
            outputs = [p for p in workspace.iterdir() if p.name.startswith('output')]
            if outputs or not verdict['source_preserved']:
                return verdict
            raw = read_regular_file(workspace/'terminal.json',max_bytes=8192)
            terminal = _terminal_value(raw,case)
            if terminal is not None:
                verdict.update(business_correct=True,structured_terminal=terminal,terminal_sha256=_sha(raw))
            return verdict
        artifact = read_regular_file(workspace/case['output_name'],max_bytes=MAX_ARTIFACT_BYTES)
        verdict['artifact_sha256'] = _sha(artifact)
        if 'expected_rows' in case:
            actual = list(csv.reader(io.StringIO(artifact.decode('utf-8'),newline=''),
                                    delimiter=case['parameters']['delimiter'],strict=True))
            correct = exact_json(actual,case['expected_rows'])
        elif 'expected_text' in case:
            correct = artifact==case['expected_text'].encode('utf-8')
        else:
            correct = exact_json(json.loads(artifact,object_pairs_hook=_unique_pairs),case['expected'])
        product = plan_from_dict(trial['oracle_plan'])
        verification = verify_plan({**product.verification_parameters(),
            'staging_path':str(workspace/case['output_name'])},product)
        verdict.update(business_correct=bool(correct and verdict['source_preserved']),
            product_oracle_passed=verification.get('passed') is True
                and verification.get('output_sha256')==verdict['artifact_sha256'])
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,csv.Error,RecursionError):
        pass
    return verdict


def _unique_pairs(pairs):
    value = {}
    for key,item in pairs:
        if key in value:
            raise ValueError('duplicate JSON fields')
        value[key] = item
    return value


def _terminal_value(raw,case):
    terminal = json.loads(raw,object_pairs_hook=_unique_pairs)
    valid = type(terminal) is dict and terminal.get('status')==case['expected_status']
    if not valid or set(terminal)-{'status','missing'}:
        return None
    if case['expected_status']=='needs_context':
        valid = _missing_fields(terminal.get('missing'))==case['expected_missing']
    elif 'missing' in terminal:
        valid = terminal['missing']==[]
    return terminal if valid else None


def _native_calls(result, database):
    """Validate durable API/model/tool/stream identity and argument links."""
    from ..integrations.hermes_gate import _persisted_events, digest_json
    events = result.get('events') or []
    if not events or events!=_persisted_events(database,result.get('session_id')):
        raise ValueError('native_durable_observations_missing')
    fields = ('session_id','task_id','turn_id','api_request_id')
    def api(event):
        identity = tuple(event.get(k) for k in fields)
        if any(not isinstance(v,str) or not v for v in identity):
            raise ValueError('native_api_identity_missing')
        return identity
    def tool(event):
        identity = (*api(event),event.get('tool_call_id'),event.get('tool_name'))
        if any(not isinstance(v,str) or not v for v in identity):
            raise ValueError('native_tool_identity_missing')
        return identity
    requests = [e for e in events if e.get('kind')=='pre_api_request']
    outcomes = [e for e in events if e.get('kind') in ('post_api_request','api_request_error')]
    counters = Counter(map(api,requests))
    if (not counters or counters!=Counter(map(api,outcomes)) or any(n!=1 for n in counters.values())
            or any(e.get('kind')=='api_request_error' for e in outcomes)):
        raise ValueError('native_api_linkage_invalid')
    pres = [e for e in events if e.get('kind')=='pre_tool_call']
    posts = [e for e in events if e.get('kind')=='post_tool_call']
    emitted = Counter((*api(e),c.get('tool_call_id'),c.get('tool_name'))
                      for e in outcomes for c in e.get('model_tool_calls',[]))
    observed = Counter(map(tool,pres))
    if not observed or observed!=Counter(map(tool,posts)) or observed!=emitted or any(n!=1 for n in observed.values()):
        raise ValueError('native_tool_linkage_invalid')
    def with_arguments(event):
        digest = event.get('arguments_sha256')
        if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('native_arguments_digest_missing')
        return (tool(event),digest)
    if Counter(map(with_arguments,pres))!=Counter(map(with_arguments,posts)):
        raise ValueError('native_arguments_linkage_invalid')
    streams = result.get('stream_tool_calls') or []
    signatures = Counter((e['tool_name'],e['arguments_sha256']) for e in pres)
    if signatures!=Counter((c.get('name'),digest_json(c.get('input'))) for c in streams):
        raise ValueError('native_stream_linkage_invalid')
    return posts,streams


def evaluate_native_address_session(result, plan, trial):
    """Native has its own tools: verify observations, never infer them from prose."""
    from ..integrations.hermes_gate import digest_json
    case = next(c for c in plan['cases'] if c['case_id']==trial['case_id'])
    verdict = {'verified':False,'status':'unverified','errors':[],'state_evidence':None}
    try:
        posts,streams = _native_calls(result,trial['db'])
        if case['capability']=='xlsx.inspect' and case['expected_status']=='ready':
            from .skill_comparison import _native_gate
            old_plan = {**plan,'db':trial['db'],'snapshot_id':plan['skill_snapshot_id'],'owned_root':plan['root']}
            old_case = {**case,**case['parameters']}
            legacy = _native_gate(result,old_plan,trial,old_case)
            if legacy.get('verified') or legacy.get('errors')!=['native_original_helper_execution_missing']:
                return legacy
            return _native_xlsx_flags(result,plan,trial,case,posts,streams,legacy)
        terminal = case['expected_status']!='ready'
        name = 'terminal.json' if terminal else case['output_name']
        workspace = Path(trial['workspace'])
        target = workspace/name
        raw = read_regular_file(target,max_bytes=MAX_ARTIFACT_BYTES)
        for stream in streams:
            args = stream.get('input')
            if type(args) is not dict:
                continue
            matching = [e for e in posts if e.get('tool_name')==stream.get('name')
                and e.get('arguments_sha256')==digest_json(args)]
            if stream.get('name')=='write_file':
                path = Path(args.get('path',''))
                path = path if path.is_absolute() else workspace/path
                if path.absolute()!=target or not isinstance(args.get('content'),str) or args['content'].encode()!=raw:
                    continue
                writes = [w for w in result.get('file_write_observations',[])
                    if w.get('tool_call_id')==stream.get('tool_call_id')]
                if len(matching)!=1 or len(writes)!=1:
                    continue
                write = writes[0]
                if (matching[0].get('status') not in ('ok','succeeded') or write.get('verified') is not True
                    or write.get('bytes_written')!=len(raw) or write.get('content_sha256')!=_sha(raw)
                    or write.get('arguments_sha256')!=digest_json(args)
                    or write.get('target_path_sha256')!=_sha(str(target).encode())):
                    continue
            elif stream.get('name')=='terminal' and not terminal:
                # Business bytes are independently checked. This only establishes
                # an observed successful native command naming its output.
                if name not in _json(args) or not any(e.get('status')=='succeeded'
                    and type(e.get('tool_exit_code')) is int and e['tool_exit_code']==0 for e in matching):
                    continue
            else:
                continue
            verdict.update(verified=True,status='accepted_rejection' if terminal else 'accepted',
                rejection_reason=case['expected_status'] if terminal else None)
            return verdict
        raise ValueError('native_bound_structured_publication_missing' if terminal else 'native_output_invocation_missing')
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error) as error:
        verdict['errors'].append(str(error) if isinstance(error,ValueError) else type(error).__name__)
    return verdict


def _native_xlsx_flags(result,plan,trial,case,posts,streams,legacy):
    """Extend the already checked legacy chain only for Python -u/-B flags.

    Reaching this function means the legacy verifier checked durable API/tool
    identities, arguments, snapshot bytes and existence of an original load.
    Order must still be checked for the particular terminal selected below.
    No events or argument hashes are rewritten to simulate another invocation.
    """
    from ..integrations.hermes_gate import digest_json, _same_call
    workspace = Path(trial['workspace'])
    helper = Path(result['session_root'])/'home/skills/productivity/xlsx/scripts/xlsx_read.py'
    events = result['events']
    row = SkillCatalog(trial['db'],plan['catalog_root']).lookup(XLSX_SKILL_ID,plan['skill_snapshot_id'])
    loaded = [post for post in posts if post.get('tool_name')=='skill_view'
        and post.get('status')=='loaded' and post.get('skill_id')==XLSX_SKILL_ID
        and post.get('snapshot_id')==plan['skill_snapshot_id']
        and post.get('skill_source_path')==str(helper.parent.parent/'SKILL.md')
        and post.get('content_sha256')==plan['skill_sha256']
        and post.get('arguments_sha256')==digest_json({'name':row['load_name']})]
    if _python_metadata(plan['helper_python'])!=plan['environment']['helper_python_identity']:
        return {**legacy,'errors':['native_frozen_interpreter_changed']}
    for stream in streams:
        args = stream.get('input')
        if stream.get('name')!='terminal' or not isinstance(args,dict):
            continue
        command = args.get('command')
        if not isinstance(command,str) or any(c in command for c in ('$', '`', '\n', '\r')):
            continue
        lexer = shlex.shlex(command,posix=True,punctuation_chars=';&|><')
        lexer.whitespace_split,lexer.commenters = True,''
        tokens = list(lexer)
        if tokens[:1]==['cd']:
            if len(tokens)<4 or Path(tokens[1]).resolve()!=workspace or tokens[2]!='&&':
                continue
            tokens = tokens[3:]
        if tokens[:1]!=[plan['helper_python']]:
            continue
        tokens = tokens[1:]
        flags = []
        while tokens and tokens[0] in {'-B','-u','-Bu','-uB'} and len(flags)<4:
            flags.append(tokens.pop(0))
        mode = {'rows':'--json','sheets':'--sheets','formulas':'--formulas'}[case['parameters']['mode']]
        mode_args = [mode]+(['--sheet',case['parameters']['sheet']] if case['parameters'].get('sheet') is not None else [])
        if (len(tokens)!=len(mode_args)+4 or tokens[0]!=str(helper)
            or tokens[1] not in {'input.xlsx',str(workspace/'input.xlsx')}
            or tokens[2:-2]!=mode_args or tokens[-2]!='>'
            or tokens[-1] not in {'output.json',str(workspace/'output.json')}):
            continue
        matching = [post for post in posts if post.get('tool_name')=='terminal'
            and post.get('arguments_sha256')==digest_json(args)
            and ('tool_call_id' not in stream or post.get('tool_call_id')==stream['tool_call_id'])]
        if len(matching)!=1 or matching[0].get('status')!='succeeded':
            continue
        pres = [event for event in events if event.get('kind')=='pre_tool_call'
            and event.get('tool_name')=='terminal' and _same_call(event,matching[0])
            and event.get('arguments_sha256')==digest_json(args)]
        if len(pres)!=1 or not any(events.index(load)<events.index(pres[0]) for load in loaded):
            continue
        if matching[0].get('tool_exit_code') not in (None,0):
            continue
        return {'verified':True,'status':'accepted','errors':[],'skill_id':XLSX_SKILL_ID,
            'snapshot_id':plan['skill_snapshot_id'],'script_sha256':plan['script_sha256'],
            'helper_call_id':matching[0]['tool_call_id'],'python_startup_flags':flags,
            'interpreter_sha256':plan['environment']['helper_python_identity']['executable_sha256']}
    return {**legacy,'invocation_status':'unrecognized_python_invocation'}


def _address_contract(plan,trial,gate):
    """Product run parameters, not model summaries, determine faithful execution."""
    from ..integrations.hermes_gate import read_run
    from ..tasks import contract_from_plan
    if trial['category']=='contract_probe':
        case = next(c for c in plan['cases'] if c['case_id']==trial['case_id'])
        return bool(gate.get('rejection_verified') is True and not gate.get('errors')
            and gate.get('status')=='accepted_rejection' and gate.get('rejection_reason')==case['expected_status']
            and (case['expected_status']!='needs_context' or _missing_fields(gate.get('missing'))==case['expected_missing']))
    if not gate.get('verified'):
        return False
    run_id = gate.get('run_id')
    if not run_id:
        return False
    try:
        run = read_run(trial['db'],run_id)
        actual = run['event']
        if 'request' in actual:
            actual = actual['request']
        from ..integrations.hermes_gate import contract_for
        return exact_json(contract_for(actual,trial['workspace']),
                          contract_from_plan(plan_from_dict(trial['oracle_plan'])))
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
        return False


def _public_gate(gate):
    # Host gates may grow private diagnostics. Persist a bounded allowlist only.
    result = {}
    for key in ('verified','harness_verified','rejection_verified','status','run_id','state_id','capability',
                'catalog_sha256','state_sha256','handoff_sha256','handoff_bytes','rejection_reason',
                'task_context_sha256','task_context_bytes','context_minimized','invocation_status','interpreter_sha256'):
        value = gate.get(key)
        if type(value) in (str,int,bool) or value is None:
            result[key] = value
    result['error_count'] = len(gate.get('errors') or [])
    result['error_codes'] = [value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value)
                             else 'redacted_diagnostic' for value in (gate.get('errors') or [])[:16]]
    if isinstance(gate.get('python_startup_flags'),list):
        result['python_startup_flags'] = [flag for flag in gate['python_startup_flags'] if flag in {'-B','-u','-Bu','-uB'}]
    return result


def _missing_fields(value):
    # Native has no RPE schema prefix. This one explicit alias preserves the
    # same business question; unknown or extra missing fields still fail.
    if not isinstance(value,list) or any(not isinstance(v,str) for v in value):
        return None
    return ['execution.key_columns' if v=='key_columns' else v for v in value]


def _observed_identity(value):
    return bool(isinstance(value,dict) and isinstance(value.get('provider'),str)
        and value.get('provider') and isinstance(value.get('requested_model'),str)
        and value.get('requested_model') and isinstance(value.get('response_models'),list)
        and value['response_models'] and all(isinstance(v,str) and v for v in value['response_models']))


def _denied(plan,trial):
    root = Path(plan['root'])
    result = [root/'fixtures',root/'records',root/'ledger',root/'preregistration.json',root/_MARKER,
              Path(__file__).resolve(),*(Path(t['workspace']) for t in plan['sessions']+plan['pilots']
                                         if t['trial_id']!=trial['trial_id'])]
    if trial['arm']=='native':
        from ..evolution.worker import _deny_read_roots
        result.extend(_deny_read_roots())
    return list(dict.fromkeys(result))


def _observe(plan,trial,runner):
    from ..integrations.hermes_session import SessionObservationError
    root = Path(plan['root'])
    workspace = Path(trial['workspace'])
    case = next(c for c in plan['cases'] if c['case_id']==trial['case_id'])
    record = {'trial_id':trial['trial_id'],'phase':trial['phase'],'arm':trial['arm'],
        'case_id':trial['case_id'],'category':trial['category'],'fingerprint':plan['sha256'],
        'status':'failed','accepted':False,'business_correct':False,'product_oracle_passed':False,
        'contract_faithful':False,'invocation_evidence':False,'source_preserved':False,
        'usage':None,'model_identity':None,'billed_cost':None,'state_evidence':None,
        'model_visible_handoff_bytes':None,'session_id':None,'elapsed_seconds':None}
    result,observation_failed = {},False
    started = time.monotonic()
    try:
        source = read_regular_file(workspace/trial['input_name'],max_bytes=MAX_ARTIFACT_BYTES)
        if _sha(source)!=trial['input_sha256']:
            raise ValueError('frozen_workspace_input_changed')
        kwargs = {'tool_enabled':trial['arm']=='address','owner_root':root/'sessions'/trial['trial_id'],
            'timeout':plan['timeout'],'hermes_root':plan['hermes_root'],
            'entry_mode':'address' if trial['arm']=='address' else 'optional',
            'max_task_attempts':1,'max_turns':plan['max_turns'],'catalog_root':plan['catalog_root'],
            'skill_snapshot_id':plan['skill_snapshot_id'],'context_sources':[],
            'read_isolation_root':root,'deny_read_roots':_denied(plan,trial)}
        if trial['arm']=='native':
            kwargs['preserve_native_tool_search'] = True
        try:
            result = runner((workspace/'prompt.txt').read_text(),workspace,trial['db'],**kwargs)
        except SessionObservationError as error:
            result,observation_failed = error.diagnostics,True
        if not isinstance(result,dict):
            raise TypeError('session result must be structured')
        identity = result.get('model_identity')
        public_identity = ({k:identity[k] for k in ('provider','requested_model','response_models')
                            if k in identity} if isinstance(identity,dict) else None)
        record.update(session_id=result.get('session_id'),usage=result.get('usage'),
            model_identity=public_identity,elapsed_seconds=result.get('elapsed_seconds'),
            original_prompt_sha256=result.get('original_prompt_sha256'),
            actual_prompt_sha256=result.get('prompt_sha256'),
            process_cleanup=result.get('process_cleanup'))
        record.update(score_address_artifact(plan,trial))
        if trial['arm']=='address':
            from ..integrations.hermes_address import evaluate_address_session
            gate = evaluate_address_session(result,trial['db'],workspace,catalog_root=plan['catalog_root'])
            record['contract_faithful'] = _address_contract(plan,trial,gate)
            # A persisted rejection is the address structured terminal. A file is
            # optional; absence of business artifacts and preserved input remain mandatory.
            if trial['category']=='contract_probe' and record['contract_faithful']:
                no_output = not any(p.name.startswith('output') for p in workspace.iterdir())
                record['business_correct'] = bool(no_output and record['source_preserved'])
        else:
            gate = evaluate_native_address_session(result,plan,trial)
            record['contract_faithful'] = bool(gate.get('verified') and record['business_correct'])
        observed = gate.get('rejection_verified') if trial['category']=='contract_probe' and trial['arm']=='address' else gate.get('verified')
        record.update(gate=_public_gate(gate),invocation_evidence=observed is True)
        record['state_evidence'] = {k:gate[k] for k in ('state_id','catalog_sha256','state_sha256','handoff_sha256')
                                    if isinstance(gate.get(k),str)} or None
        record['minimal_rpc_handoff_bytes'] = gate.get('handoff_bytes')
        # Only a raw API projection verified by the host gate establishes the
        # bytes visible to the second model node. A local state length is not it.
        if gate.get('context_minimized') is True:
            record['model_visible_handoff_bytes'] = gate.get('task_context_bytes')
            record['model_visible_handoff_sha256'] = gate.get('task_context_sha256')
        host_ok = bool(result.get('completed') is True and result.get('exit_code')==0
            and not result.get('timed_out') and not result.get('observer_errors') and not observation_failed
            and (result.get('process_cleanup') or {}).get('cleanup_complete') is True)
        record['comparable'] = bool(result.get('original_prompt_sha256')==trial['prompt_sha256']
            and result.get('prompt_sha256')==trial['prompt_sha256']
            and result.get('catalog_snapshot_id')==plan['skill_snapshot_id'] and _observed_identity(public_identity))
        record['accepted'] = bool(host_ok and record['comparable'] and record['business_correct']
            and record['contract_faithful'] and record['invocation_evidence']
            and (trial['category']=='contract_probe' or record['product_oracle_passed']))
        record['status'] = ('timed_out' if result.get('timed_out') else
            'refused' if record['accepted'] and trial['category']=='contract_probe' else
            'completed' if record['accepted'] else 'failed')
    except Exception as error:
        record['error_code'] = type(error).__name__
    if not isinstance(record['elapsed_seconds'],(int,float)) or not math.isfinite(record['elapsed_seconds']) or record['elapsed_seconds']<0:
        record['elapsed_seconds'] = time.monotonic()-started
    # No raw prompt, provider payload, model prose or stream arguments are exported.
    return record


def summarize_address_comparison(root):
    """Read compact evidence and include incomplete reservations as unknown attempts."""
    plan = _load(root)
    ledger = PhaseLedger(Path(plan['root'])/'ledger')
    phases,records = ledger.records(),[]
    known = {t['trial_id']:t for t in plan['sessions']+plan['pilots']}
    for phase in phases:
        if phase['attempt_id'] not in known:
            raise ValueError('unregistered session reservation')
        trial = known[phase['attempt_id']]
        if (phase['arm'],phase['phase'])!=(trial['arm'],trial['phase']):
            raise ValueError('session reservation identity collision')
        path = Path(plan['root'])/'records'/(trial['trial_id']+'.json')
        if path.exists():
            record = _read(path)
            if record['fingerprint']!=plan['sha256']:
                raise ValueError('record preregistration differs')
        else:
            if phase['status']!='incomplete':
                raise ValueError('finished phase lacks compact evidence')
            record = {k:trial[k] for k in ('trial_id','phase','arm','case_id','category')}
            record.update(status='incomplete',accepted=False,error_code='reserved_attempt_not_replayed')
        records.append(record)
    formal = [r for r in records if r['phase']=='formal']
    identities = {json.dumps(r['model_identity'],sort_keys=True) for r in records if r.get('model_identity')}
    by_arm = {}
    for arm in ARMS:
        rows = [r for r in formal if r['arm']==arm]
        by_arm[arm] = {**_score_counts(rows),'formal_attempts':len(rows),
            'production_artifact_successes':sum(r.get('accepted') is True and r['category']=='production' for r in rows),
            'contract_probe_successes':sum(r.get('accepted') is True and r['category']=='contract_probe' for r in rows),
            'business_correct_observations':sum(r.get('business_correct') is True for r in rows),
            'contract_faithful_observations':sum(r.get('contract_faithful') is True for r in rows),
            'invocation_observations':sum(r.get('invocation_evidence') is True for r in rows),
            'minimal_state_observations':sum(r.get('state_evidence') is not None for r in rows),
            'model_visible_handoff_observations':sum(type(r.get('model_visible_handoff_bytes')) is int for r in rows),
            'costs':summarize_phases([p for p in phases if p['arm']==arm]),
            'formal_costs':summarize_phases([p for p in phases if p['arm']==arm and p['phase']=='formal'])}
    pairs = []
    for case in plan['cases']:
        rows = {r['arm']:r for r in formal if r['case_id']==case['case_id']}
        comparable = bool(set(rows)==set(ARMS) and all(r.get('comparable') is True for r in rows.values())
            and rows['native'].get('model_identity')==rows['address'].get('model_identity'))
        pairs.append({'case_id':case['case_id'],'category':case['category'],
            'observed_arms':[arm for arm in ARMS if arm in rows],'comparable':comparable,
            **{arm+'_accepted':rows[arm].get('accepted') is True if arm in rows else None for arm in ARMS}})
    return {'schema_version':'rpe.address-comparison-summary/v1','fingerprint':plan['sha256'],
        **_score_counts(formal),
        'metric_definitions':{
            'production_business_correct':'public business truth and preserved source, independent of host/tool evidence',
            'production_complete_chain':'business truth plus independent product oracle, comparable host and verified invocation/contract',
            'contract_probe_correct':'expected strict structured terminal and no business artifact; separate from production',
            'contract_probe_complete_chain':'expected terminal plus verified native publication or durable address rejection and comparable host',
            'deprecated_aliases':{'production_artifact_successes':'production_complete_chain',
                'business_correct_observations':'production_business_correct + contract_probe_correct (legacy mixed count)',
                'contract_probe_successes':'contract_probe_complete_chain'}},
        'planned_formal_sessions':12,'planned_production_sessions':8,'planned_contract_probes':4,
        'formal_attempts':len(formal),'pilot_attempts':len(records)-len(formal),
        'accepted_sessions':sum(r.get('accepted') is True for r in formal),
        'production_artifact_successes':sum(r.get('accepted') is True and r['category']=='production' for r in formal),
        'contract_probe_successes':sum(r.get('accepted') is True and r['category']=='contract_probe' for r in formal),
        'model_identities_consistent':len(identities)<=1,'model_identity_coverage':sum(_observed_identity(r.get('model_identity')) for r in records),
        'by_arm':by_arm,'pairs':pairs,
        'comparison_ready':len(formal)==12 and all(p['comparable'] for p in pairs) and len(identities)==1,
        'model_visible_handoff_observation_coverage':sum(type(r.get('model_visible_handoff_bytes')) is int for r in formal),
        'costs':summarize_phases(phases),'formal_costs':summarize_phases([p for p in phases if p['phase']=='formal']),
        'pilot_costs':summarize_phases([p for p in phases if p['phase']=='pilot']),
        'coverage_limits':plan['coverage_limits'],'tool_policy':plan['tool_policy'],'records':records}


def _score_counts(records):
    production = [row for row in records if row['category']=='production']
    probes = [row for row in records if row['category']=='contract_probe']
    return {'production_attempts':len(production),'contract_probe_attempts':len(probes),
        'production_business_correct':sum(row.get('business_correct') is True for row in production),
        'production_business_unknown':sum(type(row.get('business_correct')) is not bool for row in production),
        'production_complete_chain':sum(row.get('accepted') is True for row in production),
        'production_oracle_verified':sum(row.get('product_oracle_passed') is True for row in production),
        'production_contract_faithful':sum(row.get('contract_faithful') is True for row in production),
        'production_invocation_verified':sum(row.get('invocation_evidence') is True for row in production),
        'contract_probe_correct':sum(row.get('business_correct') is True for row in probes),
        'contract_probe_complete_chain':sum(row.get('accepted') is True for row in probes),
        'contract_probe_terminal_verified':sum(row.get('invocation_evidence') is True for row in probes)}


def run_address_comparison(root, *, timeout=None, formal=False, max_sessions=None, runner=None):
    """Run at most two pilots or twelve frozen formal attempts; no implicit retry.

    ``formal=False`` selects the separate two-session wiring pilot. A later call
    resumes only not-yet-reserved sessions; incomplete/failed attempts stay fixed.
    ``timeout`` may confirm the registered budget, never change it.
    """
    plan = _load(root)
    if timeout is not None and timeout!=plan['timeout']:
        raise ValueError('frozen timeout cannot change')
    limit = 12 if formal else 2
    max_sessions = limit if max_sessions is None else max_sessions
    if type(max_sessions) is not int or not 0 <= max_sessions <= limit:
        raise ValueError(f'max_sessions must be between 0 and {limit}')
    _check_freeze(plan)
    if runner is None:
        from ..integrations.hermes_session import run_hermes_session
        runner = run_hermes_session
    ledger = PhaseLedger(Path(plan['root'])/'ledger')
    reserved = {r['attempt_id'] for r in ledger.records()}
    launched = 0
    for trial in plan['sessions'] if formal else plan['pilots']:
        if trial['trial_id'] in reserved or launched>=max_sessions:
            continue
        # Recheck code/config/skill and input before each paid session, including
        # its partner. A change stops the experiment; it never retunes a failure.
        _check_freeze(plan)
        ledger.reserve(trial['arm'],trial['phase'],trial['trial_id'])
        launched += 1
        record = _observe(plan,trial,runner)
        _save(Path(plan['root'])/'records'/(trial['trial_id']+'.json'),record)
        outcome = {k:record[k] for k in ('status','elapsed_seconds','usage','model_identity')}
        outcome.update(artifact_correct=record['category']=='production' and record['business_correct'],
                       workflow_verified=record['invocation_evidence'])
        if record.get('error_code'):
            outcome['error_code'] = record['error_code']
        ledger.finish(trial['trial_id'],outcome)
    return summarize_address_comparison(root)


def export_address_evidence(root, destination):
    """Export public fixtures, frozen truth, compact evidence and all cost phases.

    Export outside owned-root before cleanup. This function neither stops a
    process nor removes files; parent ownership controls the later cleanup.
    """
    plan,summary = _load(root),summarize_address_comparison(root)
    destination = _safe_path(destination)
    if destination.is_relative_to(plan['root']) or Path(plan['root']).is_relative_to(destination):
        raise ValueError('public export must be outside owned root')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('public export destination must be empty')
    destination.mkdir(parents=True,exist_ok=True)
    public = {k:v for k,v in plan.items() if k not in {'root','skill_root','hermes_root','db','catalog_root','helper_python','cases','sessions','pilots'}}
    public['cases'] = [{k:v for k,v in c.items() if k!='source_path'} for c in plan['cases']]
    public['sessions'] = [{k:v for k,v in t.items() if k not in ('workspace','oracle_plan','db')} for t in plan['sessions']]
    public['pilots'] = [{k:v for k,v in t.items() if k not in ('workspace','oracle_plan','db')} for t in plan['pilots']]
    public['controller_registration_sha256'] = plan['sha256']
    public['prompt_redaction_policy'] = ('public prompts replace the local interpreter path with '
        '<frozen-helper-python>; session prompt_sha256 remains the original unredacted hash')
    public.pop('sha256')
    _save(destination/'preregistration.json',public)
    _save(destination/'summary.json',summary)
    _save(destination/'cost-phases.json',{'records':PhaseLedger(Path(plan['root'])/'ledger').records()})
    public_files = []
    (destination/'fixtures').mkdir()
    (destination/'prompts').mkdir()
    for case in plan['cases']:
        source = Path(case['source_path'])
        raw,metadata = _export_bytes(source,source.name)
        verified = raw is not None and metadata['sha256']==case['input_sha256']
        metadata.update(raw_exported=verified,reason='frozen_public_input' if verified else 'frozen_input_changed_or_unreadable')
        public_files.append({'kind':'fixture',**metadata})
        if verified:
            (destination/'fixtures'/source.name).write_bytes(raw)
        trial = next(t for t in plan['sessions'] if t['case_id']==case['case_id'])
        raw,metadata = _export_bytes(Path(trial['workspace'])/'prompt.txt',case['case_id']+'.txt')
        verified = raw is not None and metadata['sha256']==trial['prompt_sha256']
        metadata.update(raw_exported=verified,reason='frozen_public_prompt' if verified else 'frozen_prompt_changed_or_unreadable')
        public_files.append({'kind':'prompt',**metadata})
        if verified:
            (destination/'prompts'/(case['case_id']+'.txt')).write_text(
                raw.decode('utf-8').replace(plan['helper_python'],'<frozen-helper-python>'),encoding='utf-8')
    _save(destination/'public-files.json',{'files':public_files})
    for record in summary['records']:
        trial = next(t for t in plan['sessions']+plan['pilots'] if t['trial_id']==record['trial_id'])
        case = next(c for c in plan['cases'] if c['case_id']==trial['case_id'])
        folder = destination/'artifacts'/trial['trial_id']
        folder.mkdir(parents=True)
        fresh = score_address_artifact(plan,trial)
        artifacts = []
        for name in (case['output_name'],'terminal.json'):
            source = Path(trial['workspace'])/name
            raw,metadata = _export_bytes(source,name)
            public_truth = (trial['category']=='production' and name==case['output_name']
                and fresh['business_correct'] and fresh['product_oracle_passed']
                and metadata['sha256']==fresh['artifact_sha256'])
            public_terminal = (trial['category']=='contract_probe' and name=='terminal.json'
                and fresh['business_correct'] and fresh['structured_terminal'] is not None
                and metadata['sha256']==fresh.get('terminal_sha256'))
            verified = bool(raw is not None and (public_truth or public_terminal))
            metadata.update(raw_exported=verified,reason=('reverified_public_business_truth' if public_truth
                else 'reverified_strict_terminal' if public_terminal else 'unverified_or_changed_artifact'))
            artifacts.append(metadata)
            if verified:
                (folder/name).write_bytes(raw)
        _save(folder/'manifest.json',{'artifacts':artifacts})
    return summary


def _export_bytes(path,name):
    """Unverified bytes stay local; only their bounded metadata may be exported."""
    metadata = {'name':name,'sha256':None,'size_bytes':None}
    try:
        metadata['size_bytes'] = path.lstat().st_size
        raw = read_regular_file(path,max_bytes=MAX_ARTIFACT_BYTES)
        metadata.update(sha256=_sha(raw),size_bytes=len(raw))
        return raw,metadata
    except (OSError,ValueError):
        return None,metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    register = sub.add_parser('preregister')
    register.add_argument('--root',required=True)
    register.add_argument('--skill-root',required=True)
    register.add_argument('--hermes-root')
    register.add_argument('--helper-python')
    register.add_argument('--timeout',type=int,default=120)
    register.add_argument('--max-turns',type=int,default=8)
    run = sub.add_parser('run')
    run.add_argument('--root',required=True)
    run.add_argument('--timeout',type=int)
    run.add_argument('--formal',action='store_true')
    run.add_argument('--max-sessions',type=int)
    summary = sub.add_parser('summary')
    summary.add_argument('--root',required=True)
    export = sub.add_parser('export')
    export.add_argument('--root',required=True)
    export.add_argument('--destination',required=True)
    arguments = vars(parser.parse_args(argv))
    command = arguments.pop('command')
    if command=='preregister':
        result = preregister_address_comparison(**arguments)
    elif command=='run':
        result = run_address_comparison(**arguments)
    elif command=='summary':
        result = summarize_address_comparison(**arguments)
    else:
        result = export_address_evidence(**arguments)
    print(_json(result))
    return 0
