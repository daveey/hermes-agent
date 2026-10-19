"""[daveey] tools.tool_search.defer_outside_kanban_worker: worker-lifecycle tools defer in chats only.

Kanban worker-lifecycle tools (heartbeat, complete, block, request_review...) cost ~3k tokens of
schema on every call of every desktop chat, where they are almost never used; workers call them
constantly. The list is deferred when HERMES_KANBAN_TASK is unset and kept direct in workers.
"""
import tools.tool_search as ts
from hermes_cli.config_defaults import DEFAULT_CONFIG

_LIFE = ["kanban_heartbeat", "kanban_complete"]


def _deferred(cfg, names):
    return {n for n in names if ts.is_deferrable_tool_name(n, cfg.effective_defer_tools, cfg.eager_tools)}


def _cfg(monkeypatch, **raw):
    cfg = ts.ToolSearchConfig.from_raw(raw)
    monkeypatch.setattr(ts, "load_config_readonly", lambda: cfg)
    return cfg


def test_default_is_upstream(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert DEFAULT_CONFIG["tools"]["tool_search"]["defer_outside_kanban_worker"] == []
    cfg = ts.ToolSearchConfig.from_raw({"defer": ["terminal"]})
    assert cfg.effective_defer_tools == frozenset({"terminal"})


def test_chat_defers_lifecycle_tools(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    cfg = _cfg(monkeypatch, defer=["terminal"], defer_outside_kanban_worker=_LIFE)
    assert cfg.effective_defer_tools == frozenset({"terminal", *_LIFE})
    assert _deferred(cfg, (*_LIFE, "kanban_show")) == set(_LIFE)


def test_worker_keeps_lifecycle_tools_direct(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_123")
    cfg = _cfg(monkeypatch, defer=["terminal"], defer_outside_kanban_worker=_LIFE)
    assert cfg.effective_defer_tools == frozenset({"terminal"})
    assert _deferred(cfg, _LIFE) == set()


def test_eager_still_wins(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    cfg = _cfg(monkeypatch, defer=[], eager=["kanban_complete"], defer_outside_kanban_worker=_LIFE)
    assert _deferred(cfg, _LIFE) == {"kanban_heartbeat"}


def test_scalar_value_is_ignored(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    cfg = ts.ToolSearchConfig.from_raw({"defer": [], "defer_outside_kanban_worker": "kanban_complete"})
    assert cfg.effective_defer_tools == frozenset()
