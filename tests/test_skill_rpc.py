import http.client
import json
import sys
from urllib.parse import urlsplit

from rpe_harness.integrations.hermes import HostEventStore
from rpe_harness.integrations.hermes_broker import execution_broker
from rpe_harness.integrations.hermes_gate import digest_json


def test_skill_rpc_requires_actual_policy_and_argument_bound_receipt(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    db = tmp_path / 'runs.sqlite'
    store = HostEventStore(db)
    arguments = {'q':'workbook','input_format':'xlsx','capability':'xlsx.inspect'}
    native = {'session_id':'s','task_id':'task','turn_id':'turn','api_request_id':'api',
              'tool_call_id':'call','tool_name':'rpe_discover','entry_key':'entry',
              'arguments_sha256':digest_json(arguments)}
    store.record('pre_api_request', {**native,'request':{'body':{
        'tool_choice':{'type':'function','function':{'name':'rpe_discover'}},
        'parallel_tool_calls':False,
        'tools':[{'type':'function','function':{'name':'rpe_discover','parameters':{'type':'object'}}}]}}})
    receipt = store.record('rpe_skill_rpc_received', native)
    def extension(operation, args, identity):
        assert operation == 'discover'
        assert args == arguments
        assert identity == native
        return {'snapshot_id':'a'*64,'kind':'suggestion_only'}
    with execution_broker(db_path=db, workspace=workspace, python=sys.executable,
                          extension=extension) as environment:
        url = urlsplit(environment['RPE_HERMES_BROKER_URL'])
        def post(body):
            connection = http.client.HTTPConnection(url.hostname,url.port,timeout=5)
            data = json.dumps(body).encode()
            connection.request('POST','/skill',data,headers={
                'Authorization':'Bearer '+environment['RPE_HERMES_BROKER_TOKEN'],
                'Content-Type':'application/json'})
            response = connection.getresponse()
            value = json.loads(response.read())
            connection.close()
            return response.status,value
        body = {'operation':'discover','arguments':arguments,
                'receipt_id':receipt['ingest_event_id'],'native':native}
        status,result = post(body)
        assert status == 200
        assert result['result']['snapshot_id'] == 'a'*64
        forged = {**body,'arguments':{'q':'different'}}
        assert post(forged)[0] == 403
        absent = {**body,'receipt_id':'missing'}
        assert post(absent)[0] == 403

