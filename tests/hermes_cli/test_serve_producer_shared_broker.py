"""B: one shared installation owner (broker + exec child), real processes, no relay per serve.

Every case starts the production path under a held LaunchGate with a COMPLETE cut and real exec
processes. Provider code is written to disk and executed by the child only after the manifest passed.
"""
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import serve_producer_broker as shared
from hermes_cli.serve_producer_admission import (
    REQUIRED_COLLECTORS, AdmissionCut, CollectorEvidence, LaunchGate,
)
from hermes_cli.serve_producer_custody import CustodyConflict, FamilyCustody
from hermes_cli.serve_producer_runtime import ProjectionRelay, projection_response
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('linux')

ECHO = 'import os\ndef produce(payload):\n    return {"pid": os.getpid(), "echo": payload}\n'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(path, *extra):
    """Version-1 exact manifest: the provider itself plus ``extra`` sibling files as callbacks."""
    return {'version': 1,
            'providers': {'panel': {'path': path.name, 'sha256': digest(path)}},
            'callbacks': {'done': [{'name': p.name, 'path': p.name, 'sha256': digest(p)} for p in extra],
                          'waiting': []},
            'modes': {'panel': 'external'}, 'facts': {}}


def write_provider(tmp_path, source=ECHO, name='provider.py', extra=(), with_manifest=True):
    path = tmp_path / name
    path.write_text(source, encoding='utf-8')
    spec = {'path': str(path), 'sha256': digest(path)}
    if with_manifest:
        spec['manifest'] = manifest(path, *extra)
    return spec


def complete_cut(gate, installation):
    collectors = tuple((n, CollectorEvidence((f'{n}:ok',), ())) for n in sorted(REQUIRED_COLLECTORS))
    return AdmissionCut(boot_id='boot', generation='a' * 64, installation=str(installation),
                        census=None, collectors=collectors, classifications=(), legacy_candidates=(),
                        retired=(), errors=(), _acquisition=gate._acquisition)


COMPLETE_INITIAL = {'ok': True}


def start(spec, tmp_path, *, initial=COMPLETE_INITIAL, timeout=20, home=None):
    runtime = tmp_path / 'runtime'
    relay = ProjectionRelay(spec, home=home or tmp_path, runtime=runtime, generation='unused',
                            initial=initial)
    gate = LaunchGate.acquire(runtime)
    try:
        relay.start_for_production(cut=complete_cut(gate, tmp_path), gate=gate, ready_timeout=timeout)
    finally:
        gate.close()
    return relay


def custody_free(runtime, family='panel'):  # 'panel-execution' probes the child fence
    try:
        (FamilyCustody.acquire_execution(runtime, 'panel') if family == 'panel-execution'
         else FamilyCustody.acquire(runtime, family)).close()
    except CustodyConflict:
        return False
    return True


def produce(relay, **payload):
    key = relay.submit(payload)
    assert wait_until(lambda: relay.read(key) is not None, timeout=20)
    return json.loads(relay.read(key))


def test_two_clients_share_one_owner_epoch_and_child(tmp_path):
    spec = write_provider(tmp_path)
    first = start(spec, tmp_path)
    second = start(spec, tmp_path)
    try:
        assert first.broker_pid == second.broker_pid != os.getpid()
        assert first.config['generation'] == second.config['generation'] != 'unused'
        assert first.ping()['child_pid'] == second.ping()['child_pid']
        one, two = produce(first, value=1), produce(second, value=2)
        assert one['pid'] == two['pid'] == first.ping()['child_pid']
        assert second.ping()['clients'] >= 4  # two connections per attached serve
        # A serve detaching leaves the one owner running for the other.
        assert second.close(timeout=5)
        assert first.is_alive() and produce(first, value=3)['echo']['value'] == 3
    finally:
        assert first.retire(timeout=10)
    assert not first.is_alive()
    assert shared.read_owner(tmp_path / 'runtime') is None
    assert not (tmp_path / 'runtime' / shared.OWNER_FILE).exists()
    assert wait_until(lambda: custody_free(tmp_path / 'runtime'), timeout=10)


def test_spawning_serve_exit_does_not_end_the_shared_owner(tmp_path):
    spec = write_provider(tmp_path)
    first = start(spec, tmp_path)
    owner = first.broker_pid
    assert first.close(timeout=5)  # the spawning serve goes away
    second = start(spec, tmp_path)
    try:
        assert second.broker_pid == owner  # attached, not respawned
        assert produce(second, value=9)['echo']['value'] == 9
    finally:
        assert second.retire(timeout=10)


