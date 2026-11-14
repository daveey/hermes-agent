"""Extension-code diagnostics must not weaken core/activity/update custody."""
import json
import hashlib
import shutil
from pathlib import Path

import pytest

from gateway import quiet_control
from tests.gateway import test_quiet_control as helpers
from tests.hermes_cli import test_code_attestation_route as route

# Keep the production bootstrap route, audit hook, file execution and native loader real.
core = route.core
python = route.python
env = helpers.env
pytestmark = pytest.mark.live_system_guard_bypass

PROBE = '''import json, os, sys, pathlib, types, time
from hermes_cli import code_attestation as ca, local_work, kanban_deploy_window as window
from gateway import quiet_control as qc
from gateway.run import GatewayRunner
from gateway.turn_lease import SessionTurnLeaseRegistry
from gateway.runtime_ownership import ProfileOwnership
from agent import claim_quiet as cq
root = pathlib.Path(os.environ['HERMES_HOME'])
plugin = root / 'plugins' / 'sample.py'
plugin.parent.mkdir(parents=True, exist_ok=True)
plugin.write_text('import dataclasses, ctypes\\n@dataclasses.dataclass\\nclass Point:\\n    x: int\\nlib = ctypes.CDLL(' + repr(os.environ['QUIET_NATIVE_LIBRARY']) + ')\\n')
import importlib.util
spec = importlib.util.spec_from_file_location('quiet_sample', plugin)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
assert mod.Point(3).x == 3
ledger = ca._ledger()
for index in range(25):
    ledger._module_file('extension_inventory_' + str(index), str(plugin))
# Core skew is exercised after the real extension has loaded.
if os.environ.get('QUIET_TAMPER'):
    victim = pathlib.Path(__file__).parent / 'quiet_victim.py'
    victim.write_text('value = 2\\n')
    exec(compile(victim.read_bytes(), str(victim), 'exec'), {})
qc.capture_bootstrap_registry()
registry = local_work.get_registry()
ownership = ProfileOwnership(track_membership=True)
ownership.reserve([root])
runner = object.__new__(GatewayRunner)
runner.session_runtime_descriptor = {'instance_id': 'fixture', 'state': 'ready'}
runner._turn_leases = SessionTurnLeaseRegistry()
runner._draining = runner._restart_requested = False
qc.bind_registry(runner)
qc.finalize_attestation(runner)
control = qc.QuietControl(runner)
p = window.window_path(); p.parent.mkdir(parents=True, exist_ok=True)
now = int(time.time())
p.write_text(json.dumps(dict(version=1, host=window.host_identity(), observed_at=now,
    window_at=now+3600, state='idle', state_since=now, drain_bound_s=60)))
check = control.check({'schema': 1}, 'fixture')
with cq.acquire(max_s=480, reason='isolated regression'):
    lease = cq.read_active()
    identity = control._gateway_identity()
    expected = {k: identity[k] for k in ('pid', 'started_at', 'instance_id', 'code_sha')}
    params = dict(schema=1, episode_id=lease.episode_id, producer_pid=lease.producer_pid,
        producer_started_at=lease.producer_started_at, producer_incarnation=lease.producer_incarnation,
        expected_gateway=expected, deadline_mono_ns=lease.deadline_ns)
    start = control.start(params, 'fixture')
    snap = end = None
    if 'observer_token' in start:
        follow = {k: params[k] for k in ('schema', 'episode_id', 'expected_gateway', 'deadline_mono_ns')}
        follow['observer_token'] = start['observer_token']
        snap = control.snapshot(follow, 'fixture')
        end = control.end(follow, 'fixture')
json.dump(dict(check=check, start=start, snapshot=snap, end=end, unknowns=ledger.unknowns()),
    open(os.environ['ATTEST_PROBE_OUT'], 'w'))
ownership.close()
'''


@pytest.fixture
def extension_core(core, tmp_path):
    dest = tmp_path / 'extension-core'
    shutil.copytree(core, dest)
    (dest / 'attest_probe.py').write_text(PROBE)
    (dest / 'quiet_victim.py').write_text('value = 1\n')
    route._git(dest, 'add', '-A')
    route._git(dest, 'commit', '-qm', 'extension fixture')
    return dest


