"""CLI create carries ancestry visibility, without treating its own advertised env as ancestry."""
import pytest

from contextlib import asynccontextmanager
from tests.hermes_cli.test_gateway_session_store import FakeClient


@pytest.fixture
def store():
    from hermes_cli.gateway_connection import GatewayConnection
    from hermes_cli.gateway_session_store import GatewaySessionStore
    client = FakeClient()
    @asynccontextmanager
    async def connect():
        yield client
    connection = GatewayConnection(connect=connect)
    connection.start(5)
    yield GatewaySessionStore(connection), client
    connection.close()


@pytest.mark.parametrize('marker,visible,expected', [
    ('LC_HERMES_AGENT', False, True), ('HERMES_AGENT', False, True),
    (None, False, False), ('HERMES_AGENT', True, False),
])
def test_create_carries_start_time_ancestry(store, monkeypatch, marker, visible, expected):
    from hermes_cli import kanban_comment_writer as ancestry
    monkeypatch.setattr(ancestry.os, 'getpid', lambda: 999)
    monkeypatch.setattr(ancestry, '_read_proc', lambda pid: (998, {}) if pid == 999 else
                        (0, {marker: 'parent'} if marker else {}))
    monkeypatch.setenv('HERMES_AGENT', '1')  # main's setdefault must not hide a human CLI
    monkeypatch.setenv('HERMES_CHAT_VISIBLE', '1' if visible else '0')
    db, client = store
    db.create()
    params = client.calls[-1][1]
    assert params.get('hidden', False) is expected
    assert params.get('agent_cli', False) is (marker is not None)


def test_inconclusive_ancestry_stays_visible(store, monkeypatch):
    from hermes_cli import kanban_comment_writer as ancestry
    def unreadable(*args, **kwargs):
        raise ancestry.Inconclusive('unreadable')
    monkeypatch.setattr(ancestry, 'agent_ancestor', unreadable)
    db, client = store
    db.create()
    assert client.calls[-1][1].get('hidden', False) is False


def test_visible_chat_flag_is_parsed():
    import argparse
    from hermes_cli._parser import _build_chat_parser
    parser = argparse.ArgumentParser()
    _build_chat_parser(parser.add_subparsers())
    assert parser.parse_args(['chat', '--visible']).visible is True


@pytest.mark.parametrize('marker', ['LC_HERMES_AGENT', 'HERMES_AGENT'])
def test_real_parent_start_environment_is_detected(tmp_path, marker):
    import json
    import os
    import subprocess
    import sys
    child = tmp_path / 'visibility_child.py'
    child.write_text('import json; from hermes_cli.chat_visibility import creation_visibility; '
                     'print(json.dumps(creation_visibility()))')
    parent = tmp_path / 'visibility_parent.py'
    parent.write_text('import os, subprocess, sys; env=dict(os.environ); '
                      'env.pop(sys.argv[1], None); '
                      'subprocess.run([sys.executable, sys.argv[2]], env=env, check=True)')
    env = {k: v for k, v in os.environ.items() if not k.startswith(('HERMES_', 'LC_HERMES_'))}
    env.update({marker: 'proof-parent', 'PYTHONPATH': os.getcwd()})
    result = subprocess.run([sys.executable, str(parent), marker, str(child)], env=env,
                            capture_output=True, text=True, timeout=15, check=True)
    assert json.loads(result.stdout) == {'agent_cli': True, 'hidden': True}