def test_startup_serve_exit_keeps_gate_until_initial_ready(tmp_path):
    """B05 only: synthetic cut isolates handoff; this is NOT full COMPLETE census acceptance."""
    release, entered = tmp_path / 'release', tmp_path / 'entered'
    source = ('from pathlib import Path\nimport select\n'
              'def produce(payload):\n'
              '    if "release" in payload:\n'
              '        Path(payload["entered"]).touch()\n'
              '        while not Path(payload["release"]).exists():\n'
              '            select.select([], [], [], .02)\n'
              '    return payload\n')
    spec = write_provider(tmp_path, source)
    spec_file = tmp_path / 'spec.json'
    spec_file.write_text(json.dumps(spec))
    launcher = tmp_path / 'collecting_serve.py'
    root = str(Path(__file__).parents[2])
    launcher.write_text(
        'import json, sys\nfrom pathlib import Path\n'
        f'sys.path.insert(0, {root!r})\n'
        'from tests.hermes_cli.test_serve_producer_shared_broker import start\n'
        'from hermes_cli.serve_producer_broker import OwnerUnavailable\n'
        'home = Path(sys.argv[1])\n'
        'try:\n'
        '    start(json.loads((home / "spec.json").read_text()), home, timeout=1,\n'
        '          initial={"release": str(home / "release"), "entered": str(home / "entered")})\n'
        'except OwnerUnavailable:\n'
        '    (home / "observer-exited").touch()\n')
    error_log = tmp_path / 'startup-error.log'
    with error_log.open('wb') as stderr:
        process = subprocess.Popen([sys.executable, str(launcher), str(tmp_path)],
                                   stdout=subprocess.DEVNULL, stderr=stderr)
    runtime = tmp_path / 'runtime'
    relay = None
    try:
        assert wait_until(entered.exists, timeout=20)
        assert process.wait(timeout=10) == 0, error_log.read_text()
        assert (tmp_path / 'observer-exited').exists()
        assert shared.read_owner(runtime) is None
        with pytest.raises(CustodyConflict):
            LaunchGate.acquire(runtime)
        release.touch()
        assert wait_until(lambda: shared.read_owner(runtime) is not None, timeout=10)
        relay = start(spec, tmp_path)
        assert produce(relay, recovered=True)['recovered'] is True
        # READY releases the short admission gate, not family/execution custody.
        LaunchGate.acquire(runtime).close()
    finally:
        release.touch()
        if process.poll() is None:
            process.wait(timeout=15)
        if relay is not None:
            assert relay.retire(timeout=10)


def test_ready_is_withheld_until_initial_publication_completes(tmp_path):
    gate_file, started = tmp_path / 'gate', tmp_path / 'started'
    source = ('from pathlib import Path\nimport select\n'
              'def produce(payload):\n'
              '    if "gate" in payload:\n'
              '        Path(payload["started"]).touch()\n'
              '        while not Path(payload["gate"]).exists():\n'
              '            select.select([], [], [], .02)\n'
              '    return payload\n')
    spec = write_provider(tmp_path, source)
    runtime = tmp_path / 'runtime'
    result = {}

    def boot():
        try:
            result['relay'] = start(spec, tmp_path, initial={'gate': str(gate_file), 'started': str(started)})
        except BaseException as exc:
            result['error'] = exc

    thread = threading.Thread(target=boot)
    thread.start()
    try:
        assert wait_until(started.exists, timeout=20)
        time.sleep(0.3)  # health: allow HX013 -- absence of READY is the behaviour: observed over a real interval while the provider blocks
        # Custody and the child exist, but there is no route: no record, no listener.
        assert shared.read_owner(runtime) is None
        assert not (runtime / shared.OWNER_FILE).exists() and not (runtime / shared.SOCKET_FILE).exists()
        # The original census gate remains locked across exec, custody, import and initial output.
        # A second launch cannot enter between the collecting serve and atomic READY.
        with pytest.raises(CustodyConflict):
            LaunchGate.acquire(runtime)
        assert 'relay' not in result
        gate_file.touch()
        thread.join(20)
        relay = result['relay']
    finally:
        gate_file.touch()
        thread.join(20)
    try:
        assert shared.read_owner(runtime)['pid'] == relay.broker_pid
    finally:
        assert relay.retire(timeout=10)