@pytest.mark.parametrize('tampered', [False, True])
def test_real_plugin_dataclass_and_absolute_ctypes_through_gateway(tmp_path, extension_core, python, tampered):
    # An actual absolute native library, without invoking a compiler or host discovery subprocess.
    libraries = list(Path('/lib').glob('*/libc.so.6')) + list(Path('/lib64').glob('libc.so.6'))
    if not libraries:
        pytest.skip('absolute libc fixture requires a Linux native library')
    native = str(libraries[0].resolve())
    result = route.run_stub(tmp_path, python, route.current_script(extension_core), extra_env={
        'QUIET_NATIVE_LIBRARY': native, 'QUIET_TAMPER': 'yes' if tampered else ''})
    if tampered:
        assert not result['check']['admitted']
        assert any('source-skew:' in r for r in result['check']['reasons']), result
        return
    assert result['check']['admitted'], result
    for phase in ('check', 'start', 'snapshot', 'end'):
        report = result[phase]['unattested_code']
        assert report['count'] > 0 and len(report['sha256']) == 64
        entries = sorted(result['unknowns'])
        assert report['count'] == len(entries) > 20
        assert report['entries'] == entries[:20]
        assert report['sha256'] == hashlib.sha256(json.dumps(
            entries, ensure_ascii=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        assert any(r.startswith('native-load:') for r in report['entries']), report
        assert any(r.startswith('generated-origin-unknown:') for r in report['entries']), report
    assert result['start']['valid'] and result['snapshot']['valid'], result


def test_core_native_soname_is_not_external(tmp_path, extension_core, python):
    libraries = list(Path('/lib').glob('*/libc.so.6'))
    if not libraries or not Path('/proc/self/maps').exists():
        pytest.skip('loader-search regression requires Linux libc and proc maps')
    native = extension_core / 'libquietreview.so'
    shutil.copyfile(libraries[0].resolve(), native)
    probe = extension_core / 'attest_probe.py'
    probe.write_text(PROBE.replace(
        'assert mod.Point(3).x == 3',
        "assert mod.Point(3).x == 3\nassert str(pathlib.Path(__file__).parent / 'libquietreview.so') in pathlib.Path('/proc/self/maps').read_text()"))
    route._git(extension_core, 'add', '-A')
    route._git(extension_core, 'commit', '-qm', 'native loader search fixture')
    result = route.run_stub(tmp_path, python, route.current_script(extension_core), extra_env={
        'QUIET_NATIVE_LIBRARY': native.name, 'LD_LIBRARY_PATH': str(extension_core)})
    assert 'unknown:code:native-load:' + native.name in result['check']['reasons'], result
    assert not result['check']['admitted'] and not result['start']['valid']


@pytest.mark.parametrize('state,delta,age,foreign,hold,admitted', [
    ('batching', 3600, 0, False, False, True),
    ('batching', 600, 0, False, False, True),
    ('batching', 599, 0, False, False, False),
    ('batching', 0, 0, False, False, False),
    ('draining', 3600, 0, False, False, False),
    ('applying', 3600, 0, False, False, False),
    ('blocked', 3600, 0, False, False, False),
    ('unknown', 3600, 0, False, False, False),
    ('idle', 0, 600, False, False, True),
    ('idle', 0, 661, False, False, False),
    ('batching', 3600, 661, False, False, False),
    ('batching', 3600, 0, True, False, False),
    ('batching', 3600, 0, False, True, False),
])
def test_window_policy_matches_check_start_and_host(env, monkeypatch, state, delta, age, foreign, hold, admitted):
    helpers.attest(env)
    now = int(__import__('time').time())
    monkeypatch.setattr(quiet_control.time, 'time', lambda: now)
    helpers.write_window(env.root, state, age=age)
    from hermes_cli import kanban_deploy_window as window
    path = window.window_path()
    data = json.loads(path.read_text())
    data.update(window_at=now + delta)
    if foreign:
        data['host'] = 'foreign-fixture'
    path.write_text(json.dumps(data))
    if hold:
        helpers.write_update_hold(env.root, 'admission')
    check = env.control.check({'schema': 1}, helpers.PEER)
    start = env.control.start(helpers.start_params(env), helpers.PEER)
    assert check['admitted'] == admitted, check
    assert start['valid'] == admitted, start
    assert (not quiet_control.start_preflight_reasons(now=now, margin_s=0)) == admitted


def test_check_predicates_are_informational(env):
    helpers.attest(env)
    env.runner._running_agents['fixture'] = object()
    try:
        result = env.control.check({'schema': 1}, helpers.PEER)
        assert result['admitted']
        assert result['predicates']['active_agents'] == 1
    finally:
        env.runner._running_agents.clear()


@pytest.mark.parametrize('reason', [
    'unsupported-route:fixture', 'relaunch-exec-mismatch', 'source-skew:core.py',
    'residual:fixture', 'dependency-unreceipted:fixture', 'pth-unreceipted:fixture',
    'prehook-unrooted:fixture', 'non-source-module:fixture', 'fork-inherited-attestation',
    'unattested_bootstrap', 'activity', 'coverage_pending', 'future-kind:fixture',
    'unrooted-exec:/external.py', 'unrooted-module:plugin',
    'generated-origin-unknown:plugin', 'native-load:/library.so',
])
def test_registry_unknowns_are_never_informational(env, reason):
    helpers.attest(env)
    env.registry.mark_unknown(reason)
    assert 'unknown:' + reason in env.control.check({'schema': 1}, helpers.PEER)['reasons']
    assert 'unknown:' + reason in env.control.start(helpers.start_params(env), helpers.PEER)['reasons']


def test_unreadable_ledger_still_refuses(env, monkeypatch):
    helpers.attest(env)
    def unreadable():
        raise OSError('fixture unreadable')
    monkeypatch.setattr(env.runner._quiet_code, 'unknowns', unreadable)
    assert 'unknown:code:unreadable' in env.control.check({'schema': 1}, helpers.PEER)['reasons']
    assert 'unknown:code:unreadable' in env.control.start(helpers.start_params(env), helpers.PEER)['reasons']


@pytest.mark.parametrize('collision', ['draining', 'admission', 'drain'])
def test_far_batch_does_not_mask_mid_episode_collision(env, collision):
    helpers.attest(env)
    from hermes_cli import kanban_deploy_window as window
    helpers.write_window(env.root, 'batching')
    path = window.window_path()
    data = json.loads(path.read_text())
    data['window_at'] += 3600
    path.write_text(json.dumps(data))
    start = env.control.start(helpers.start_params(env), helpers.PEER)
    assert start['valid'], start
    if collision == 'draining':
        helpers.write_window(env.root, 'draining')
    else:
        helpers.write_update_hold(env.root, collision)
    snapshot = env.control.snapshot(helpers.follow_params(env, start['observer_token']), helpers.PEER)
    assert not snapshot['valid'] and snapshot['update']['collision'], snapshot


@pytest.mark.parametrize('elapsed', [1, 300, 479])
@pytest.mark.parametrize('advance_window', [False, True])
def test_admitted_batch_survives_elapsed_lease(env, monkeypatch, elapsed, advance_window):
    helpers.attest(env)
    from hermes_cli import kanban_deploy_window as window
    wall = int(__import__('time').time())
    mono = env.lease.deadline_ns - 480 * 1_000_000_000
    monkeypatch.setattr(quiet_control.time, 'time', lambda: wall)
    monkeypatch.setattr(env.control, '_mono', lambda: mono)
    helpers.write_window(env.root, 'batching')
    path = window.window_path()
    data = json.loads(path.read_text())
    data['window_at'] = wall + 600
    path.write_text(json.dumps(data))
    start = env.control.start(helpers.start_params(env), helpers.PEER)
    assert start['valid'], start
    wall += elapsed
    mono += elapsed * 1_000_000_000
    if advance_window:
        # Moving the update one second inside the remaining-lease safety margin refuses.
        data['window_at'] -= 1
        path.write_text(json.dumps(data))
    snapshot = env.control.snapshot(helpers.follow_params(env, start['observer_token']), helpers.PEER)
    assert snapshot['valid'] is (not advance_window), snapshot
    assert snapshot['update']['collision'] is advance_window


def test_check_unreadable_predicate_is_informational(env):
    helpers.attest(env)
    env.runner._turn_leases = None
    result = env.control.check({'schema': 1}, helpers.PEER)
    assert result['admitted']
    assert result['predicates']['held_turn_leases'] is None
    assert result['predicate_diagnostics'] == ['predicate_unreadable:held_turn_leases']
