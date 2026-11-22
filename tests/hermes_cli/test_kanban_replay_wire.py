"""RFC8785 digest framing matches the Board, not Python's JSON spelling."""
import hashlib

import pytest


def test_digest_uses_jcs_and_excludes_replaceable_session_and_self_digest():
    from hermes_cli.kanban_replay_wire import freeze, verify

    op = {'client_op_id': 'operation-0001', 'fields': {'n': 1.0, 'z': -0.0},
          'replay': {'protocol': 'processed_prefix1', 'generation': '1', 'sequence': '1', 'session': '9'}}
    wire = freeze(op)
    hashed = (b'{"client_op_id":"operation-0001","fields":{"n":1,"z":0},'
              b'"replay":{"generation":"1","protocol":"processed_prefix1","sequence":"1"}}')
    digest = hashlib.sha256(hashed).hexdigest()
    assert wire['replay']['payload_sha256'] == digest
    assert 'payload_sha256' not in op['replay']
    assert verify(wire) == digest
    wire['replay']['session'] = '10'
    assert verify(wire) == digest
    wire['fields']['n'] = 2
    with pytest.raises(ValueError, match='digest mismatch'):
        verify(wire)


def test_jcs_utf16_order_escaping_and_number_spelling():
    import rfc8785
    from hermes_cli.kanban_replay_wire import freeze

    wire = freeze({'client_op_id': 'operation-0001',
                   'fields': {'\ue000': 1e30, '\U0001f600': 1e-7, 'small': 1e-6, 'large': 1e20},
                   'replay': {'protocol': 'processed_prefix1', 'generation': '1', 'sequence': '1'}})
    # JCS sorts UTF-16 units: the astral surrogate pair sorts BEFORE U+E000.
    assert rfc8785.dumps(wire['fields']) == (
        '{"large":100000000000000000000,"small":0.000001,"😀":1e-7,"\ue000":1e+30}'.encode())


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf'), '\ud800', 2**53])
def test_unrepresentable_canonical_values_refuse(value):
    from hermes_cli.kanban_replay_wire import freeze

    with pytest.raises(ValueError):
        freeze({'fields': {'bad': value}, 'replay': {}})


def test_predecessor_ack_is_part_of_digest():
    from hermes_cli.kanban_replay_wire import freeze, verify

    wire = freeze({'client_op_id': 'operation-0002', 'replay': {
        'protocol': 'processed_prefix1', 'generation': '1', 'sequence': '2',
        'ack': {'sequence': '1', 'op_id': 'operation-0001', 'payload_sha256': '0' * 64}}})
    wire['replay']['ack']['op_id'] = 'operation-9999'
    with pytest.raises(ValueError, match='digest mismatch'):
        verify(wire)
