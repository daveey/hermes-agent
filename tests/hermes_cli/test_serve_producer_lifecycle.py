"""R: one mode, both directions, same serve PID; failed drain stays unavailable and fenced."""
import hashlib
import json
import os
import subprocess
import sys
import time

import pytest

from hermes_cli.serve_producer_admission import (
    REQUIRED_COLLECTORS, AdmissionCut, AdmissionUnavailable, CollectorEvidence, LaunchGate,
)
from hermes_cli.serve_producer_lifecycle import (
    DRAINING, EXTERNAL, OFF, STARTING, UNAVAILABLE, PanelLifecycle,
)
from hermes_cli.serve_producer_mode import MASTER_ENV, PANEL_ENV, initialize_mode, read_mode, write_mode
from hermes_cli.serve_producer_runtime import ProjectionRelay
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('posix')


def _cut(gate, installation, errors=()):
    collectors = tuple((name, CollectorEvidence((f'{name}:ok',), ())) for name in sorted(REQUIRED_COLLECTORS))
    return AdmissionCut(boot_id='boot', generation='a' * 64, installation=str(installation),
                        census=None, collectors=collectors, classifications=(), legacy_candidates=(),
                        retired=(), errors=tuple(errors), _acquisition=gate._acquisition)


class Harness:
    def __init__(self, tmp_path, *, errors=(), retire=True, publish=True, environ=None):
        self.tmp = tmp_path
        self.log = []
        self.gate: LaunchGate | None = None
        self.relays = []
        provider = tmp_path / 'provider.py'
        provider.write_text('import os\ndef produce(payload):\n    return {"pid": os.getpid()}\n')
        digest = hashlib.sha256(provider.read_bytes()).hexdigest()
        self.spec = {'path': str(provider), 'sha256': digest, 'manifest': {
            'version': 1, 'providers': {'p': {'path': 'provider.py', 'sha256': digest}},
            'callbacks': {'done': [], 'waiting': []}, 'modes': {}, 'facts': {}}}
        self.errors, self.retire, self.publish = errors, retire, publish
        self.lifecycle = PanelLifecycle(
            tmp_path, admit=self.admit, retire_legacy=self.retire_legacy, start_legacy=self.start_legacy,
            make_relay=self.make_relay, publish_initial=self.publish_initial,
            attach=lambda relay: self.log.append('attach'), detach=lambda relay: self.log.append('detach'),
            environ={} if environ is None else environ, close_timeout=10)

    def admit(self):
        self.log.append('admit')
        gate = LaunchGate.acquire(self.tmp / 'runtime')
        return gate, _cut(gate, self.tmp, self.errors)

    def retire_legacy(self):
        self.log.append('retire')
        return self.retire

    def start_legacy(self):
        self.log.append('legacy')

    def make_relay(self):
        relay = ProjectionRelay(self.spec, home=self.tmp, runtime=self.tmp / 'runtime',
                                generation=f'g{len(self.relays)}', initial={'ok': True})
        self.relays.append(relay)
        return relay

    def publish_initial(self, relay):
        self.log.append('publish')
        if not self.publish:
            return False
        key = relay.submit({'value': 1})
        wait_until(lambda: relay.read(key) is not None, timeout=30)
        return True


def test_both_directions_same_pid_with_ordered_drain(tmp_path):
    h = Harness(tmp_path)
    pid = os.getpid()
    lc = h.lifecycle
    assert lc.poll_once() == OFF and lc.revision_applied == 0 and h.log == []
    write_mode(tmp_path, external=True)
    assert lc.poll_once() == EXTERNAL
    assert h.log == ['admit', 'retire', 'publish', 'attach']  # retirement before child; publish before attach
    first = h.relays[0]
    key = first.submit({'value': 2})
    wait_until(lambda: first.read(key) is not None, timeout=30)
    write_mode(tmp_path, external=False)
    assert lc.poll_once() == OFF
    assert h.log[-2:] == ['detach', 'legacy']  # admission closed before the legacy owner returns
    assert not first.is_alive() and first.read(key) is None  # old outputs revoked/closed
    write_mode(tmp_path, external=True)
    assert lc.poll_once() == EXTERNAL
    assert h.log.count('retire') == 2, 'legacy was restarted on OFF, so the successor must positively retire it again'
    assert h.relays[1] is not first and h.relays[1].broker_pid != first.broker_pid
    write_mode(tmp_path, external=False)
    assert lc.poll_once() == OFF
    assert h.log.count('legacy') == 2
    assert os.getpid() == pid


def test_incomplete_admission_never_retires_legacy_or_starts_child(tmp_path):
    h = Harness(tmp_path, errors=('raw_ledger:missing',))
    write_mode(tmp_path, external=True)
    assert h.lifecycle.poll_once() == UNAVAILABLE
    assert 'retire' not in h.log and h.relays == [] and 'admission incomplete' in h.lifecycle.reason


def test_unretired_legacy_blocks_child_and_there_is_no_fallback(tmp_path):
    h = Harness(tmp_path, retire=False)
    write_mode(tmp_path, external=True)
    assert h.lifecycle.poll_once() == UNAVAILABLE
    assert h.relays == []
    assert 'legacy' not in h.log and 'attach' not in h.log
    write_mode(tmp_path, external=False)
    assert h.lifecycle.poll_once() == UNAVAILABLE, 'fenced until a supervised restart'


def test_failed_initial_publication_closes_child_and_never_attaches(tmp_path):
    h = Harness(tmp_path, publish=False)
    write_mode(tmp_path, external=True)
    assert h.lifecycle.poll_once() == UNAVAILABLE
    assert 'attach' not in h.log and 'legacy' not in h.log
    assert not h.relays[0].is_alive()


