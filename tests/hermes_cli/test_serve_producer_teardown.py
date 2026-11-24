"""Broker cleanup must reap failed producers without a second cleanup failure."""
import hashlib

import pytest

from tests.waiting import wait_until


@pytest.mark.platforms('posix')
def test_exhausted_restart_closes_cleanly(tmp_path, capfd):
    from hermes_cli.serve_producer_runtime import ProjectionRelay

    provider = tmp_path / 'crash.py'
    provider.write_text('import os\ndef produce(payload):\n    os._exit(7)\n', encoding='utf-8')
    relay = ProjectionRelay(
        {'path': str(provider), 'sha256': hashlib.sha256(provider.read_bytes()).hexdigest()},
        home=tmp_path, runtime=tmp_path / 'runtime', generation='g')
    try:
        relay.start_for_fixture()
        relay.submit({'value': 'crash'})
        wait_until(lambda: relay.error is not None, timeout=20)
    finally:
        assert relay.close(timeout=10)
    assert 'Traceback' not in capfd.readouterr().err
    assert not relay.is_alive()
    from hermes_cli.serve_producer_custody import FamilyCustody
    successor = FamilyCustody.acquire(tmp_path / 'runtime', 'panel')
    successor.close()


@pytest.mark.parametrize('error', [None, 'EBADF', 'ENOTCONN', 'EIO'])
def test_shutdown_only_tolerates_completed_channel_teardown(error):
    import errno
    import socket
    from types import SimpleNamespace
    from hermes_cli.serve_producer_process import shutdown_channel

    calls = []

    def shutdown(how):
        calls.append(how)
        if error is not None:
            raise OSError(getattr(errno, error), error)

    channel = SimpleNamespace(sock=SimpleNamespace(shutdown=shutdown))
    if error == 'EIO':
        with pytest.raises(OSError, match='EIO'):
            shutdown_channel(channel)
    else:
        shutdown_channel(channel)
    assert calls == [socket.SHUT_RDWR]
