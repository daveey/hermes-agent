"""Configured endpoint is not a native decision or sender-profile attestation."""
import json

import pytest

from hermes_cli import kanban_bb_binding_source as source
from hermes_cli import kanban_bb_binding_runtime as runtime
from hermes_cli.kanban_card_answer import AnswerRefused
from tests.hermes_cli import test_kanban_bb_binding_runtime as helpers

binding_db = helpers.binding_db
fixture = helpers.fixture


@pytest.mark.parametrize('field,value', [('from', 'gw:foreign'), ('from', 'human'),
                                        ('from', None), ('base_version', 1),
                                        ('base_version', False)])
def test_source_and_null_base_before_board_access(fixture, monkeypatch, capsys, field, value):
    fixture[2][field] = value
    monkeypatch.setattr(runtime, '_board_db', lambda *_: pytest.fail('untrusted request opened board'))
    code, out = helpers.invoke(capsys)
    assert code == 2 and out['refused'] == 'bb_binding_request_invalid'


def test_missing_base_is_not_explicit_null(fixture, capsys):
    fixture[2].pop('base_version')
    assert helpers.invoke(capsys)[0] == 2


@pytest.mark.parametrize('config', [{}, {'bb_owner_response': None}, {'bb_owner_response': {}}] + [
    {'bb_owner_response': {'source': s}} for s in [None, [], 'producer', {},
        {'gateway': 'producer'}, {'profile': 'bb'}, {'gateway': '*', 'profile': 'bb'},
        {'gateway': 'producer', 'profile': 'b/b'}, {'gateway': 'producer', 'profile': ''},
        {'gateway': True, 'profile': 'bb'}, {'gateway': 'producer', 'profile': 'bb', 'url': 'x'}]])
def test_missing_or_malformed_configuration_is_pending(fixture, monkeypatch, capsys, config):
    monkeypatch.setattr('hermes_cli.config_effective.load_user_config_effective', lambda **kw: config)
    code, out = helpers.invoke(capsys)
    assert code == 3 and out == {'ok': False, 'retryable': True, 'error': 'unavailable'}


def test_supported_effective_config_reader(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from hermes_cli import config_backend
    config_backend.write_config_document(tmp_path / 'config.yaml',
                                         {'bb_owner_response': {'source': {'gateway': 'source', 'profile': 'work'}}})
    assert source.configured_source() == {'gateway': 'source', 'profile': 'work'}


@pytest.mark.parametrize('raw', [b'{"request":{},"request":{}}', b'{"request":{"id":"binding-request","claims":NaN}}',
    b'{"request":{"id":"binding-request","claims":1e400}}',
    b'{"request":{"id":"binding-request","args":{"x":1,"x":2}}}',
    b'{"request":{"id":"binding-request"},"override":true}',
    b'{"request":{"id":"binding-request","override":true}}',
    b'[]', b'{"request":null}', b'1', b'null',
    b'{"request":{"id":"foreign"}}', b'\xff'])
def test_strict_authenticated_json(monkeypatch, raw):
    records = object.__new__(source.BindingRecords)
    monkeypatch.setattr(records, 'raw', lambda rid: raw)
    with pytest.raises(AnswerRefused, match='courier_record_invalid'):
        records.read(helpers.REQUEST)


def test_unavailable_configuration_is_pending(fixture, monkeypatch, capsys):
    from hermes_cli.config_backend import ConfigBackendUnavailable
    def unavailable(**kw):
        raise ConfigBackendUnavailable('synthetic unavailable')
    monkeypatch.setattr('hermes_cli.config_effective.load_user_config_effective', unavailable)
    code, out = helpers.invoke(capsys)
    assert code == 3 and out['error'] == 'unavailable'


def test_strict_record_preserves_finite_json_and_read_identity(monkeypatch):
    records = object.__new__(source.BindingRecords)
    seen = []
    def raw(rid):
        seen.append(rid)
        return json.dumps({'request': {'id': rid, 'claims': 1.25}}).encode()
    monkeypatch.setattr(records, 'raw', raw)
    assert records.read(helpers.REQUEST) == {'id': helpers.REQUEST, 'claims': 1.25}
    assert seen == [helpers.REQUEST]


def test_gateway_is_authenticated_not_unobservable_sender_profile(fixture, capsys):
    # No source profile is on the binding wire. Its destination is the receiving profile.
    assert 'source' not in fixture[2] and fixture[2]['args'] == {}
    assert helpers.invoke(capsys)[0] == 0
    fixture[2]['key'] = 'bb/default/' + fixture[2]['key'].split('/')[-1]
    assert helpers.invoke(capsys)[0] == 2
