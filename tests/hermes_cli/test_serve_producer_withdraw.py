"""R: a withdrawn relay generation is dead; retained callbacks cannot revive it, and idle OFF watching is cheap."""
import hashlib

import pytest

from hermes_cli import serve_producer_lifecycle as lifecycle_mod
from hermes_cli.serve_producer_lifecycle import EXTERNAL, OFF, PanelLifecycle
from hermes_cli.serve_producer_mode import mode_path, write_mode
from hermes_cli.serve_producer_protocol import ResourceExhausted
from hermes_cli.serve_producer_runtime import ProjectionRelay
from tests.hermes_cli.test_serve_producer_lifecycle import Harness

pytestmark = pytest.mark.platforms('posix')


def _relay(tmp_path):
    provider = tmp_path / 'provider.py'
    provider.write_text('def produce(payload):\n    return {}\n')
    digest = hashlib.sha256(provider.read_bytes()).hexdigest()
    spec = {'path': str(provider), 'sha256': digest, 'manifest': {
        'version': 1, 'providers': {'p': {'path': 'provider.py', 'sha256': digest}},
        'callbacks': {'done': [], 'waiting': []}, 'modes': {}, 'facts': {}}}
    return ProjectionRelay(spec, home=tmp_path, runtime=tmp_path / 'runtime', generation='g', initial={'ok': True})


def test_withdraw_closes_admission_outputs_and_event_sink_attach(tmp_path):
    relay = _relay(tmp_path)
    assert relay.retired is False
    key = relay.submit({'value': 1})
    relay._outputs[key] = ({'v': 1}, 2 ** 62)
    assert relay.read(key) == {'v': 1}
    relay.set_event_sink(lambda payload: None)  # a live relay accepts its consumer
    relay.withdraw()
    assert relay.retired is True and relay.pending_count == 0
    assert relay.read(key) is None, 'withdrawn outputs are never observable'
    assert relay.acknowledged(key) is False
    with pytest.raises(ResourceExhausted):
        relay.submit({'value': 2})
    with pytest.raises(RuntimeError):
        relay.set_event_sink(lambda payload: None)  # a retained callback cannot re-attach
    relay.set_event_sink(None)  # detaching stays allowed


def test_a_closed_relay_is_as_dead_as_a_withdrawn_one(tmp_path):
    relay = _relay(tmp_path)
    key = relay.submit({'value': 1})
    relay._outputs[key] = ({'v': 1}, 2 ** 62)
    relay._pending.clear()  # a published output with no pending work: acknowledged until the relay dies
    assert relay.acknowledged(key) is True and relay.read(key) == {'v': 1}
    relay._stop.set()  # close()/retire() path, without withdraw()
    assert relay.retired is True and relay._withdrawn is False
    assert relay.read(key) is None and relay.acknowledged(key) is False
    with pytest.raises(ResourceExhausted):
        relay.submit({'value': 2})
    with pytest.raises(RuntimeError):
        relay.set_event_sink(lambda payload: None)
    relay.set_event_sink(None)


def test_withdrawn_alone_retires_without_a_stop(tmp_path):
    relay = _relay(tmp_path)
    key = relay.submit({'value': 1})
    relay._outputs[key] = ({'v': 1}, 2 ** 62)
    relay._pending.clear()
    assert relay.acknowledged(key) is True
    relay._withdrawn = True
    assert relay.retired is True and not relay._stop.is_set()
    assert relay.read(key) is None and relay.acknowledged(key) is False


def test_acknowledged_needs_a_published_output_and_no_pending_work(tmp_path):
    relay = _relay(tmp_path)
    key = relay.submit({'value': 1})
    assert relay.acknowledged(key) is False, 'enqueue is not an acknowledgement'
    relay._outputs[key] = ({'v': 1}, 2 ** 62)
    assert relay.acknowledged(key) is False, 'still pending: no ACK until the child publication is the last word'
    relay._pending.clear()
    assert relay.acknowledged(key) is True
    assert relay.acknowledged('absent') is False
    relay.set_event_sink(lambda payload: None)  # a live relay accepts a sink


def test_revoke_generation_alone_keeps_the_relay_usable(tmp_path):
    relay = _relay(tmp_path)
    relay.revoke_generation()  # a flush of one generation (roster change), not a retirement
    assert relay.retired is False
    assert relay.submit({'value': 3})
    relay.set_event_sink(lambda payload: None)


def test_to_off_withdraws_before_detach_and_drain(tmp_path):
    h = Harness(tmp_path)
    seen = []

    def detach(relay):
        seen.append(relay.retired)
        h.log.append('detach')
    h.lifecycle._detach = detach
    write_mode(tmp_path, external=True)
    assert h.lifecycle.poll_once() == EXTERNAL
    relay = h.relays[0]
    assert relay.retired is False
    write_mode(tmp_path, external=False)
    assert h.lifecycle.poll_once() == OFF
    assert seen == [True], 'admission is closed before detach and before the legacy owner returns'


def test_idle_watch_does_not_reread_an_unchanged_mode_file(tmp_path, monkeypatch):
    reads = []
    real = lifecycle_mod.read_mode
    monkeypatch.setattr(lifecycle_mod, 'read_mode', lambda installation: (reads.append(1), real(installation))[1])
    lc = PanelLifecycle(tmp_path, admit=None, retire_legacy=None, start_legacy=None, make_relay=None,
                        publish_initial=None, attach=None, detach=None, environ={})
    assert lc.poll_once() == OFF  # no mode file: revision 0 applied without creating state
    assert not mode_path(tmp_path).exists()
    first = len(reads)
    for _ in range(5):
        assert lc.poll_once() == OFF
    assert len(reads) == first, 'unchanged stat stamp: no further reads'
    write_mode(tmp_path, external=False)
    assert lc.poll_once() == OFF and lc.revision_applied == 1
    assert len(reads) > first, 'a changed stamp is read again'
    after = len(reads)
    assert lc.poll_once() == OFF and len(reads) == after
    write_mode(tmp_path, external=False)  # same content, new revision: the stamp still changes
    assert lc.poll_once() == OFF and lc.revision_applied == 2
