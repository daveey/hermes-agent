"""A failed spawn may be a racing owner, but neither success nor failure waits forever."""
from types import SimpleNamespace

import pytest

from hermes_cli import serve_producer_broker as shared
from hermes_cli import serve_producer_runtime as runtime


@pytest.mark.parametrize('scenario', ['successful', 'racing', 'expired'])
def test_spawn_poll_observes_receipt_and_deadline(tmp_path, monkeypatch, scenario):
    relay = runtime.ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'r', generation='g')
    info = {'digest': shared.compatibility({}, relay.config['home'])}
    reads, sleeps, attached = [], [], []
    clock = [0.0]

    def read_owner(path):
        reads.append(path)
        assert len(reads) <= 6, 'poll ignored owner or deadline'
        if scenario == 'racing' and len(reads) >= 3:
            return info
        return None

    def pause(seconds):
        assert scenario != 'successful', 'successful spawn without READY must not get a race allowance'
        sleeps.append(seconds)
        clock[0] += seconds

    def spawn(timeout, *, gate):
        if scenario != 'successful':
            raise OSError('another serve may own admission')

    monkeypatch.setattr(shared, 'read_owner', read_owner)
    monkeypatch.setattr(relay, '_spawn_owner', spawn)
    monkeypatch.setattr(relay, '_attach_owner', lambda record, timeout: attached.append(record))
    monkeypatch.setattr(runtime, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=pause))
    gate = SimpleNamespace(require_runtime=lambda path: None)
    if scenario == 'racing':
        relay._start_shared(gate, .1)
        assert attached == [info] and sleeps == [.05]
    else:
        with pytest.raises(shared.OwnerUnavailable):
            relay._start_shared(gate, .1)
        assert not attached
        assert len(sleeps) == (2 if scenario == 'expired' else 0)