def test_surviving_descendant_keeps_unavailable_and_fenced(tmp_path):
    h = Harness(tmp_path)
    write_mode(tmp_path, external=True)
    assert h.lifecycle.poll_once() == EXTERNAL
    relay = h.relays[0]
    real_retire = relay.retire
    relay.retire = lambda timeout=10: False  # the family did not drain
    write_mode(tmp_path, external=False)
    assert h.lifecycle.poll_once() == UNAVAILABLE
    assert 'legacy' not in h.log and h.lifecycle.relay is relay
    assert h.lifecycle.reason == 'producer family still draining'
    write_mode(tmp_path, external=True)
    assert h.lifecycle.poll_once() == UNAVAILABLE
    relay.retire = real_retire
    assert relay.retire(timeout=10)


def test_superseded_transition_never_attaches(tmp_path):
    h = Harness(tmp_path)
    original = h.publish_initial

    def publish(relay):
        ok = original(relay)
        h.lifecycle._token += 1  # a retained callback from an older transition
        return ok
    h.lifecycle._publish_initial = publish
    write_mode(tmp_path, external=True)
    assert h.lifecycle.poll_once() == UNAVAILABLE
    assert 'attach' not in h.log and not h.relays[0].is_alive()


def test_master_off_overrides_stored_external_mode(tmp_path):
    h = Harness(tmp_path, environ={MASTER_ENV: '0'})
    write_mode(tmp_path, external=True)
    assert h.lifecycle.poll_once() == OFF
    assert h.log == [] and h.relays == []


def test_master_off_start_publishes_off_so_external_peers_drain(tmp_path):
    peer = Harness(tmp_path)
    write_mode(tmp_path, external=True)
    assert peer.lifecycle.poll_once() == EXTERNAL
    state = initialize_mode(tmp_path, {MASTER_ENV: '0'})
    assert state['external'] is False and state['revision'] == 2
    assert peer.lifecycle.poll_once() == OFF


def test_unset_env_never_overrides_stored_setter_and_panel_env_initializes(tmp_path):
    write_mode(tmp_path, external=True)
    assert initialize_mode(tmp_path, {}) == {'external': True, 'revision': 1}
    assert initialize_mode(tmp_path, {PANEL_ENV: 'false'}) == {'external': False, 'revision': 2}
    assert initialize_mode(tmp_path, {PANEL_ENV: 'false'}) == {'external': False, 'revision': 2}


def test_watch_thread_follows_the_real_cli_setter_without_restart(tmp_path):
    h = Harness(tmp_path)
    h.lifecycle.start_watching(interval=0.1)
    try:
        env = {**os.environ, 'HERMES_HOME': str(tmp_path)}
        for value, state in (('true', EXTERNAL), ('false', OFF)):
            proc = subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'producer-mode', '--panel-external', value,
                                   '--installation', str(tmp_path)], capture_output=True, text=True, env=env, timeout=120, check=False)
            assert proc.returncode == 0, proc.stderr
            wait_until(lambda: h.lifecycle.state == state and h.lifecycle.revision_applied == json.loads(proc.stdout)['revision'], timeout=60)
    finally:
        h.lifecycle.stop_watching()
    assert read_mode(tmp_path)['revision'] == 2


@pytest.mark.parametrize('refusal', ['false', 'exception'])
def test_failed_legacy_successor_never_claims_off_or_reopens_admission(tmp_path, refusal):
    h = Harness(tmp_path)
    write_mode(tmp_path, external=True)
    assert h.lifecycle.poll_once() == EXTERNAL
    first = h.lifecycle.relay
    assert first is not None
    def refused():
        if refusal == 'exception':
            raise RuntimeError('fixture custody unavailable')
        return False
    h.lifecycle._start_legacy = refused
    write_mode(tmp_path, external=False)
    assert h.lifecycle.poll_once() == UNAVAILABLE
    assert h.lifecycle.relay is None
    assert h.lifecycle._legacy_running is False
    assert first.retired
    assert h.lifecycle.reason is not None
    assert 'legacy startup failed' in h.lifecycle.reason
    write_mode(tmp_path, external=True)
    assert h.lifecycle.poll_once() == UNAVAILABLE
    assert len(h.relays) == 1



def test_roster_descriptor_is_resolved_under_the_held_launch_gate(tmp_path):
    """B02: the relay (home/roster binding) is built only while the admission gate is held and closes after."""
    h = Harness(tmp_path)
    held = []
    original_admit, original_make = h.admit, h.make_relay

    def admit():
        gate, cut = original_admit()
        h.gate = gate
        return gate, cut

    def make_relay():
        assert h.gate is not None
        held.append(h.gate.fd is not None and os.fstat(h.gate.fd).st_nlink >= 0)
        return original_make()

    h.lifecycle._admit, h.lifecycle._make_relay = admit, make_relay
    write_mode(tmp_path, external=True)
    h.lifecycle.poll_once()
    wait_until(lambda: h.lifecycle.state in (EXTERNAL, UNAVAILABLE), timeout=60)
    assert h.lifecycle.state == EXTERNAL, h.lifecycle.reason
    assert held == [True]
    with pytest.raises(AdmissionUnavailable):
        assert h.gate is not None
        h.gate.fd  # released only after the relay started
    write_mode(tmp_path, external=False)
    h.lifecycle.poll_once()
    wait_until(lambda: h.lifecycle.state in (OFF, UNAVAILABLE), timeout=60)
    assert h.lifecycle.state == OFF
