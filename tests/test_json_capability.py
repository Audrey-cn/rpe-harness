"""Literal JSON truths exercise admission, lossless output and the independent oracle."""
import hashlib
import importlib
import importlib.util
from pathlib import Path

import pytest


def _api():
    # Missing capability is an assertion failure during the initial RED run.
    assert importlib.util.find_spec('rpe_harness.json_contract') is not None, 'JSON contract is not implemented'
    return (importlib.import_module('rpe_harness.json_contract'),
            importlib.import_module('rpe_harness.json_execution'),
            importlib.import_module('rpe_harness.json_verification'))


def _request(tmp_path, raw=b'{"b":1,"a":[]}', style='pretty'):
    source, output = tmp_path / 'source.json', tmp_path / 'output.json'
    source.write_bytes(raw)
    return {'schema_version': 2, 'event_id': 'json-test', 'capability': 'json.normalize',
            'contract_version': 1, 'goal': 'Normalize JSON',
            'execution': {'input_path': str(source), 'output_path': str(output), 'style': style}}


@pytest.mark.parametrize(('raw', 'style', 'expected', 'root_type'), [
    (b' { "b" : 1, "a" : [ true, null, {} ] } ', 'pretty',
     b'{\n  "b": 1,\n  "a": [\n    true,\n    null,\n    {}\n  ]\n}\n', 'object'),
    (b' { "b" : 1, "a" : [ true, null, {} ] } ', 'minify',
     b'{"b":1,"a":[true,null,{}]}\n', 'object'),
    (b' [ 9007199254740993, 1.2300, 1E+003, -0, 1e999999 ] ', 'minify',
     b'[9007199254740993,1.2300,1E+003,-0,1e999999]\n', 'array'),
    (b' { "escaped" : "\\u4f60\\u597d", "x": "\\ud83d\\ude00", "space": " a b " } ', 'minify',
     b'{"escaped":"\\u4f60\\u597d","x":"\\ud83d\\ude00","space":" a b "}\n', 'object'),
    (' {"中文":"你好😀"} '.encode(), 'pretty', '{\n  "中文": "你好😀"\n}\n'.encode(), 'object'),
    (b' [ {}, [], [ 1 ] ] ', 'pretty', b'[\n  {},\n  [],\n  [\n    1\n  ]\n]\n', 'array'),
    (b' true ', 'pretty', b'true\n', 'boolean'),
    (b' false ', 'minify', b'false\n', 'boolean'),
    (b' null ', 'pretty', b'null\n', 'null'),
    (b' -0 ', 'pretty', b'-0\n', 'number'),
    (b' "a\\n b\\\\c\\\"" ', 'minify', b'"a\\n b\\\\c\\\""\n', 'string'),
    (b'{}', 'pretty', b'{}\n', 'object'),
    (b'[]', 'pretty', b'[]\n', 'array'),
])
def test_normalization_publishes_literal_lossless_artifacts(tmp_path, raw, style, expected, root_type):
    contract, execution, verification = _api()
    from rpe_harness.execution import cleanup_staging, publish_output
    request = _request(tmp_path, raw, style)
    plan = contract.JsonNormalizePlan.from_request(request)
    prepared = execution.prepare_json_normalization(**plan.execution_kwargs())
    try:
        assert not Path(plan.output_path).exists()
        assert Path(prepared['staging_path']).read_bytes() == expected
        report = verification.verify_json_normalization(prepared, plan)
        assert report['passed'] is True
        assert report['root_type'] == root_type
        assert report['input_bytes'] == len(raw)
        assert report['output_bytes'] == len(expected)
        assert report['plan_sha256'] == plan.sha256
        assert not {'input_rows', 'output_rows', 'removed_rows'} & set(report)
        publish_output(prepared, plan)
        assert Path(plan.output_path).read_bytes() == expected
        assert Path(plan.input_path).read_bytes() == raw
    finally:
        cleanup_staging(prepared)


