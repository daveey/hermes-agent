"""Independent review: a failed refresh cannot acknowledge old publication."""
import hashlib

import pytest

from hermes_cli.serve_producer_runtime import ProjectionRelay, projection_response
from tests.waiting import wait_until


@pytest.mark.platforms('posix')
def test_failed_refresh_revokes_prior_prepared_output(tmp_path):
    provider = tmp_path / 'provider.py'
    provider.write_text(
        'from pathlib import Path\n'
        'def produce(payload):\n'
        '    if (Path(__file__).parent / "fail").exists():\n'
        '        raise RuntimeError("source capture unavailable")\n'
        '    return {"healthy": True}\n', encoding='utf-8')
    relay = ProjectionRelay(
        {'path': str(provider), 'sha256': hashlib.sha256(provider.read_bytes()).hexdigest()},
        home=tmp_path, runtime=tmp_path / 'runtime', generation='review')
    body = {'sessions': ['chat']}
    try:
        relay.start_for_fixture()
        key = relay.submit({'body': body, 'accepted': 1})
        assert wait_until(lambda: relay.acknowledged(key), timeout=10)
        assert relay.read(key) == b'{"healthy":true}'
        other = relay.submit({'body': {'sessions': ['other']}, 'accepted': 1})
        assert wait_until(lambda: relay.acknowledged(other), timeout=10)
        (tmp_path / 'fail').touch()
        assert relay.submit({'body': body, 'accepted': 2}) == key
        assert wait_until(lambda: relay.error == 'RuntimeError' and relay.pending_count == 0, timeout=10)
        # Real child returned UNAVAILABLE; no new publication has happened.
        actual = (relay.acknowledged(key), relay.read(key), projection_response(relay, body).status_code)
        assert actual == (False, None, 503)
        assert relay.acknowledged(other)
        assert relay.read(other) == b'{"healthy":true}'
        # The failure revokes only this key, not the relay or another publication.
        (tmp_path / 'fail').unlink()
        assert relay.submit({'body': body, 'accepted': 3}) == key
        assert wait_until(lambda: relay.acknowledged(key), timeout=10)
        assert relay.read(key) == b'{"healthy":true}'
        assert projection_response(relay, body).status_code == 200
    finally:
        assert relay.close(timeout=10)
