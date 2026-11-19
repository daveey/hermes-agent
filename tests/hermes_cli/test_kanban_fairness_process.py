"""Fresh-interpreter managed-worker capture startup, without providers or sockets.

The ownership transport/store and agent are fixtures; main/execute, policy binding,
profile config hydration, activation and urgency reader/journal are production code.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from hermes_cli.kanban_fairness_store import load


CHILD = r'''
import hermes_bootstrap
import json, os, sys, types
from pathlib import Path

def module(name, **items):
    m = types.ModuleType(name)
    m.__dict__.update(items)
    sys.modules[name] = m
    return m

class RPC:
    def __init__(self, home): pass
    def __call__(self, method, **kw):
        assert method == 'worker.adopt'
        return {'owner_epoch': 1}
    def close(self): pass
class Store:
    def __init__(self, *a): pass
    def get_messages_as_conversation(self, sid): return []
    def flush_token_counts(self): pass
    def finish(self): pass
    def close(self): pass
module('agent.runtime_session_store', RuntimeSessionStore=Store, WorkerRPC=RPC)
module('gateway.session_kanban', bind_worker_context=lambda f: None,
       run_worker_turns=lambda a, f, h: a.run_conversation(),
       record_worker_failure=lambda *a: None)
module('agent.origin_managed_custody', bind_adopted_agent=lambda *a, **k: None)
module('tools.process_registry', process_registry=types.SimpleNamespace(recover_from_checkpoint=lambda **k: None))
module('gateway.session_worker_construct', checkpoint_agent_kwargs=lambda c: {},
       construct_kwargs=lambda f: {}, routing_agent_kwargs=lambda *a: {}, worker_route=lambda *a: {})
module('agent.title_generator', wait_for_title_upgrades=lambda: None)

class Agent:
    def __init__(self, **kw):
        from hermes_cli.config import load_config
        from hermes_cli import kanban_fairness_runtime as runtime
        from hermes_cli.kanban_turn_urgency import read_owners
        cfg = load_config()
        assert cfg['kanban']['max_running'] == 2
        self.model, self.provider = 'fixture-model', 'custom'
        home = Path(os.environ['HERMES_HOME'])
        other = home.parent / 'other'
        before = runtime._key('default', other) in runtime._SESSIONS
        read_owners(board='default', now_s=150, path=home / 'missing-urgency')
        from agent.safe_worker_policy import worker_config_snapshot
        self.answer = json.dumps({'active': runtime.enabled(board='default'), 'other_active': before,
                                  'config': worker_config_snapshot()['kanban'], 'pid': os.getpid()})
    def run_conversation(self): return {'final_response': self.answer, 'completed': True}
    def shutdown_memory_provider(self, messages): pass
    def release_clients(self): pass
import agent.managed_worker as worker
worker.discover_profile_mcp = lambda policy: None
bind = worker.bind_worker_policy
def bind_then_install(frame):
    bind(frame)
    module('run_agent', AIAgent=Agent)
    # Hook discovery is outside this startup contract; do not load user plugins.
    import gateway.session_policy as policies
    policies.register_worker_hooks = lambda policy: None
worker.bind_worker_policy = bind_then_install
raise SystemExit(worker.main())
'''


@pytest.mark.parametrize('mode', ['armed', 'unarmed', 'disabled', 'safe', 'ignore'])
def test_managed_execute_capture_startup_and_profile_isolation(tmp_path, monkeypatch, mode):
    from agent.managed_worker import encode_frame
    from gateway.session_policy import LocalSessionPolicy
    from gateway.session_managed_worker import _worker_config
    from hermes_cli import kanban_fairness_activation as activation

    home = tmp_path / 'owner'
    home.mkdir()
    other = tmp_path / 'other'
    other.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    capture = {'enabled': mode != 'disabled', 'capture_id': 'process-fixture',
               'directory': str(home / 'capture')}
    live = {'kanban': {'fairness_capture': capture, 'max_running': 99}}
    (home / 'config.yaml').write_text(json.dumps(live))
    if mode in ('armed', 'safe', 'ignore'):
        activation.arm(live, home=home, board='default', source_gateway='fixture-gateway', board_uid='fixture-board')
    # A second profile really has a valid arm, not merely a missing directory.
    other_cfg = {'kanban': {'fairness_capture': {**capture, 'enabled': True,
                                              'directory': str(other / 'capture')}}}
    activation.arm(other_cfg, home=other, board='default', source_gateway='other-gateway', board_uid='other-board')
    (other / 'config.yaml').write_text(json.dumps(other_cfg))
    bypass = mode in ('safe', 'ignore')
    frozen = {'kanban': {'max_running': 2}}
    policy = LocalSessionPolicy(source='cli', platform='cli', cwd=str(home), model='fixture-model',
                                toolsets=(), config_json=json.dumps(frozen), request_json='{}',
                                terminal_json=json.dumps({'TERMINAL_CWD': str(home)}),
                                safe_mode=mode == 'safe', ignore_user_config=bypass)
    hydrated = _worker_config(SimpleNamespace(profile_id=str(home)), policy)
    assert hydrated['kanban']['max_running'] == 2
    assert ('fairness_capture' in hydrated['kanban']) is not bypass
    policy = replace(policy, config_json=json.dumps(hydrated))
    # Mutate disk after owner hydration: the child must use its assigned snapshot.
    (home / 'config.yaml').write_text(json.dumps({'kanban': {'max_running': 777}}))
    root = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED') if k in os.environ}
    user = tmp_path / 'user'
    user.mkdir()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root))
    # The script imports the real bootstrap before any runtime dependencies.
    proc = subprocess.Popen([sys.executable, '-c', CHILD], cwd=root, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # communicate supplies the bounded bootstrap and finish without orphaning the controls reader.
        frame = {'version': 1, 'home': str(home), 'scope': {
            'profile_id': str(home), 'session_id': 'fixture-session', 'execution_id': 'fixture-execution',
            'generation': 1, 'pid': proc.pid, 'birth': 'fixture-birth', 'secret': 'fixture-secret',
            'epoch': 1}, 'policy': asdict(policy), 'api_key': None,
            'text': 'fixture', 'route': 'fixture-route', 'user_id': 'fixture-user', 'chat_id': 'fixture-chat',
            'turn_author': None, 'safe_mode': mode == 'safe', 'ignore_user_config': bypass}
        out, err = proc.communicate(encode_frame(frame) + encode_frame({'type': 'finish'}), timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
    assert proc.returncode == 0, err.decode()
    frames = [json.loads(line) for line in out.splitlines()]
    assert [f['type'] for f in frames] == ['hello', 'ready', 'result', 'finished'], (frames, err.decode())
    answer = json.loads(frames[2]['result']['final_response'])
    assert answer['pid'] == proc.pid
    assert answer['active'] is (mode == 'armed')
    assert answer['other_active'] is False
    assert answer['config'] == hydrated['kanban']
    assert not (other / 'capture' / 'runtime').exists()
    epochs = list((home / 'capture' / 'runtime').iterdir()) if (home / 'capture' / 'runtime').exists() else []
    assert len(epochs) == (1 if mode == 'armed' else 0)
    if epochs:
        records = load(epochs[0])['records']
        receipts = [r for r in records if r['kind'] == 'owner_generation']
        assert len(receipts) == 1, records
        assert receipts[0]['payload']['outcome'] == 'missing'
        assert receipts[0]['source_gateway'] == 'fixture-gateway'
        assert receipts[0]['board_uid'] == 'fixture-board'
