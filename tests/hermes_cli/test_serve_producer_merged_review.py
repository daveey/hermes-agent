"""Regressions for the seven behavior findings in merged PR 4420."""
import copy
import importlib.util
import os
import py_compile
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import serve_producer_broker as shared
from hermes_cli import serve_producer_home as homes
from hermes_cli import serve_producer_runtime as runtime
from hermes_cli.serve_producer_admission import LaunchGate
from hermes_cli.serve_producer_home import HomeBinding, HomeRevoked
from hermes_cli.serve_producer_protocol import ResourceExhausted
from tests.hermes_cli.test_serve_producer_event_attach import Channel
from tests.hermes_cli.test_serve_producer_home import Authority
from tests.hermes_cli.test_serve_producer_observation import observe
from tests.hermes_cli.test_serve_producer_shared_broker import complete_cut, produce, start, write_provider



def relay_at(tmp_path):
    relay = runtime.ProjectionRelay({'partitioned': True}, home=tmp_path,
                                    runtime=tmp_path / 'runtime', generation='g')
    relay.bind_partition(HomeBinding('/homes/a', 'life-a', 'auth-a'))
    return relay


@pytest.mark.parametrize('revoke', ['unbind_partition', 'withdraw', 'replace_sink'])
def test_events_delivered_after_revocation(tmp_path, revoke):
    relay = relay_at(tmp_path)
    seen, replacement = [], []

    def sink(payload):
        seen.append(payload)
        if revoke == 'replace_sink':
            relay.set_event_sink(replacement.append)
        else:
            getattr(relay, revoke)()

    relay.set_event_sink(sink)
    relay._event_cursor = 0
    relay._channel = Channel({'verb': 'EVENTS', 'key': 'events', 'generation': 'g', 'seq': 2,
                              'events': [[1, {'n': 1}], [2, {'n': 2}]]})
    relay._poll_events()
    assert seen == [{'n': 1}]
    assert replacement == []


def test_submit_stamped_with_revoked_home(tmp_path, monkeypatch):
    relay = relay_at(tmp_path)
    original = copy.deepcopy

    def rebind(payload):
        relay.bind_partition(HomeBinding('/homes/b', 'life-b', 'auth-b'))
        return original(payload)

    monkeypatch.setattr(runtime.copy, 'deepcopy', rebind)
    try:
        relay.submit({'body': {'value': 1}})
    except ResourceExhausted:
        pass
    assert relay.pending_count == 0
    assert relay._bytes == 0


@pytest.mark.platforms('linux')
def test_refused_broker_left_zombie(tmp_path):
    relay = runtime.ProjectionRelay(write_provider(tmp_path), home=tmp_path,
                                    runtime=tmp_path / 'runtime', generation='g')
    gate = LaunchGate.acquire(tmp_path / 'runtime')
    try:
        with pytest.raises(shared.OwnerUnavailable):
            relay.start_for_production(cut=complete_cut(gate, tmp_path), gate=gate, ready_timeout=.2)
    finally:
        gate.close()
    process = relay._spawned
    try:
        assert relay.retire(timeout=2)
        assert not Path(f'/proc/{process.pid}').exists(), 'retired owned broker was not reaped'
    finally:
        process.wait(timeout=5)  # the failing baseline probe also reaps its own child


@pytest.mark.platforms('linux')
def test_restart_makes_family_observable(tmp_path):
    source = ('import os\nfrom pathlib import Path\n'
              'def produce(payload):\n'
              '    marker = payload.get("marker")\n'
              '    if marker and not Path(marker).exists():\n'
              '        Path(marker).touch()\n        os._exit(7)\n'
              '    return {"pid": os.getpid()}\n')
    relay = start(write_provider(tmp_path, source), tmp_path)
    try:
        before = observe(tmp_path / 'runtime')
        assert len(before) == 2
        pid = produce(relay, marker=str(tmp_path / 'crashed'))['pid']
        assert pid != before[1].identity.pid
        assert shared.read_owner(tmp_path / 'runtime')['child_pid'] == pid
        after = observe(tmp_path / 'runtime')
        assert len(after) == 2
        assert after[1].identity.pid == pid == relay.ping()['child_pid']
    finally:
        assert relay.retire(timeout=10)


@pytest.mark.platforms('linux')
def test_loaded_digest_rejects_stale_executable_bytes(tmp_path):
    helper = tmp_path / 'plugin_api.py'
    helper.write_text('VALUE = "old"\n')
    stamp = helper.stat()
    py_compile.compile(str(helper), doraise=True)
    helper.write_text('VALUE = "new"\n')
    os.utime(helper, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    # Real loader control: same size + timestamp selects stale executable code.
    spec = importlib.util.spec_from_file_location('stale_control', helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.VALUE == 'old'
    marker = tmp_path / 'executed'
    source = ('import importlib.util, sys\nfrom pathlib import Path\n'
              'def open_provider(config):\n'
              '    path = Path(__file__).with_name("plugin_api.py")\n'
              '    spec = importlib.util.spec_from_file_location("paired_api", path)\n'
              '    module = importlib.util.module_from_spec(spec)\n'
              '    sys.modules[spec.name] = module\n'
              '    spec.loader.exec_module(module)\n'
              f'    Path({str(marker)!r}).write_text(module.VALUE)\n'
              '    return lambda payload: {"value": module.VALUE}\n')
    relay = None
    try:
        with pytest.raises(shared.OwnerUnavailable):
            relay = start(write_provider(tmp_path, source, extra=(helper,)), tmp_path, timeout=1)
        assert not marker.exists(), 'unauthenticated stale code reached factory publication'
        assert shared.read_owner(tmp_path / 'runtime') is None
    finally:
        if relay is not None:
            assert relay.retire(timeout=10)


def test_recreated_home_cannot_reuse_lifecycle(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    state = SimpleNamespace(session_authority=Authority(home))
    original = os.stat
    fixed = original(home)

    def reused_identity(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if Path(path) == home:
            # Controlled inode-reuse interleaving over actual deletion/recreation.
            values = list(info)
            values[1], values[2] = fixed.st_ino, fixed.st_dev
            return os.stat_result(values)
        return info

    monkeypatch.setattr(homes.os, 'stat', reused_identity)
    original_fstat = os.fstat

    def reused_fstat(fd):
        info = original_fstat(fd)
        if Path(f'/proc/self/fd/{fd}').resolve() == home:
            values = list(info)
            values[1], values[2] = fixed.st_ino, fixed.st_dev
            return os.stat_result(values)
        return info

    monkeypatch.setattr(homes.os, 'fstat', reused_fstat)
    before = homes.bind(home, state)
    shutil.rmtree(home)
    home.mkdir()
    after = homes.bind(home, state)
    assert before.lifecycle_generation != after.lifecycle_generation
    assert not homes.is_current(before, state)
    assert homes.is_current(after, state)


def test_explicit_authority_mismatch_cannot_become_standalone(tmp_path, monkeypatch):
    import hermes_constants

    home, other = tmp_path / 'home', tmp_path / 'other'
    home.mkdir()
    other.mkdir()
    monkeypatch.setattr(hermes_constants, 'get_process_hermes_home', lambda: home)
    assert homes.bind(home).home == str(home)
    with pytest.raises(HomeRevoked):
        homes.bind(home, SimpleNamespace(session_authority=Authority(other)))
