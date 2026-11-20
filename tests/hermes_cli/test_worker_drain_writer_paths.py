"""Root baseline writes must never traverse a mutable or symlinked parent."""
import json
import os
from types import SimpleNamespace

import pytest

from hermes_cli import worker_drain_baseline as producer

pytestmark = pytest.mark.platforms('linux')


@pytest.fixture
def root_dirs(monkeypatch, tmp_path):
    real = os.fstat
    def root_stat(fd):
        st = real(fd)
        # The runner's scratch ancestors are intentionally mutable. Model the
        # root-owned deployment ancestors, retaining real modes in our fixture.
        mode = st.st_mode if os.readlink(f'/proc/self/fd/{fd}').startswith(str(tmp_path)) else st.st_mode & ~0o022
        return SimpleNamespace(st_uid=0, st_mode=mode)
    monkeypatch.setattr(producer.os, 'fstat', root_stat)
    monkeypatch.setattr(producer.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(producer.os, 'fchown', lambda *args: None)


def test_symlinked_ancestor_cannot_redirect_writer(tmp_path, root_dirs):
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises((producer.CaptureRefused, OSError)):
        producer.write_baseline(link / 'baseline.json', {})
    assert not (real / 'baseline.json').exists()


def test_mutable_parent_cannot_receive_root_baseline(tmp_path, root_dirs):
    mutable = tmp_path / 'mutable'
    mutable.mkdir(mode=0o777)
    mutable.chmod(0o777)
    with pytest.raises(producer.CaptureRefused, match='parent'):
        producer.write_baseline(mutable / 'baseline.json', {})
    assert not (mutable / 'baseline.json').exists()


def test_install_refuses_before_unlink_or_capture(tmp_path, root_dirs, monkeypatch):
    mutable = tmp_path / 'mutable'
    mutable.mkdir()
    path = mutable / 'baseline.json'
    path.write_text('preserve foreign bytes')
    mutable.chmod(0o777)
    monkeypatch.setattr(producer, 'capture', lambda *a, **k: pytest.fail('capture before path trust'))
    with pytest.raises(producer.CaptureRefused, match='parent'):
        producer.install(path=path)
    assert path.read_text() == 'preserve foreign bytes'


def test_atomic_replace_uses_pinned_directory(tmp_path, root_dirs):
    path = tmp_path / 'baseline.json'
    producer.write_baseline(path, {'version': 2})
    assert json.loads(path.read_text()) == {'version': 2}
    assert path.stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.glob('.baseline.json.*'))