@pytest.mark.parametrize('raw', [
    b'', b' ', b'{} {}', b'{"a":1,"a":2}', b'{"a":1,"\\u0061":2}',
    b'{"a":{"x":1,"x":2}}', b'NaN', b'Infinity', b'-Infinity', b'[NaN]',
    b'"\\ud800"', b'"\\udfff"', b'{"\\ud800":1}', b'"\\ud800a\\udfff"',
    b'"line\nbreak"', b'"\xff"', b'\xef\xbb\xbf{}', b'{"x":01}', b'[1,]',
    b'/*comment*/{}', b'{"x":1e}', b'{"x":.1}', b'{"x":+1}', b'"\\x00"',
    b'"unfinished', b'\x00{}', b'\xc0\x80', b'\xed\xa0\x80',
])
def test_invalid_json_is_rejected_without_staging(tmp_path, raw):
    contract, execution, _ = _api()
    request = _request(tmp_path, raw)
    with pytest.raises(ValueError):
        contract.JsonNormalizePlan.from_request(request)
    with pytest.raises(ValueError):
        execution.prepare_json_normalization(
            Path(request['execution']['input_path']), Path(request['execution']['output_path']), style='pretty')
    assert not (tmp_path / 'output.json').exists()
    assert not list(tmp_path.glob('.rpe-*'))


@pytest.mark.parametrize(('raw', 'accepted'), [
    (b'[' * 64 + b'0' + b']' * 64, True),
    (b'[' * 65 + b'0' + b']' * 65, False),
    (b'1' * 256, True), (b'1' * 257, False),
    (b' ' * (1024 * 1024 - 1) + b'0', True),
    (b' ' * (1024 * 1024) + b'0', False),
    (b'[' + b'0,' * 49999 + b'0]', False),
    (b'[' + b'0,' * 49998 + b'0]', True),
], ids=['depth-64', 'depth-65', 'number-256', 'number-257', 'input-limit',
        'input-over-limit', 'tokens-over-limit', 'tokens-within-limit'])
def test_fixed_source_budgets_apply_before_output(tmp_path, raw, accepted):
    contract, execution, verification = _api()
    from rpe_harness.execution import cleanup_staging
    request = _request(tmp_path, raw, 'minify')
    if not accepted:
        with pytest.raises(ValueError):
            contract.JsonNormalizePlan.from_request(request)
        with pytest.raises(ValueError):
            execution.prepare_json_normalization(
                Path(request['execution']['input_path']), Path(request['execution']['output_path']), style='minify')
        assert not list(tmp_path.glob('.rpe-*'))
        return
    plan = contract.JsonNormalizePlan.from_request(request)
    prepared = execution.prepare_json_normalization(**plan.execution_kwargs())
    try:
        assert verification.verify_json_normalization(prepared, plan)['passed'] is True
    finally:
        cleanup_staging(prepared)


@pytest.mark.parametrize('mutation', ['style_missing', 'style_unknown', 'columns', 'budget', 'v1',
                                      'capability', 'version_bool', 'extra_top', 'constraint', 'extension'])
def test_exact_json_request_authority(tmp_path, mutation):
    contract, _, _ = _api()
    request = _request(tmp_path)
    if mutation == 'style_missing':
        request['execution'].pop('style')
    elif mutation == 'style_unknown':
        request['execution']['style'] = 'canonical'
    elif mutation == 'columns':
        request['execution']['key_columns'] = ['x']
    elif mutation == 'budget':
        request['execution']['max_bytes'] = 2**30
    elif mutation == 'v1':
        request['schema_version'] = 1
    elif mutation == 'capability':
        request['capability'] = 'json.validate'
    elif mutation == 'version_bool':
        request['contract_version'] = True
    elif mutation == 'extra_top':
        request['overwrite'] = True
    elif mutation == 'constraint':
        request['constraints'] = ['preserve_formulas']
    elif mutation == 'extension':
        request['execution']['output_path'] = str(tmp_path / 'output.txt')
    with pytest.raises(ValueError):
        contract.JsonNormalizePlan.from_request(request)