def test_failed_initial_publication_never_publishes_ready(tmp_path):
    source = ('def produce(payload):\n'
              '    if payload.get("fail"):\n        raise ValueError("no initial output")\n'
              '    return payload\n')
    spec = write_provider(tmp_path, source)
    runtime = tmp_path / 'runtime'
    with pytest.raises(shared.OwnerUnavailable):
        start(spec, tmp_path, initial={'fail': True}, timeout=5)
    assert shared.read_owner(runtime) is None and not (runtime / shared.OWNER_FILE).exists()
    assert wait_until(lambda: custody_free(runtime), timeout=10)  # nothing keeps the family fenced


def test_missing_initial_never_publishes_ready(tmp_path):
    """Production READY requires a completed initial payload, not a skipped produce."""
    spec = write_provider(tmp_path)
    runtime = tmp_path / 'runtime'
    with pytest.raises(shared.OwnerUnavailable):
        start(spec, tmp_path, initial=None, timeout=5)
    assert shared.read_owner(runtime) is None and not (runtime / shared.OWNER_FILE).exists()
    assert wait_until(lambda: custody_free(runtime), timeout=10)
    assert custody_free(runtime, 'panel-execution')


@pytest.mark.parametrize('mutation', ['missing', 'digest', 'callback', 'section'])
def test_manifest_refuses_before_custody_child_or_import(tmp_path, mutation):
    marker = tmp_path / 'imported'
    callback = tmp_path / 'done_callback.py'
    callback.write_text('VALUE = 1\n')
    source = f'open({str(marker)!r}, "w").write("imported")\ndef produce(payload):\n    return payload\n'
    spec = write_provider(tmp_path, source, extra=(callback,))
    if mutation == 'missing':
        spec.pop('manifest')
    elif mutation == 'digest':
        spec['manifest']['providers']['panel']['sha256'] = '0' * 64
    elif mutation == 'callback':
        callback.write_text('VALUE = 2\n')  # configured done callback changed after the manifest
    else:
        del spec['manifest']['callbacks']['waiting']  # absent is not "disabled"
    with pytest.raises(shared.OwnerUnavailable):
        start(spec, tmp_path, timeout=3)
    assert not marker.exists(), 'provider imported before its manifest verified'
    assert shared.read_owner(tmp_path / 'runtime') is None
    assert custody_free(tmp_path / 'runtime')
    assert custody_free(tmp_path / 'runtime', 'panel-execution')


def test_undeclared_loaded_module_refuses_before_ready_and_declared_one_serves(tmp_path):
    sibling = tmp_path / 'sibling_mod.py'
    sibling.write_text('VALUE = 7\n')
    source = ('import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\nimport sibling_mod\n'
              'def produce(payload):\n    return {"value": sibling_mod.VALUE}\n')
    undeclared = write_provider(tmp_path, source)
    with pytest.raises(shared.OwnerUnavailable):
        start(undeclared, tmp_path, timeout=5)
    assert shared.read_owner(tmp_path / 'runtime') is None
    assert wait_until(lambda: custody_free(tmp_path / 'runtime'), timeout=10)
    declared = write_provider(tmp_path, source, extra=(sibling,))
    relay = start(declared, tmp_path)
    try:
        assert produce(relay, x=1)['value'] == 7
    finally:
        assert relay.retire(timeout=10)


def test_tampered_loaded_module_digest_refuses_ready(tmp_path):
    sibling = tmp_path / 'sibling_mod.py'
    sibling.write_text('VALUE = 7\n')
    source = ('import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\nimport sibling_mod\n'
              'def produce(payload):\n    return {"value": sibling_mod.VALUE}\n')
    spec = write_provider(tmp_path, source, extra=(sibling,))
    modules = {'sibling_mod': type('M', (), {'__file__': str(sibling)})}
    from hermes_cli.serve_producer_manifest import verify_loaded
    from hermes_cli.serve_producer_protocol import ProtocolError
    with pytest.raises(ProtocolError, match='executable bytes unverified'):
        verify_loaded(spec['manifest'], tmp_path, allowed=('provider.py',), modules=modules)
    sibling.write_text('VALUE = 8\n')
    with pytest.raises(ProtocolError, match='digest mismatch'):
        verify_loaded(spec['manifest'], tmp_path, allowed=('provider.py',), modules=modules)


