"""M1(B) process clause boundaries: manifest gating in ``serve_producer_process``.

Pins, with real code at each seam (real ``load_provider``/``producer`` calls over a real socketpair
``Channel``, and the real exec ``main`` dispatch through ``spawn``), that:

* a SHARED config with no manifest is refused before any provider import, while a non-shared one
  with no manifest still loads (missing is only refused for the shared owner);
* a declared manifest is verified before import (bad digest refuses, no side effect), and an absent
  manifest never reaches ``verify_declared``/``verify_loaded``;
* the producer verifies what the import loaded only when a manifest is declared;
* ``main`` sends ``shared`` brokers to ``run_shared`` and legacy brokers to ``broker``.
"""
import hashlib
import socket
import sys

import pytest

from hermes_cli import serve_producer_process as process
from hermes_cli.serve_producer_protocol import ProtocolError
from hermes_cli.serve_producer_runtime import Channel, ProjectionRelay, spawn
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('posix')

MODULE = '_hermes_scoped_producer_v1'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(source, encoding='utf-8')
    return path


def provider(tmp_path, *, imports=None, extra_manifest=()):
    """Provider that leaves a marker when imported; optionally imports a sibling module."""
    marker = tmp_path / 'imported'
    body = f'open({str(marker)!r}, "w").write("imported")\n'
    if imports:
        body += f'import sys\nsys.path.insert(0, {str(tmp_path)!r})\nimport {imports}\n'
    body += 'def produce(payload):\n    return payload\n'
    return write(tmp_path, 'provider.py', body), marker


def manifest(path, *siblings, sha=None):
    return {'version': 1,
            'providers': {'panel': {'path': path.name, 'sha256': sha or digest(path)}},
            'callbacks': {'done': [{'name': s.name, 'path': s.name, 'sha256': digest(s)} for s in siblings],
                          'waiting': []},
            'modes': {'panel': 'external'}, 'facts': {}}


def config(tmp_path, path, *, shared=None, declared=None, generation='g'):
    spec = {'path': str(path), 'sha256': digest(path)}
    if declared is not None:
        spec['manifest'] = declared
    cfg = {'provider': spec, 'home': str(tmp_path), 'runtime': str(tmp_path),
           'generation': generation, 'capability': 'c' * 64}
    if shared is not None:
        cfg['shared'] = shared
    return cfg


@pytest.fixture(autouse=True)
def _clean_import_state(monkeypatch):
    """load_provider/producer register a module and the provider may extend sys.path."""
    monkeypatch.setattr(sys, 'path', list(sys.path))
    before = set(sys.modules)
    sys.modules.pop(MODULE, None)
    yield
    for name in set(sys.modules) - before:
        sys.modules.pop(name, None)
    sys.modules.pop(MODULE, None)


# --- load_provider: the shared/missing-manifest gate ---------------------------------------------

@pytest.mark.parametrize('shared', [None, False])
def test_nonshared_without_manifest_still_loads(tmp_path, shared):
    path, marker = provider(tmp_path)
    module = process.load_provider(config(tmp_path, path, shared=shared))
    assert marker.read_text() == 'imported'
    assert module.produce({'a': 1}) == {'a': 1}


def test_shared_without_manifest_refuses_before_import(tmp_path):
    path, marker = provider(tmp_path)
    with pytest.raises(ProtocolError, match='^manifest required$'):
        process.load_provider(config(tmp_path, path, shared=True))
    assert not marker.exists(), 'provider code ran although the shared manifest was missing'
    assert MODULE not in sys.modules


def test_shared_with_valid_manifest_loads(tmp_path):
    path, marker = provider(tmp_path)
    module = process.load_provider(config(tmp_path, path, shared=True, declared=manifest(path)))
    assert marker.read_text() == 'imported'
    assert module.produce(3) == 3


# --- load_provider: verify_declared runs exactly when a manifest is declared ---------------------

@pytest.mark.parametrize('shared', [None, True])
def test_declared_bad_digest_refuses_before_import(tmp_path, shared):
    path, marker = provider(tmp_path)
    bad = manifest(path, sha='0' * 64)
    with pytest.raises(ProtocolError, match='^manifest digest mismatch$'):
        process.load_provider(config(tmp_path, path, shared=shared, declared=bad))
    assert not marker.exists(), 'provider code ran before its manifest was verified'


def test_declared_malformed_manifest_refuses_before_import(tmp_path):
    path, marker = provider(tmp_path)
    with pytest.raises(ProtocolError, match='^manifest version$'):
        process.load_provider(config(tmp_path, path, declared={'version': 2}))
    assert not marker.exists()


def test_declared_exact_manifest_loads_nonshared(tmp_path):
    path, marker = provider(tmp_path)
    process.load_provider(config(tmp_path, path, declared=manifest(path)))
    assert marker.read_text() == 'imported'


# --- producer: verify_loaded runs exactly when a manifest is declared ----------------------------

def run_producer(tmp_path, cfg):
    """Run the real child entry over a real Channel; the peer queues STOP so it returns after READY."""
    child_sock, peer_sock = socket.socketpair()
    child, peer = Channel(child_sock), Channel(peer_sock)
    try:
        peer.send({'verb': 'STOP'}, 'stop')
        process.producer(child, cfg)
        child.close()
        return peer.receive()
    finally:
        peer.close()
        try:
            child.close()
        except OSError:
            pass


