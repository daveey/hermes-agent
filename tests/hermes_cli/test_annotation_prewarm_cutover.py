"""Drain annotation boot work without taking ownership of project warming."""
import threading

import pytest

from hermes_cli import kanban_annotation_prewarm as warm
from hermes_cli import kanban_session_activity as activity


def test_suspend_drains_running_annotation_and_fences_new_work(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    reads = []

    def snapshot():
        reads.append(True)
        entered.set()
        assert release.wait(3)
        return object()

    monkeypatch.setattr(activity, '_annotation_snapshot', snapshot)
    worker = threading.Thread(target=warm.prewarm_annotation)
    worker.start()
    try:
        assert entered.wait(3)
        with pytest.raises(TimeoutError):
            warm.suspend_annotation_prewarm(timeout=0)
        assert warm.prewarm_annotation() is False
        assert reads == [True]
    finally:
        release.set()
        worker.join(3)
        warm.resume_annotation_prewarm()
    assert not worker.is_alive()
    warm.suspend_annotation_prewarm(timeout=0)
    try:
        assert warm.prewarm_annotation() is False
    finally:
        warm.resume_annotation_prewarm()
    assert warm.prewarm_annotation() is True
    assert reads == [True, True]


def test_suspended_boot_preserves_project_tree_owner(monkeypatch):
    from hermes_cli import projects_tree_prewarm

    reads = []
    monkeypatch.setattr(activity, '_annotation_snapshot', lambda: reads.append('annotation'))
    monkeypatch.setattr(projects_tree_prewarm, 'prewarm_project_tree', lambda: reads.append('tree'))
    warm.suspend_annotation_prewarm(timeout=0)
    try:
        warm._boot_prewarm()
        assert reads == ['tree']
    finally:
        warm.resume_annotation_prewarm()


def test_failed_warm_releases_drain_custody(monkeypatch):
    def fail():
        raise OSError('source unavailable')

    monkeypatch.setattr(activity, '_annotation_snapshot', fail)
    assert warm.prewarm_annotation() is False
    warm.suspend_annotation_prewarm(timeout=0)
    warm.resume_annotation_prewarm()
    warm.resume_annotation_prewarm()


def test_transfer_lock_covers_prewarm_drain_window(monkeypatch):
    from hermes_cli import kanban_annotation_host as host

    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(host, '_holder_suspended', False)
    monkeypatch.setattr(host, '_holder', [])

    def drain(**kwargs):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(warm, 'suspend_annotation_prewarm', drain)
    thread = threading.Thread(target=host.suspend_holder)
    thread.start()
    try:
        assert entered.wait(3)
        acquired = host._holder_transfer_lock.acquire(blocking=False)
        if acquired:
            host._holder_transfer_lock.release()
        assert not acquired
        assert host.start_holder('late') is None
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()


def test_holder_transfer_orders_warmup_drain_and_single_restart(monkeypatch):
    from hermes_cli import kanban_annotation_host as host

    calls = []
    monkeypatch.setattr(host, '_holder_suspended', False)
    monkeypatch.setattr(warm, 'suspend_annotation_prewarm', lambda **kw: calls.append('warm-stop'))
    monkeypatch.setattr(warm, 'resume_annotation_prewarm', lambda: calls.append('warm-resume'))
    monkeypatch.setattr(host, 'stop_holder', lambda **kw: calls.append('holder-stop'))
    monkeypatch.setattr(host, '_start_holder', lambda role: calls.append('holder-start'))
    host.suspend_holder()
    assert calls == ['warm-stop', 'holder-stop']
    host.start_holder('late')
    assert calls == ['warm-stop', 'holder-stop']
    host.resume_holder('serve')
    host.resume_holder('serve')
    assert calls == ['warm-stop', 'holder-stop', 'holder-stop', 'warm-resume', 'holder-start']
