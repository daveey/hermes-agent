"""Annotation ownership transfer must finish before shared facts are active."""
import threading

import pytest

from hermes_cli import kanban_annotation_host as host


def test_stop_joins_holder_before_allowing_restart(monkeypatch):
    stop = threading.Event()
    exited = threading.Event()
    thread = threading.Thread(target=lambda: (stop.wait(), exited.set()))
    thread.start()
    monkeypatch.setattr(host, '_holder', [(thread, stop, object())])
    wakes = []
    monkeypatch.setattr(host, 'wake_holder', lambda: wakes.append(True))
    try:
        host.stop_holder(timeout=2)
    finally:
        stop.set()
        thread.join(2)
    assert exited.is_set()
    assert not thread.is_alive()
    assert host._holder == []
    assert wakes == [True]
    host.stop_holder(timeout=2)
    assert wakes == [True]


def test_stop_timeout_retains_holder_custody(monkeypatch):
    release = threading.Event()
    stop = threading.Event()
    thread = threading.Thread(target=release.wait)
    thread.start()
    entry = (thread, stop, object())
    monkeypatch.setattr(host, '_holder', [entry])
    monkeypatch.setattr(host, 'wake_holder', lambda: None)
    try:
        with pytest.raises(TimeoutError):
            host.stop_holder(timeout=0)
        assert stop.is_set()
        assert host._holder == [entry]
    finally:
        release.set()
        thread.join(2)
    host.stop_holder(timeout=2)
    assert host._holder == []
