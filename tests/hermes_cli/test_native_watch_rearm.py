"""Ordinary writes hint without tearing down native coverage. [daveey]"""
import sys
import time

import pytest

from hermes_cli.annotation_file_events import FileEvents
from hermes_cli.fact_source_watch import NativeSourceWatch
from tests.waiting import wait_until

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith('linux'), reason='Linux native notification boundary')


def test_ordinary_writes_hint_without_reinstall(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    path.write_bytes(b'init')
    hints = []
    original = FileEvents.replace
    calls = []

    def counted(self, *args, **kwargs):
        calls.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FileEvents, 'replace', counted)
    watch = NativeSourceWatch((path,), lambda: hints.append(time.monotonic()))
    try:
        assert wait_until(lambda: calls, timeout=2), 'coverage must install before writes'
        baseline = len(calls)
        for i in range(12):
            path.write_bytes(b'x' * (i + 1))
        assert wait_until(lambda: hints, timeout=2), 'native watch must hint on ordinary writes'
        assert len(calls) == baseline, (
            'ordinary IN_MODIFY must not replace inotify coverage; '
            f'got {len(calls)} installs after {baseline} baseline')
    finally:
        watch.close()


def test_new_parent_directory_retains_future_write_hints(tmp_path):
    path = tmp_path / 'later' / 'state.db'
    hints = []
    watch = NativeSourceWatch((path,), lambda: hints.append(time.monotonic()))
    try:
        path.parent.mkdir()
        assert wait_until(lambda: hints, timeout=2)
        watch._owner.submit(lambda: None).wait()
        baseline = len(hints)
        path.write_bytes(b'new source')
        assert wait_until(lambda: len(hints) > baseline, timeout=2), (
            'ancestor creation must install coverage for future source writes')
    finally:
        watch.close()
