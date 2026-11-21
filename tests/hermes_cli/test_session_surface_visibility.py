"""Canonical GUI provenance must survive local routing without admitting local automation."""
import pytest

from hermes_cli.session_listing import AutomatedSourcePolicy, stamp_automated


@pytest.mark.parametrize("source,created,automated", [
    ("gui", "desktop", False), ("local", "desktop", False),
    ("local", "cli", False), ("local", "tui", False),
    ("local", "oneshot", True), ("local", "bot_room", True),
    ("local", "eval", True), ("local", "unknown", True),
    ("local", None, True), ("oneshot", "desktop", True),
    ("kanban", "desktop", True), ("eval", "desktop", True),
])
def test_surface_classification(source, created, automated):
    policy = AutomatedSourcePolicy(["kanban", "oneshot"], ["desktop", "cli", "tui"])
    row = {"source": source, "created_source": created}
    assert stamp_automated([row], automated_sources=policy)[0]["automated"] is automated


@pytest.mark.parametrize("deny", [["*"], ["local"], ["desktop"], ["gui"]])
def test_explicit_deny_keeps_precedence(deny):
    row = {"source": "local", "created_source": "desktop"}
    assert stamp_automated([row], automated_sources=AutomatedSourcePolicy(deny, ["desktop"]))[0]["automated"]


@pytest.mark.parametrize("source", ["gui", "desktop", "local"])
def test_gui_policy_spelling_is_the_same_surface(source):
    row = {"source": source, "created_source": "desktop"}
    assert not stamp_automated([row], automated_sources=AutomatedSourcePolicy([], ["gui"]))[0]["automated"]
