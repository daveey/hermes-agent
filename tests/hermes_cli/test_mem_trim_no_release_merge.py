"""Allocator-only calls must also skip GC when page release is unavailable."""
from unittest.mock import Mock

from hermes_cli import mem_trim


def test_no_release_allocator_only_skips_collection(monkeypatch):
    collect = Mock()
    monkeypatch.setattr(mem_trim, "_probe_glibc_malloc_trim", lambda: None)
    monkeypatch.setattr(mem_trim, "_config_settings", lambda: (True, 0.0, 1, 0.0))
    monkeypatch.setattr(mem_trim, "_last_trim_monotonic", 0.0)
    monkeypatch.setattr(mem_trim, "_last_alloc_trim_monotonic", 0.0)
    monkeypatch.setattr(mem_trim.gc, "collect", collect)
    assert mem_trim.trim_memory(collect=False) is False
    collect.assert_not_called()


def test_idle_collection_reclaims_cycles_without_page_release(monkeypatch):
    from agent.review_idle_queue import QUEUE

    collect = Mock(return_value=0)
    monkeypatch.setattr(QUEUE, "quiet_period", lambda: (600.0, 1.0))
    monkeypatch.setattr(mem_trim, "_idle_collected_for", None)
    monkeypatch.setattr(mem_trim, "_agent_closed_since_idle_collect", False)
    monkeypatch.setattr(mem_trim, "_probe_glibc_malloc_trim", lambda: None)
    monkeypatch.setattr(mem_trim, "_config_settings", lambda: (True, 0.0, 1, 0.0))
    monkeypatch.setattr(mem_trim, "_last_trim_monotonic", 0.0)
    monkeypatch.setattr(mem_trim, "_last_alloc_trim_monotonic", 0.0)
    monkeypatch.setattr(mem_trim.gc, "collect", collect)
    assert mem_trim.trim_memory_when_idle(reason="test") is False
    assert mem_trim.trim_memory_when_idle(reason="test") is False
    collect.assert_called_once_with()
