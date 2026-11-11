"""Worker-first enrollment audit (self-heal kanban-12ee0e4b65).

Only 13 of 172 hub cards in three days were enrolled worker-first, and nothing recorded why the rest stayed on the
hub. Two guards: a producer can fence quoted evidence (a proposal's "what happened on the hub") off the host-need
scan, and every card's ``created`` event carries the ``portable_reason`` the enrollment check returned.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_placement as placement

EVIDENCE = ("Since 11-02 the hub's 7-slot cap fills: `sqlite3 ~/.hermes/kanban.db` shows the cron job and the "
            "ssh relay on the hub, and hermes-sync logs the same.")


# The producer-facing contract is the literal marker text (plugins write it), so the test pins the strings.
OPEN, CLOSE = "<!-- placement:evidence -->", "<!-- /placement:evidence -->"


def fenced(text: str) -> str:
    return f"{OPEN}\n{text}\n{CLOSE}"


def test_marker_constants_match_the_contract():
    assert (getattr(placement, "EVIDENCE_OPEN", None), getattr(placement, "EVIDENCE_CLOSE", None)) == (OPEN, CLOSE)


def test_fenced_evidence_is_not_a_host_need():
    work = "Fix plugins/coach/self_heal.py in a hermes-mods worktree, add a regression test, push."
    args = {"title": "self-heal fix: cap", "requires": ["terminal", "file", "git"],
            "body": f"{work}\n\n{fenced(EVIDENCE)}\n"}
    assert placement.portable_reason(args, {}) is None
    result = placement.decide(args, cfg={"worker_pools": {"hub": ["worker-1"]}}, self_gw="hub")
    assert result["placement_policy"] == "worker-first-v1"


def test_unfenced_evidence_still_keeps_the_card_home():
    args = {"title": "self-heal fix: cap", "requires": ["terminal", "file", "git"], "body": EVIDENCE}
    assert placement.portable_reason(args, {}) == "home gateway marker"


@pytest.mark.parametrize("body", [
    # a host need in the work text outside the fence still counts
    "Then run `systemctl restart hermes` yourself.\n" + fenced("quoted"),
    # an unterminated fence exempts nothing (a producer bug must not open every card)
    f"{OPEN}\n{EVIDENCE}\n",
    # a closing marker alone exempts nothing
    f"{EVIDENCE}\n{CLOSE}",
])
def test_fence_never_hides_work_text(body):
    assert placement.portable_reason({"title": "fix", "body": body}, {}) is not None


def test_fenced_mac_words_are_evidence_too():
    args = {"title": "fix", "body": fenced("The Desktop chat on the MacBook showed it (launchctl list).")}
    assert placement.portable_reason(args, {}) is None


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("kanban:\n  placement:\n    worker_pools:\n      hub: [worker-1]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def created_payload(task_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        return next(e.payload for e in kb.list_events(conn, task_id) if e.kind == "created")


@pytest.mark.parametrize("extra, reason", [
    ("--requires terminal --requires host_repo", "uncarried requirement"),
    ("--requires terminal --body 'read kanban.db'", "home gateway marker"),
    ("--requires terminal", None),
])
def test_cli_created_event_records_portable_reason(kanban_home, extra, reason):
    import re

    from hermes_cli import kanban as kc

    out = kc.run_slash(f"create 'audit card' --assignee default {extra}")
    tid = re.search(r"(t_[0-9a-f]+)", out).group(1)
    payload = created_payload(tid)
    assert "portable_reason" in payload, payload
    assert payload["portable_reason"] == reason


@pytest.mark.parametrize("requires, reason", [(["terminal", "host_repo"], "uncarried requirement"),
                                              (["terminal"], None)])
def test_tool_created_event_records_portable_reason(kanban_home, monkeypatch, requires, reason):
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    result = json.loads(kt._handle_create({"title": "tool audit card", "assignee": "default",
                                           "requires": requires}))
    assert result.get("ok"), result
    payload = created_payload(result["task_id"])
    assert "portable_reason" in payload, payload
    assert payload["portable_reason"] == reason


def test_created_event_omits_audit_when_no_pool_is_configured(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="plain")
    assert "portable_reason" not in created_payload(tid)


def test_create_task_records_a_passed_reason(kanban_home):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="tool card", placement_audit={"portable_reason": "local marker"})
    assert created_payload(tid)["portable_reason"] == "local marker"
    assert json.dumps(created_payload(tid))  # stays JSON


@pytest.mark.parametrize("pinned", [False, True])
def test_db_policy_is_creation_time_not_later_state(kanban_home, monkeypatch, pinned):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Implement cloneable repository fix", assignee="default",
                             requires=["terminal", "file", "git"], placement_checked=pinned,
                             placement_audit={"portable_reason": None, "placement_policy": "untrusted"})
        expected = None if pinned else "worker-first-v1"
        assert kb.get_task(conn, tid).placement_policy == expected
        # Simulate subsequent assignment changes in this isolated fixture, never production.
        conn.execute("UPDATE tasks SET placement_policy=NULL WHERE id=?", (tid,))
    assert created_payload(tid)["placement_policy"] == expected


def test_runtime_pin_does_not_disable_cloneable_implementation():
    from hermes_cli.kanban_worker_placement import creation_audit

    cfg = {"worker_pools": {"hub": ["worker-1"]}}
    repo = {"title": "Implement owner-feed regression in a cloneable repository",
            "requires": ["terminal", "file", "git"], "assignee": "default"}
    runtime = {**repo, "title": "Investigate stale session-idle owner evidence",
               "runs_on": ["machine:hub"], "placement": "local"}
    assert creation_audit(runtime, cfg)["portable_reason"] is not None
    assert "placement_policy" not in placement.decide(runtime, cfg=cfg, self_gw="hub")
    assert creation_audit(repo, cfg)["portable_reason"] is None
    assert placement.decide(repo, cfg=cfg, self_gw="hub")["placement_policy"] == "worker-first-v1"
