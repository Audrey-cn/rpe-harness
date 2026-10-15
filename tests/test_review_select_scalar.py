"""Scalar selection is an unverified provider compatibility attempt, never XML repair."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from test_review_skills import skill_env as skill_env, skill_review as skill_review, step


@pytest.mark.parametrize('skill_env', ['warm'], indirect=True)
@pytest.mark.parametrize('selection, expected_status', [
    ('__no_skill__', 'selected_none'), ('productivity/xlsx', 'selected')])
def test_scalar_select_wire_preserves_auto_choice_and_exact_selection(skill_review, selection, expected_status):
    step(skill_review, 'rpe_discover', {'q': 'Excel spreadsheet'})
    result = step(skill_review, 'rpe_select', {'skill_id': selection})
    assert result['status'] == expected_status, result
    request = skill_review['request']
    assert request['tool_choice'] == 'auto'
    assert 'parallel_tool_calls' not in request
    function = request['tools'][0]['function']
    assert function['parameters']['properties']['skill_id']['type'] == 'string'
    assert '__no_skill__' in json.dumps(request['messages'])
    runtime = Path('/Users/audrey/.hermes/hermes-agent/.venv/bin/python')
    if not runtime.is_file():
        pytest.skip('installed OpenAI SDK runtime unavailable')
    script = r"""
import json,os,sys
from pathlib import Path
os.environ['HERMES_HOME']=sys.argv[2]
sys.path.insert(0,sys.argv[1])
import httpx
from openai import OpenAI
from agent.transports.chat_completions import ChatCompletionsTransport
incoming=json.loads(sys.stdin.read())
seen={}
def respond(request):
 body=json.loads(request.content)
 seen['schema_type']=body['tools'][0]['function']['parameters']['properties']['skill_id']['type']
 seen['choice']=body['tool_choice']
 seen['parallel_present']='parallel_tool_calls' in body
 seen['names']=[tool['function']['name'] for tool in body['tools']]
 return httpx.Response(200,json={'id':'local-scalar-mock','object':'chat.completion','created':1,
  'model':'mimo-v2.6-pro','choices':[{'index':0,'finish_reason':'tool_calls','message':{
   'role':'assistant','content':None,'tool_calls':[{'id':'call_scalar','type':'function',
    'function':{'name':'rpe_select','arguments':json.dumps({'skill_id':incoming['selection']})}}]}}]})
client=OpenAI(api_key='local-mock-only',base_url='https://mock.invalid/v1',max_retries=0,
 http_client=httpx.Client(transport=httpx.MockTransport(respond)))
response=client.chat.completions.create(model='mimo-v2.6-pro',**incoming['request'])
normalized=ChatCompletionsTransport().normalize_response(response)
seen['normalized_name']=normalized.tool_calls[0].name
seen['normalized_arguments']=json.loads(normalized.tool_calls[0].arguments)
client.close()
print(json.dumps(seen))
"""
    request = {key: request[key] for key in ('messages', 'tools', 'tool_choice')}
    completed = subprocess.run([str(runtime), '-I', '-B', '-c', script,
        str(runtime.parent.parent.parent), str(skill_review['home'])],
        input=json.dumps({'request': request, 'selection': selection}), capture_output=True, text=True,
        cwd=skill_review['home'], timeout=30, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert completed.returncode == 0, completed.stderr
    seen = json.loads(completed.stdout.splitlines()[-1])
    assert seen == {'schema_type': 'string', 'choice': 'auto', 'parallel_present': False,
        'names': ['rpe_select'], 'normalized_name': 'rpe_select', 'normalized_arguments': {'skill_id': selection}}
