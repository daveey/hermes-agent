"""A procfs identity assembled across a restart or exec is not evidence of idle."""
import pytest

from hermes_cli import worker_drain_census as census
from tests.hermes_cli.test_worker_drain_baseline import process

pytestmark = pytest.mark.platforms('linux')


@pytest.mark.parametrize('change', ['start', 'cgroup', 'exe'])
def test_identity_change_during_read_is_unknown(tmp_path, monkeypatch, change):
    process(tmp_path)
    original = census._read
    reads = []

    def read(path):
        value = original(path)
        if str(path).endswith('/10/stat'):
            reads.append(path)
        # The second stat read starts the revalidation pass. Inject an exec or
        # migration after the first complete sample (not into Path's realpath).
        if str(path).endswith('/10/stat') and len(reads) == 2:
            if change == 'start':
                process(tmp_path, start=101)
                value = original(path)
            elif change == 'cgroup':
                (tmp_path / '10' / 'cgroup').write_text('0::/other\n')
            else:
                (tmp_path / '10' / 'exe').unlink()
                (tmp_path / '10' / 'exe').symlink_to('/usr/sbin/other')
        return value

    monkeypatch.setattr(census, '_read', read)
    with pytest.raises(ValueError, match='process changed'):
        census.process_identity(str(tmp_path), '10')


def test_stable_identity_remains_usable(tmp_path):
    process(tmp_path)
    entry = census.process_identity(str(tmp_path), '10')
    assert entry[0:2] == (10, 100)
    assert entry[3:] == ('/system.slice/idle.service', '/usr/sbin/idled')
