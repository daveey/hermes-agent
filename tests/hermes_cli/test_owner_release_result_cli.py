"""Actual CLI/socket completion and reopen, with labelled fixture Board adapter.

The private output is a rebound HTTP/DO fixture, not live DO/HOME composition.
"""
import json
import os
import subprocess
import sys

import pytest

from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_native_owner_release_cli as helpers
from tests.hermes_cli import test_owner_release_result as evidence
from tools import origin_cli_bridge as bridge
from tools.environments.local import build_subprocess_env

turn = helpers.turn
transport = helpers.transport


def cli(native, *extra):
    action=evidence.action()
    env=build_subprocess_env(scrub_secrets=True)
    env['PYTHONPATH']=os.getcwd()
    env[bridge.ENV_KEY]=native.locator
    argv=[sys.executable,'-m','hermes_cli.main','kanban','release',action['task_id'],
          '--episode',action['episode'],'--runs',str(action['runs']),'--note',action['note'],'--json']
    for episode in action['capability_episodes']:
        argv.extend(['--capability-episode',episode])
    return subprocess.run([*argv,*extra],env=env,pass_fds=(native.child_fd,),capture_output=True,
                          text=True,encoding='utf-8',timeout=25,check=False)


def publish(native):
    first=cli(native)
    assert first.returncode==0,(first.stdout,first.stderr)
    nonce=json.loads(first.stdout)['release_read']['nonce']
    ack=cli(native,'--seen-release',nonce)
    assert ack.returncode==0,(ack.stdout,ack.stderr)
    assert json.loads(ack.stdout)['publication_stage']=='published'


def private_fixture(transport):
    wire=transport.carriers[0]
    body=transport.sent[0]['intent']
    return evidence.application(body,wire)


def test_actual_cli_completed_result_wins_after_carrier_loss_and_replays_after_reopen(turn,transport,monkeypatch):
    native=helpers.launch(monkeypatch)
    try:
        publish(native)
        value=private_fixture(transport)
        calls=[]
        def completed(self,board,uid):
            calls.append(uid)
            return value
        monkeypatch.setattr(transport,'owner_release_application',completed)
        def unavailable(*args,**kwargs):
            pytest.fail('completed historical result must not use mutable preflight/publication')
        transport.support=0
        transport.carrier_rows.clear()
        monkeypatch.setattr(transport,'row_evidence',unavailable)
        monkeypatch.setattr(transport,'owner_release_comments',unavailable)
        monkeypatch.setattr(transport,'owner_release_evidence',unavailable)
        result=cli(native)
        assert result.returncode==0,(result.stdout,result.stderr)
        got=json.loads(result.stdout)
        assert got['release_stage']=='applied' and got['historical'] is True
        assert got['execution_clearance'] is False
        assert got['result']==value['operation']['proposal']['application']['result']
        with kbc.connect_closing() as conn:
            assert conn.execute('SELECT stage FROM native_owner_release_intents').fetchone()[0]=='applied'
            assert conn.execute("SELECT count(*) FROM task_events WHERE kind='stop_released'").fetchone()[0]==0
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_release_home_queue'").fetchone()
        monkeypatch.setattr(transport,'owner_release_application',unavailable)
        again=cli(native)
        assert again.returncode==0,(again.stdout,again.stderr)
        assert json.loads(again.stdout)==got
        assert calls==[value['consumption']['intent']['intent_uid']]
        assert len(transport.carriers)==1 and len(transport.sent)==1
    finally:
        helpers.close(native)


def test_actual_cli_successor_read_and_delivery_progress_after_completed_reopen(turn,transport,monkeypatch):
    native=helpers.launch(monkeypatch)
    try:
        publish(native)
        value=private_fixture(transport)
        original=transport.owner_release_application
        monkeypatch.setattr(transport,'owner_release_application',lambda *args:value)
        assert json.loads(cli(native).stdout)['release_stage']=='applied'
        monkeypatch.setattr(transport,'owner_release_application',original)
        first=cli(native,'--episode','successor-scope')
        assert first.returncode==0,(first.stdout,first.stderr)
        nonce=json.loads(first.stdout)['release_read']['nonce']
        ack=cli(native,'--episode','successor-scope','--seen-release',nonce)
        assert ack.returncode==0,(ack.stdout,ack.stderr)
        assert json.loads(ack.stdout)['publication_stage']=='published'
        with kbc.connect_closing() as conn:
            assert sorted(r[0] for r in conn.execute('SELECT stage FROM native_owner_release_intents'))==['applied','delivery_pending']
    finally:
        helpers.close(native)


def test_actual_cli_private_reply_loss_keeps_one_delivery_then_recovers(turn,transport,monkeypatch):
    native=helpers.launch(monkeypatch)
    try:
        publish(native)
        value=private_fixture(transport)
        def lost(*args):
            raise OSError('fixture private reply lost')
        monkeypatch.setattr(transport,'owner_release_application',lost)
        failed=cli(native)
        assert failed.returncode==2,(failed.stdout,failed.stderr)
        assert json.loads(failed.stdout)['outcome']=='outcome_unknown'
        with kbc.connect_closing() as conn:
            assert conn.execute('SELECT stage FROM native_owner_release_intents').fetchone()[0]=='delivery_pending'
        monkeypatch.setattr(transport,'owner_release_application',lambda *args:value)
        recovered=cli(native)
        assert recovered.returncode==0,(recovered.stdout,recovered.stderr)
        assert json.loads(recovered.stdout)['release_stage']=='applied'
        assert len(transport.carriers)==1 and len(transport.sent)==1
    finally:
        helpers.close(native)