def test_producer_without_manifest_does_not_verify_loaded(tmp_path):
    # An undeclared sibling is fine when no manifest exists: verify_loaded must not even run
    # (it would refuse a None manifest with 'manifest version').
    write(tmp_path, 'helper_nomanifest.py', 'VALUE = 1\n')
    path, marker = provider(tmp_path, imports='helper_nomanifest')
    ready = run_producer(tmp_path, config(tmp_path, path))
    assert ready['verb'] == 'READY'
    assert marker.read_text() == 'imported'
    assert (tmp_path / 'projection.sqlite3').exists()


def test_producer_shared_without_manifest_refuses_before_ready(tmp_path):
    path, marker = provider(tmp_path)
    with pytest.raises(ProtocolError, match='^manifest required$'):
        run_producer(tmp_path, config(tmp_path, path, shared=True))
    assert not marker.exists()
    assert not (tmp_path / 'projection.sqlite3').exists()


def test_producer_bad_manifest_refuses_before_import_and_ready(tmp_path):
    path, marker = provider(tmp_path)
    with pytest.raises(ProtocolError, match='^manifest digest mismatch$'):
        run_producer(tmp_path, config(tmp_path, path, declared=manifest(path, sha='1' * 64)))
    assert not marker.exists()
    assert not (tmp_path / 'projection.sqlite3').exists()


def test_producer_refuses_undeclared_module_loaded_by_import(tmp_path):
    helper = write(tmp_path, 'helper_undeclared.py', 'VALUE = 1\n')
    path, _ = provider(tmp_path, imports='helper_undeclared')
    with pytest.raises(ProtocolError, match='^undeclared provider module executed$'):
        run_producer(tmp_path, config(tmp_path, path, shared=True, declared=manifest(path)))
    assert helper.exists()
    assert not (tmp_path / 'projection.sqlite3').exists(), 'READY path ran after an undeclared import'


def test_producer_refuses_declared_sibling_with_wrong_digest_before_import(tmp_path):
    helper = write(tmp_path, 'helper_changed.py', 'VALUE = 1\n')
    path, _ = provider(tmp_path, imports='helper_changed')
    declared = manifest(path, helper)
    declared['callbacks']['done'][0]['sha256'] = '2' * 64
    with pytest.raises(ProtocolError, match='^manifest digest mismatch$'):
        run_producer(tmp_path, config(tmp_path, path, declared=declared))


def test_producer_accepts_fully_declared_modules(tmp_path):
    helper = write(tmp_path, 'helper_declared.py', 'VALUE = 1\n')
    path, marker = provider(tmp_path, imports='helper_declared')
    ready = run_producer(tmp_path, config(tmp_path, path, shared=True, declared=manifest(path, helper)))
    assert ready['verb'] == 'READY'
    assert marker.read_text() == 'imported'


# --- main: broker role dispatch through the real exec seam ---------------------------------------

def startup_reply(cfg):
    """Real exec ``main`` in a child; return the startup channel's first message and the process."""
    process_, channel = spawn('broker', cfg)
    channel.sock.settimeout(30)
    try:
        return process_, channel, channel.receive()
    except BaseException:
        channel.close()
        process_.kill()
        process_.wait(10)
        raise


def test_main_shared_broker_dispatches_to_run_shared_and_refuses_missing_manifest(tmp_path):
    path, marker = provider(tmp_path)
    process_, channel, reply = startup_reply(config(tmp_path, path, shared=True))
    try:
        assert reply == {'verb': 'REFUSED', 'error': 'manifest'}
        assert process_.wait(30) == 0
    finally:
        channel.close()
        if process_.poll() is None:
            process_.kill()
            process_.wait(10)
    assert not marker.exists(), 'provider imported despite the shared manifest refusal'


def test_main_legacy_broker_dispatches_to_broker_and_serves_without_manifest(tmp_path):
    # Non-shared, no manifest: ``broker`` serves it. run_shared would REFUSE it ('manifest'), and a
    # dropped legacy branch would raise 'unknown fixture role' before any READY.
    path, marker = provider(tmp_path)
    relay = ProjectionRelay({'path': str(path), 'sha256': digest(path)}, home=tmp_path,
                            runtime=tmp_path / 'runtime', generation='g')
    try:
        relay.start_for_fixture(ready_timeout=30)
        key = relay.submit({'value': 7})
        wait_until(lambda: relay.read(key) is not None, timeout=30)
        assert marker.read_text() == 'imported'
    finally:
        assert relay.close(timeout=10)


def test_main_unknown_role_refuses(tmp_path, monkeypatch):
    # Pin the final ``else``: only the three roles dispatch; nothing else reaches the broker.
    child_sock, peer_sock = socket.socketpair()
    peer = Channel(peer_sock)
    peer.send({'capability': 'x'}, 'init')
    monkeypatch.setattr(sys, 'argv', ['serve_producer_process.py', 'bogus', str(child_sock.detach())])
    try:
        with pytest.raises(ProtocolError, match='^unknown fixture role$'):
            process.main()
    finally:
        peer.close()