def test_incompatible_client_is_an_unavailable_observer_and_never_replaces_the_owner(tmp_path):
    first = start(write_provider(tmp_path), tmp_path)
    other = write_provider(tmp_path, ECHO + '# different bytes\n', name='other.py')
    try:
        with pytest.raises(shared.IncompatibleOwner):
            start(other, tmp_path, timeout=3)
        with pytest.raises(shared.IncompatibleOwner):
            start(write_provider(tmp_path), tmp_path, timeout=3, home=tmp_path / 'elsewhere')
        assert first.is_alive() and produce(first, value=1)['echo']['value'] == 1
    finally:
        assert first.retire(timeout=10)


def test_child_crash_restarts_once_for_every_client_before_acknowledging(tmp_path):
    source = ('from pathlib import Path\nimport os\n'
              'def produce(payload):\n'
              '    marker = payload.get("marker")\n    if marker is None:\n        return {"pid": os.getpid(), "echo": payload}\n    marker = Path(marker)\n'
              '    if not marker.exists():\n        marker.touch()\n        os._exit(7)\n'
              '    return {"pid": os.getpid()}\n')
    spec = write_provider(tmp_path, source)
    first, second = start(spec, tmp_path), start(spec, tmp_path)
    try:
        old = first.ping()['child_pid']
        marker = str(tmp_path / 'crashed')
        assert produce(first, marker=marker)['pid'] != old
        assert first.ping()['child_pid'] == second.ping()['child_pid'] != old
        assert produce(second, marker=marker, other=1)['pid'] == first.ping()['child_pid']
    finally:
        assert first.retire(timeout=10)


def _fork_descendant_source():
    return '''import os, select, sys
from pathlib import Path
home = Path(__file__).parent
def produce(payload):
    if not payload.get('go'):
        return {'ready': True}
    if (home / 'forked').exists():
        return {'overlap': True}
    (home / 'forked').touch()
    pid = os.fork()
    if pid == 0:
        os.close(int(sys.argv[2]))
        (home / 'descendant').write_text(str(os.getpid()), encoding='ascii')
        while not (home / 'release').exists():
            select.select([], [], [], .01)
        os._exit(0)
    while not (home / 'descendant').exists():
        select.select([], [], [], .01)
    os._exit(17)
'''


def test_surviving_descendant_fences_every_successor_and_no_ready_is_published(tmp_path):
    spec = write_provider(tmp_path, _fork_descendant_source())
    runtime = tmp_path / 'runtime'
    relay = start(spec, tmp_path)
    second = start(spec, tmp_path)
    try:
        key = relay.submit({'go': 1})
        assert wait_until(lambda: relay.error is not None or relay.read(key) is not None, timeout=20)
        assert relay.read(key) is None and not relay.acknowledged(key)
        assert projection_response(relay, {'sessions': ['x']}).status_code == 503
        # The owner drains itself (restart refused over the descendant); READY is withdrawn.
        assert wait_until(lambda: shared.read_owner(runtime) is None, timeout=20)
        assert wait_until(lambda: not relay.is_alive(), timeout=20)
        with pytest.raises(CustodyConflict):
            FamilyCustody.acquire(runtime, 'panel')
        with pytest.raises(CustodyConflict):
            FamilyCustody.acquire_execution(runtime, 'panel')
        with pytest.raises(shared.OwnerUnavailable):
            start(write_provider(tmp_path, ECHO, name='successor.py'), tmp_path, timeout=3)
        assert not (runtime / shared.OWNER_FILE).exists()
    finally:
        (tmp_path / 'release').touch()
        relay.close(timeout=5)
        second.close(timeout=5)
    assert wait_until(lambda: custody_free(runtime) and custody_free(runtime, 'panel-execution'), timeout=15)
    again = start(write_provider(tmp_path, ECHO, name='successor.py'), tmp_path)
    try:
        assert again.config['generation'] != relay.config['generation']
        assert produce(again, value=1)['echo']['value'] == 1
    finally:
        assert again.retire(timeout=10)


