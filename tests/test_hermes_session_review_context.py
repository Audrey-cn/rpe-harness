"""Public source admission occurs before native dependencies or credentials."""
import pytest
from rpe_harness.integrations import hermes_session


@pytest.mark.parametrize('sources', [[{'hidden': 'history'}], {'source': 'not-an-array'}, {}, False])
def test_review_invalid_attachments_block_before_host_setup(tmp_path, monkeypatch, sources):
    monkeypatch.setattr(hermes_session, '_installed_python', lambda *a: pytest.fail('host dependency inspected'))
    with pytest.raises(ValueError, match='review'):
        hermes_session.run_hermes_session('处理我的当前文件', tmp_path, tmp_path / 'state.sqlite',
            owner_root=tmp_path / 'owned', tool_enabled=True, entry_mode='review',
            context_sources=sources)


def test_review_explicit_valid_sources_reach_host_setup_without_hidden_contract(tmp_path, monkeypatch):
    class ReachedHost(Exception):
        pass
    def dependency(*args):
        raise ReachedHost()
    monkeypatch.setattr(hermes_session, '_installed_python', dependency)
    with pytest.raises(ReachedHost):
        hermes_session.run_hermes_session('当前请求不改写', tmp_path, tmp_path / 'state.sqlite',
            owner_root=tmp_path / 'owned', tool_enabled=True, entry_mode='review',
            context_sources=[{'source': 'user', 'event_id': 'earlier-explicit-key',
                'payload': {'text': '以客户编号作为业务键'}, 'facts': {'key_columns': ['客户编号']}}])
