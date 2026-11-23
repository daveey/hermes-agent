"""RFC8785 digest framing matches the Board, not Python's JSON spelling."""
import hashlib
import subprocess
import sys
from pathlib import Path
import textwrap

import pytest


def test_legacy_updater_parser_does_not_require_new_replay_dependency(tmp_path):
    """A hand-pulled legacy venv must reach update before installing new deps."""
    probe = tmp_path / 'replay_dependency_probe.py'
    probe.write_text(textwrap.dedent('''
        import builtins
        import sys
        sys.path.insert(0, sys.argv[1])
        original_import = builtins.__import__
        def without_rfc8785(name, *args, **kwargs):
            if name == 'rfc8785':
                raise ModuleNotFoundError("controlled legacy venv: no rfc8785", name=name)
            return original_import(name, *args, **kwargs)
        builtins.__import__ = without_rfc8785
        from hermes_cli.main import _build_cli_parser
        parser, _ = _build_cli_parser()
        args = parser.parse_args(['update', '--yes', '--branch', 'main'])
        assert args.command == 'update'
        from hermes_cli.kanban_replay_wire import freeze
        try:
            freeze({'fields': {}, 'replay': {}})
        except ModuleNotFoundError as exc:
            assert exc.name == 'rfc8785'
        else:
            raise AssertionError('replay must not silently substitute noncanonical JSON')
        print('legacy parser reachable; replay still requires canonical dependency')
    '''))
    result = subprocess.run([sys.executable, '-I', str(probe), str(Path(__file__).resolve().parents[2])],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'legacy parser reachable' in result.stdout


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
