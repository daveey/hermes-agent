"""A failed attach closes only sockets acquired so far and preserves the refusal."""
import pytest

from hermes_cli import serve_producer_broker as shared
from hermes_cli.serve_producer_runtime import ProjectionRelay


@pytest.mark.parametrize('fail_on', [1, 2])
def test_attach_connect_failure_closes_partial_sockets(tmp_path, monkeypatch, fail_on):
    class Refusal(OSError):
        pass

    class Socket:
        closed = 0
        def close(self):
            self.closed += 1

    sock = Socket()
    attempts = []

    def connect(runtime, timeout):
        attempts.append((runtime, timeout))
        if len(attempts) == fail_on:
            raise Refusal('expected connect refusal')
        return sock

    monkeypatch.setattr(shared, 'connect_owner', connect)
    relay = ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'r', generation='g')
    with pytest.raises(Refusal, match='expected connect refusal'):
        relay._attach_owner({'capability': 'c', 'generation': 'g'}, 1)
    assert sock.closed == (1 if fail_on == 2 else 0)
    assert relay._control is None
    assert relay._owner is None
    assert relay._thread is None
