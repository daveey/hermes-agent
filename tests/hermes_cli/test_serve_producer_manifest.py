"""B: exact provider manifest is verified before any provider import; missing is never fresh-empty."""
import hashlib

import pytest

from hermes_cli.serve_producer_runtime import ProjectionRelay

pytestmark = pytest.mark.platforms('posix')


def _provider(tmp_path, marker):
    path = tmp_path / 'provider.py'
    path.write_text(f'open({str(marker)!r}, "w").write("imported")\n'
                    'def produce(payload):\n    return payload\n')
    member = tmp_path / 'member.py'
    member.write_text('VALUE = 1\n')
    return path, member


def _spec(path, files):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'files': files}


def _start(tmp_path, spec):
    relay = ProjectionRelay(spec, home=tmp_path, runtime=tmp_path / 'runtime', generation='g')
    try:
        relay.start_for_fixture(ready_timeout=30)
    finally:
        pass
    return relay


@pytest.mark.parametrize('mutation', ['digest', 'missing', 'absolute', 'traversal', 'empty'])
def test_bad_manifest_refuses_before_provider_import(tmp_path, mutation):
    marker = tmp_path / 'imported'
    path, member = _provider(tmp_path, marker)
    digest = hashlib.sha256(member.read_bytes()).hexdigest()
    files = {
        'digest': {'member.py': '0' * 64},
        'missing': {'gone.py': digest},
        'absolute': {str(member): digest},
        'traversal': {'../member.py': digest},
        'empty': {},
    }[mutation]
    relay = ProjectionRelay(_spec(path, files), home=tmp_path, runtime=tmp_path / 'runtime', generation='g')
    try:
        with pytest.raises((EOFError, OSError, ValueError)):
            relay.start_for_fixture(ready_timeout=30)
    finally:
        relay.close(timeout=10)
    assert not marker.exists(), 'provider code ran before its manifest was verified'


def test_exact_manifest_starts_provider(tmp_path):
    marker = tmp_path / 'imported'
    path, member = _provider(tmp_path, marker)
    spec = _spec(path, {'member.py': hashlib.sha256(member.read_bytes()).hexdigest()})
    relay = _start(tmp_path, spec)
    try:
        key = relay.submit({'value': 1})
        from tests.waiting import wait_until
        wait_until(lambda: relay.read(key) is not None, timeout=30)
        assert marker.read_text() == 'imported'
    finally:
        assert relay.close(timeout=10)