def test_owner_crash_with_blocked_child_keeps_the_fences_until_the_child_retires(tmp_path):
    gate, started = tmp_path / 'gate', tmp_path / 'started'
    source = ('from pathlib import Path\nimport select\n'
              'def produce(payload):\n'
              '    if "gate" in payload:\n'
              '        Path(payload["started"]).touch()\n'
              '        while not Path(payload["gate"]).exists():\n'
              '            select.select([], [], [], .02)\n'
              '    return payload\n')
    spec = write_provider(tmp_path, source)
    runtime = tmp_path / 'runtime'
    relay = start(spec, tmp_path)
    try:
        relay.submit({'gate': str(gate), 'started': str(started)})
        assert wait_until(started.exists, timeout=20)
        os.kill(relay.broker_pid, signal.SIGKILL)  # broker crash; the child is still inside the provider
        assert wait_until(lambda: not relay.is_alive(), timeout=10)
        assert shared.read_owner(runtime) is None  # a dead owner's record is never READY
        assert not custody_free(runtime) and not custody_free(runtime, 'panel-execution')
        with pytest.raises(shared.OwnerUnavailable):
            start(spec, tmp_path, timeout=2)
    finally:
        gate.touch()
        relay.close(timeout=5)
    assert wait_until(lambda: custody_free(runtime) and custody_free(runtime, 'panel-execution'), timeout=20)
    again = start(spec, tmp_path)
    try:
        assert again.config['generation'] != relay.config['generation']
    finally:
        assert again.retire(timeout=10)


def test_retire_over_a_blocked_child_reports_false_and_withdraws_ready(tmp_path):
    gate, started = tmp_path / 'gate', tmp_path / 'started'
    source = ('from pathlib import Path\nimport select\n'
              'def produce(payload):\n'
              '    if "gate" in payload:\n'
              '        Path(payload["started"]).touch()\n'
              '        while not Path(payload["gate"]).exists():\n'
              '            select.select([], [], [], .02)\n'
              '    return payload\n')
    spec = write_provider(tmp_path, source)
    runtime = tmp_path / 'runtime'
    relay = start(spec, tmp_path)
    try:
        relay.submit({'gate': str(gate), 'started': str(started)})
        assert wait_until(started.exists, timeout=20)
        assert relay.retire(timeout=0.5) is False  # still draining: retained, never killed
        assert shared.read_owner(runtime) is None  # READY withdrawn before the drain finished
        assert not custody_free(runtime)
    finally:
        gate.touch()
    assert wait_until(lambda: custody_free(runtime) and custody_free(runtime, 'panel-execution'), timeout=20)


def test_unauthorized_peers_are_refused_and_connections_are_bounded(tmp_path):
    from hermes_cli.serve_producer_runtime import Channel
    relay = start(write_provider(tmp_path), tmp_path)
    runtime = tmp_path / 'runtime'
    try:
        info = shared.read_owner(runtime)
        peer = Channel(shared.connect_owner(runtime, 5))
        regs = ({'verb': 'PING', 'capability': 'wrong', 'generation': info['generation']},
                {'verb': 'PING', 'capability': info['capability'], 'generation': 'old'},
                {'verb': 'REGISTER', 'capability': info['capability'], 'generation': info['generation']})
        for request in regs:
            peer.send(request, os.urandom(8).hex())
            assert peer.receive()['verb'] == 'REFUSED'
        peer.close()
        extra = [shared.connect_owner(runtime, 5) for _ in range(shared.MAX_CLIENTS + 2)]
        try:
            extra[-1].settimeout(5)
            assert extra[-1].recv(1) == b''  # over the bound: closed, never served
        finally:
            for sock in extra:
                sock.close()
        assert relay.ping()['verb'] == 'PONG'  # the owner kept serving its real clients
    finally:
        assert relay.retire(timeout=10)


