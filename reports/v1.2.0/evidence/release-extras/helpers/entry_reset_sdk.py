"""LIVE SDK child: run only through entry_reset_parent.py's owned sandbox.

The parent, not this script, owns credential preparation, process tracking and
acceptance. No fabricated native IDs/hooks or product policy changes occur here.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import uuid


PREFIX = 'RPE_ENTRY_RESET_RESULT '
STAGE = 'module_start'


def diagnostic(error):
    trace=error.__traceback__
    frames=[]
    while trace:
        frames.append({'module':trace.tb_frame.f_globals.get('__name__',''),
                       'function':trace.tb_frame.f_code.co_name,'line':trace.tb_lineno})
        trace=trace.tb_next
    return {'completed':False,'stage':STAGE,'error_type':type(error).__name__,
            'errno':getattr(error,'errno',None),'trace':frames}


def main():
    global STAGE
    STAGE='owned_environment'
    probe = Path(os.environ['RPE_PROBE_RUN_ROOT']).resolve(strict=True)
    home = Path(os.environ['HERMES_HOME']).resolve(strict=True)
    workspace = Path(os.environ['RPE_HERMES_WORKSPACE']).resolve(strict=True)
    db = Path(os.environ['RPE_HERMES_DB']).resolve()
    if (not home.is_relative_to(probe) or not workspace.is_relative_to(probe)
            or not db.is_relative_to(probe) or os.environ.get('RPE_HERMES_ENTRY_MODE')!='strict'
            or os.environ.get('RPE_HERMES_TOOL_ENABLED')!='1'
            or os.environ.get('RPE_HERMES_REQUIRED_REQUEST_PATH')):
        raise RuntimeError('owned strict probe environment required')
    STAGE='owned_configuration'
    configuration = json.loads((home/'config.yaml').read_text())
    if (configuration['platform_toolsets']['cli']!=['rpe_harness']
            or configuration['tools']['tool_search']['enabled']!='off'):
        raise RuntimeError('required direct tool exposure missing')
    STAGE='owned_tasks'
    manifest = json.loads((probe/'session/tasks.json').read_text())
    if len(manifest['rounds'])!=2:
        raise RuntimeError('exactly two probe tasks required')

    STAGE='installed_host_path'
    # The parent has already canonicalized this fixed path outside the sandbox.
    # A second strict realpath would inspect protected daily-home ancestors.
    install = Path(os.environ['RPE_HERMES_INSTALL_ROOT'])
    if not install.is_absolute() or not install.is_dir():
        raise RuntimeError('parent-validated installed host directory required')
    sys.path.insert(0,str(install))
    # The official bootstrap sees the same command posture as the isolated CLI.
    # It activates installed dependencies and hardens imports without editing code.
    sys.argv = [sys.argv[0],'chat']
    STAGE='official_bootstrap_import'
    importlib.import_module('hermes_bootstrap')
    sys.path.insert(0,os.environ['RPE_HARNESS_IMPORT_ROOT'])
    STAGE='harness_bootstrap_import'
    from rpe_harness.integrations.hermes_bootstrap import install_process_telemetry, install_usage_telemetry
    STAGE='process_telemetry'
    install_process_telemetry()
    STAGE='usage_telemetry'
    install_usage_telemetry()
    STAGE='truststore_import'
    from agent.ssl_verify import install_truststore
    STAGE='truststore_install'
    install_truststore()
    STAGE='plugin_manager_import'
    from hermes_cli.plugins import get_plugin_manager
    STAGE='plugin_manager_get'
    manager = get_plugin_manager()
    STAGE='plugin_discovery'
    manager.discover_and_load()
    loaded = manager._plugins['rpe-harness']
    if not loaded.enabled or loaded.error:
        raise RuntimeError('isolated shipped plugin failed to load')
    STAGE='run_agent_import'
    from run_agent import AIAgent
    # run_agent's official dotenv loader has loaded the parent's isolated home.
    # The key remains in memory; this script never reads a credential file.
    STAGE='isolated_key_presence'
    key = os.environ.get('XIAOMI_API_KEY')
    if not key:
        raise RuntimeError('isolated API key unavailable')
    STAGE='event_store'
    from rpe_harness.integrations.hermes import HostEventStore
    store = HostEventStore(db)
    if os.environ.get('RPE_PROBE_STARTUP_ONLY')=='1':
        print(PREFIX+json.dumps({'probe':'sdk_startup_only_diagnostic','completed':False,
            'startup_completed':True,'stage':'before_agent_initialization','agent_instance_count':0,
            'rounds':[]},ensure_ascii=False),flush=True)
        return 0
    STAGE='agent_initialization'
    agent = AIAgent(base_url=manifest['base_url'],api_key=key,provider='xiaomi',
        requested_provider='xiaomi',api_mode='chat_completions',model=manifest['model'],
        max_iterations=12,enabled_toolsets=['rpe_harness'],quiet_mode=True,
        save_trajectories=False,verbose_logging=False,platform='cli',
        session_id='entry-reset-'+uuid.uuid4().hex,cwd=str(workspace),
        skip_context_files=True,skip_memory=True,skip_background_review=True,
        run_budget_seconds=180,checkpoints_enabled=False)
    history = None
    returned = []
    try:
        for index,task in enumerate(manifest['rounds'],1):
            STAGE='run_conversation_'+str(index)
            previous = {e['ingest_event_id'] for e in store.list(agent.session_id)}
            previous_count = len(history) if history is not None else 0
            result = agent.run_conversation(task['prompt'],conversation_history=history)
            rows = [e for e in store.list(agent.session_id) if e['ingest_event_id'] not in previous]
            turn_ids = sorted({e['turn_id'] for e in rows if e.get('turn_id')})
            requests = [e for e in rows if e['kind']=='pre_api_request']
            posts = [e for e in rows if e['kind']=='post_tool_call' and e.get('tool_name')=='rpe_task']
            returned.append({'round':index,'completed':result.get('completed') is True,
                'failed':result.get('failed') is True,'interrupted':result.get('interrupted') is True,
                'api_calls':result.get('api_calls'),'history_messages_passed':previous_count,
                'returned_message_count':len(result.get('messages') or []),
                'turn_ids':turn_ids,'request_choices':[e.get('request_tool_choice') for e in requests],
                'tool_call_ids':[e.get('tool_call_id') for e in posts],
                'run_ids':[e.get('run_id') for e in posts]})
            if result.get('completed') is not True or not isinstance(result.get('messages'),list):
                break
            # Carry the actual unmodified first-turn transcript into the same agent.
            history = result['messages']
    finally:
        STAGE='agent_close'
        agent.close()
    safe = {'probe':'same_agent_two_round_entry_reset','session_id':agent.session_id,
            'agent_instance_count':1,'per_round_budget_seconds':180,'rounds':returned,
            'completed':len(returned)==2 and all(r['completed'] for r in returned)}
    print(PREFIX+json.dumps(safe,ensure_ascii=False,allow_nan=False),flush=True)
    return 0 if safe['completed'] else 2


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # No SDK exception text or response content enters the durable report.
        print(PREFIX+json.dumps(diagnostic(error),ensure_ascii=False),flush=True)
        raise SystemExit(3)