def test_plan_hash_freezes_style_and_roundtrips_without_file_reinterpretation(tmp_path):
    contract, _, _ = _api()
    request = _request(tmp_path)
    plan = contract.JsonNormalizePlan.from_request(request)
    saved = plan.to_dict()
    assert contract.JsonNormalizePlan.from_dict(saved).sha256 == plan.sha256
    assert saved['constraints'] == []
    saved.update(goal='Other prose', source_ref='elsewhere', session_id='another session')
    assert contract.JsonNormalizePlan.from_dict(saved).sha256 == plan.sha256
    saved['style'] = 'minify'
    assert contract.JsonNormalizePlan.from_dict(saved).sha256 != plan.sha256
    Path(plan.input_path).unlink()
    assert contract.JsonNormalizePlan.from_dict(plan.to_dict()).sha256 == plan.sha256
    saved['key_columns'] = []
    with pytest.raises(ValueError):
        contract.JsonNormalizePlan.from_dict(saved)


@pytest.mark.parametrize('replacement', [
    b'{"b":1,"a":[]}\n',                 # Wrong style, identical JSON values.
    b'{\n  "b": true,\n  "a": []\n}\n', # bool/int equality must not pass.
    b'{\n  "b": 1.0,\n  "a": []\n}\n',
    b'{\n  "a": [],\n  "b": 1\n}\n',
    b'{\n  "b": 1,\n  "a": []\n}',
    b'{\n  "b": 1,\n  "a": []\n}\n\n',
    b'{\n    "b": 1,\n    "a": []\n}\n',
    b'{\r\n  "b": 1,\r\n  "a": []\r\n}\r\n',
])
def test_oracle_rejects_changed_tokens_and_wrong_exact_style(tmp_path, replacement):
    contract, execution, verification = _api()
    from rpe_harness.execution import cleanup_staging, publish_output
    plan = contract.JsonNormalizePlan.from_request(_request(tmp_path))
    prepared = execution.prepare_json_normalization(**plan.execution_kwargs())
    try:
        assert verification.verify_json_normalization(prepared, plan)['passed'] is True
        Path(prepared['staging_path']).write_bytes(replacement)
        report = verification.verify_json_normalization(prepared, plan)
        assert report['passed'] is False
        assert '_verified_output_sha256' not in prepared
        assert '_verified_plan_sha256' not in prepared
        with pytest.raises(ValueError):
            publish_output(prepared, plan)
        assert not Path(plan.output_path).exists()
    finally:
        cleanup_staging(prepared)


def test_oracle_rejects_escaped_string_reencoding(tmp_path):
    contract, execution, verification = _api()
    from rpe_harness.execution import cleanup_staging
    plan = contract.JsonNormalizePlan.from_request(_request(tmp_path, b'"\\u0061"', 'minify'))
    prepared = execution.prepare_json_normalization(**plan.execution_kwargs())
    try:
        Path(prepared['staging_path']).write_bytes(b'"a"\n')
        assert verification.verify_json_normalization(prepared, plan)['passed'] is False
    finally:
        cleanup_staging(prepared)


def test_changed_source_and_prepared_parameters_cannot_publish(tmp_path):
    contract, execution, verification = _api()
    from rpe_harness.execution import cleanup_staging
    plan = contract.JsonNormalizePlan.from_request(_request(tmp_path))
    prepared = execution.prepare_json_normalization(**plan.execution_kwargs())
    try:
        prepared['style'] = 'minify'
        assert verification.verify_json_normalization(prepared, plan)['passed'] is False
        prepared['style'] = 'pretty'
        Path(plan.input_path).write_bytes(b'{}')
        assert verification.verify_json_normalization(prepared, plan)['passed'] is False
    finally:
        cleanup_staging(prepared)
    with pytest.raises(ValueError):
        execution.prepare_json_normalization(**plan.execution_kwargs())