def test_owner_record_is_private_regular_and_well_formed(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    record = runtime / shared.OWNER_FILE
    assert shared.read_owner(runtime) is None
    good = {'version': 1, 'ready': True, 'pid': os.getpid(), 'start': shared.process_start(os.getpid()),
            'capability': 'a' * 64, 'generation': 'b' * 32, 'digest': 'c' * 64}

    def write(value, mode=0o600):
        record.unlink(missing_ok=True)
        fd = os.open(record, os.O_WRONLY | os.O_CREAT, mode)
        os.write(fd, value if isinstance(value, bytes) else json.dumps(value).encode())
        os.close(fd)
        os.chmod(record, mode)

    write(good)
    assert shared.read_owner(runtime)['pid'] == os.getpid()
    write(dict(good, pid=2 ** 22 + 12345))  # not running: a stale record is absence
    assert shared.read_owner(runtime) is None
    write(dict(good, start='1'))  # same pid number, different incarnation
    assert shared.read_owner(runtime) is None
    for bad in (dict(good, ready=False), dict(good, version=2), dict(good, capability='z' * 64),
                dict(good, generation='b' * 31), dict(good, digest=1), dict(good, pid=True), b'not json',
                [1], dict(good, start=5)):
        write(bad)
        with pytest.raises(shared.OwnerUnavailable):
            shared.read_owner(runtime)
    write(good, mode=0o640)
    with pytest.raises(shared.OwnerUnavailable):
        shared.read_owner(runtime)
    record.unlink()
    target = tmp_path / 'elsewhere'
    target.write_text(json.dumps(good))
    record.symlink_to(target)
    with pytest.raises(shared.OwnerUnavailable):
        shared.read_owner(runtime)
    record.unlink()
    write(b'x' * (shared.MAX_OWNER_BYTES + 1))
    with pytest.raises(shared.OwnerUnavailable):
        shared.read_owner(runtime)


def test_zombie_owner_is_not_alive(tmp_path):
    import subprocess
    import sys
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    assert wait_until(lambda: shared.process_start(child.pid) is None, timeout=10)  # exited, unreaped
    child.wait()
    assert shared.process_start(os.getpid()) is not None


def test_socket_address_works_for_paths_longer_than_sun_path(tmp_path):
    deep = tmp_path
    for part in ('d' * 40, 'e' * 40, 'f' * 40):
        deep = deep / part
    spec = write_provider(tmp_path)
    relay = ProjectionRelay(spec, home=tmp_path, runtime=deep / 'runtime', generation='unused',
                            initial={'ok': True})
    deep.mkdir(parents=True)
    gate = LaunchGate.acquire(deep / 'runtime')
    try:
        relay.start_for_production(cut=complete_cut(gate, tmp_path), gate=gate, ready_timeout=20)
    finally:
        gate.close()
    try:
        assert relay.ping()['verb'] == 'PONG' and len(os.fsencode(str(deep / 'runtime' / shared.SOCKET_FILE))) > 100
    finally:
        assert relay.retire(timeout=10)


def test_second_serve_attaches_without_spawning_and_cannot_start_twice(tmp_path):
    spec = write_provider(tmp_path)
    first = start(spec, tmp_path)
    second = start(spec, tmp_path)
    try:
        assert first._spawned is not None and second._spawned is None  # joined, never a relay per serve
        runtime = tmp_path / 'runtime'
        gate = LaunchGate.acquire(runtime)
        try:
            with pytest.raises(RuntimeError, match='already started'):
                second.start_for_production(cut=complete_cut(gate, tmp_path), gate=gate, ready_timeout=5)
        finally:
            gate.close()
        assert second.is_alive()
    finally:
        second.close(timeout=5)
        assert first.retire(timeout=10)


def test_relay_config_carries_initial_only_when_given(tmp_path):
    spec = write_provider(tmp_path)
    plain = ProjectionRelay(spec, home=tmp_path, runtime=tmp_path / 'r', generation='g')
    assert 'initial' not in plain.config
    withit = ProjectionRelay(spec, home=tmp_path, runtime=tmp_path / 'r', generation='g', initial={'a': 1})
    assert withit.config['initial'] == {'a': 1}


def test_socket_address_is_direct_when_short_and_fd_relative_when_long(tmp_path):
    seen = []
    from pathlib import Path
    # Canonical pytest isolation may itself exceed sun_path; select a genuinely short path.
    short = Path('/short')
    shared._with_address(short, seen.append)
    assert seen == [str(short / shared.SOCKET_FILE)]
    deep = tmp_path
    for part in ('d' * 40, 'e' * 40, 'f' * 40):
        deep = deep / part
    deep.mkdir(parents=True)
    seen.clear()
    shared._with_address(deep, seen.append)
    assert seen[0].startswith('/proc/self/fd/') and seen[0].endswith('/' + shared.SOCKET_FILE)
    assert len(os.fsencode(seen[0])) < 100


@pytest.mark.parametrize('text', ['1 (a) R', '1 (a b) S ' + ' '.join(['0'] * 18), '1 (a) Z ' + ' '.join(['0'] * 30),
                                  '1 (a) X ' + ' '.join(['0'] * 30)])
def test_process_start_is_none_for_short_or_dead_state_records(tmp_path, monkeypatch, text):
    monkeypatch.setattr(shared.Path, 'read_text', lambda self, **kw: text)
    assert shared.process_start(1) is None
    monkeypatch.setattr(shared.Path, 'read_text', lambda self, **kw: '1 (a) S ' + ' '.join(str(i) for i in range(30)))
    assert shared.process_start(1) == '18'  # field 19 of the post-comm tail; zombie check is state only