def test_existing_output_and_symlink_source_are_rejected(tmp_path):
    contract, _, _ = _api()
    request = _request(tmp_path)
    target = Path(request['execution']['output_path'])
    target.write_bytes(b'keep')
    with pytest.raises(ValueError):
        contract.JsonNormalizePlan.from_request(request)
    assert target.read_bytes() == b'keep'
    target.unlink()
    source = Path(request['execution']['input_path'])
    raw = source.read_bytes()
    source.unlink()
    original = tmp_path / 'original.json'
    original.write_bytes(raw)
    source.symlink_to(original)
    with pytest.raises(ValueError):
        contract.JsonNormalizePlan.from_request(request)


def test_producer_and_oracle_read_the_broker_bound_descriptor(tmp_path):
    contract, execution, verification = _api()
    from rpe_harness.execution import cleanup_staging, publish_output
    from rpe_harness.path_binding import BoundExecution
    request = _request(tmp_path, b'{ "x" : 1 }', 'minify')
    stage = tmp_path / 'owned-staging'
    stage.mkdir()
    binding = BoundExecution.capture(request, tmp_path, stage,
                                     hashlib.sha256(b'{ "x" : 1 }').hexdigest())
    try:
        with binding.activate():
            plan = contract.JsonNormalizePlan.from_request(request)
            prepared = execution.prepare_json_normalization(**plan.execution_kwargs())
            try:
                assert Path(prepared['staging_path']).parent == stage
                assert verification.verify_json_normalization(prepared, plan)['passed'] is True
                publish_output(prepared, plan)
                assert Path(plan.output_path).read_bytes() == b'{"x":1}\n'
            finally:
                cleanup_staging(prepared)
    finally:
        binding.close()


def test_json_oracle_rejects_symlinked_artifact_parent(tmp_path):
    contract, execution, verification = _api()
    from rpe_harness.execution import cleanup_staging
    plan = contract.JsonNormalizePlan.from_request(_request(tmp_path))
    prepared = execution.prepare_json_normalization(**plan.execution_kwargs())
    alias = tmp_path / 'alias'
    alias.symlink_to(tmp_path, target_is_directory=True)
    try:
        unsafe = dict(prepared, staging_path=str(alias / Path(prepared['staging_path']).name))
        assert verification.verify_json_normalization(unsafe, plan)['passed'] is False
    finally:
        cleanup_staging(prepared)


def test_direct_producer_rejects_non_string_style_and_false_constraints(tmp_path):
    _, execution, _ = _api()
    request = _request(tmp_path)
    source, output = (Path(request['execution'][key]) for key in ('input_path', 'output_path'))
    with pytest.raises(ValueError):
        execution.prepare_json_normalization(source, output, style=[])
    with pytest.raises(ValueError):
        execution.prepare_json_normalization(source, output, style='pretty', constraints='')


def test_oracle_rejects_bounded_special_and_oversized_artifacts(tmp_path):
    contract, execution, verification = _api()
    from rpe_harness.execution import cleanup_staging
    plan = contract.JsonNormalizePlan.from_request(_request(tmp_path))
    prepared = execution.prepare_json_normalization(**plan.execution_kwargs())
    try:
        directory = tmp_path / 'directory.json'
        directory.mkdir()
        unsafe = dict(prepared, staging_path=str(directory))
        assert verification.verify_json_normalization(unsafe, plan)['passed'] is False
        with Path(prepared['staging_path']).open('wb') as handle:
            handle.truncate(16 * 1024 * 1024 + 1)
        assert verification.verify_json_normalization(prepared, plan)['passed'] is False
    finally:
        cleanup_staging(prepared)


@pytest.mark.parametrize('revision', ['executor_revision', 'verifier_revision'])
def test_oracle_rejects_stale_implementation_revision(tmp_path, revision):
    contract, execution, verification = _api()
    from rpe_harness.execution import cleanup_staging
    plan = contract.JsonNormalizePlan.from_request(_request(tmp_path))
    prepared = execution.prepare_json_normalization(**plan.execution_kwargs())
    stale = plan.to_dict()
    stale[revision] = '0' * 64
    try:
        report = verification.verify_json_normalization(prepared, contract.JsonNormalizePlan.from_dict(stale))
        assert report['passed'] is False
        assert report['checks'][revision] is False
    finally:
        cleanup_staging(prepared)
