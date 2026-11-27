"""Required acceptance continuation uses the real completion producer and budgeted apply."""
import hashlib
import json
import pytest
from hermes_cli.kanban_containment_connections import bootstrap

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lanes as lanes
from hermes_cli import kanban_lanes_plan as plan
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person
CRITERION = "Verify actual Desktop sidebar grouping if accessible"


def _renderer(conn, *, declaration=True):
    parent = kb.create_task(conn, title="Preserve chat ownership", body=f"Acceptance:\n- {CRITERION}.",
                            assignee="default", created_by="user", workspace_path="/src/hermes-agent")
    assert kb.claim_task(conn, parent, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    run = kb.get_task(conn, parent).current_run_id
    child = kb.create_task(conn, title="Verify painted Desktop sidebar grouping (read-only)",
                           body=f"Read-only acceptance verification: {CRITERION}. No clicks or focus changes.",
                           assignee="default", created_by="default", creator_task_id=parent, parents=[parent],
                           workspace_path="/src/hermes-agent")
    bootstrap(conn)  # public local identity bootstrap; production synced boards already carry portable UIDs
    metadata = {"followup_card": child, "renderer": "unverified_backend_only"}
    if declaration:
        metadata["acceptance_continuations"] = {
            "version": 1,
            "source_body_sha256": hashlib.sha256(kb.get_task(conn, parent).body.encode()).hexdigest(),
            "items": [{"card": child, "criterion": CRITERION}],
        }
    return parent, child, run, metadata


def test_renderer_required_continuation_real_completion_apply_once(conn, configured):
    parent, child, run, metadata = _renderer(conn)
    assert kb.get_task(conn, child).block_kind == lanes.GATE_KIND
    assert kb.complete_task(conn, parent, summary="Backend delivered; renderer acceptance carried by child.",
                            metadata=metadata, created_cards=[child], expected_run_id=run)
    bootstrap(conn)
    action = {"action": "admit_continuation", "card": child, "reason": "required original acceptance"}
    result = plan.apply(conn, [action])
    assert result["applied"] == [action], result
    assert kb.get_task(conn, child).status == "ready"
    assert lanes.admitted_today(conn, "hermes") == 1
    assert not plan.apply(conn, [action])["applied"]
    assert lanes.admitted_today(conn, "hermes") == 1
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='completed'", (parent,)).fetchone()
    assert json.loads(event[0])["acceptance_continuations"]["version"] == 1


def test_actual_held_producer_then_public_cli_apply(conn, configured, capsys, tmp_path):
    from tests.hermes_cli.test_kanban_lanes_plan import cli
    parent, child, run, metadata = _renderer(conn)
    assert cli("lanes", "held", "--json") == 0
    before = json.loads(capsys.readouterr().out)[0]["continuation"]
    assert not before["eligible"] and "parent not genuinely complete" in before["reason"]
    assert kb.complete_task(conn, parent, summary="Backend delivered; renderer remains.", metadata=metadata,
                            expected_run_id=run)
    bootstrap(conn)
    assert cli("lanes", "held", "--json") == 0
    candidate = json.loads(capsys.readouterr().out)[0]["continuation"]
    assert candidate["eligible"] and candidate["source"]["parent"] == parent
    actions = [{"action": "admit_continuation", "card": child, "reason": "required acceptance"}]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(actions))
    assert cli("lanes", "apply", str(path), "--json") == 0
    assert json.loads(capsys.readouterr().out)["applied"] == actions


def _complete(conn, parent, run, metadata):
    assert kb.complete_task(conn, parent, summary="Backend delivered; original acceptance remains on child.",
                            metadata=metadata, expected_run_id=run)
    bootstrap(conn)


def _admit(conn, child):
    return plan.apply(conn, [{"action": "admit_continuation", "card": child, "reason": "required acceptance"}])


def test_legacy_renderer_metadata_never_becomes_authority(conn, configured):
    parent, child, run, metadata = _renderer(conn, declaration=False)
    _complete(conn, parent, run, metadata)
    result = _admit(conn, child)
    assert not result["applied"] and "typed completion attestation missing" in result["rejected"][0]["why"]
    assert lanes.admitted_today(conn, "hermes") == 0


@pytest.mark.parametrize("mutation,why", [
    ("digest", "exact parent body digest"), ("criterion", "literal source criterion"),
    ("unrelated", "created dependency"), ("no_run_owner", "current owned genuinely claimed run"),
])
def test_completion_declaration_refuses_forgery_atomically(conn, configured, mutation, why):
    parent, child, run, metadata = _renderer(conn)
    if mutation == "digest":
        metadata["acceptance_continuations"]["source_body_sha256"] = "0" * 64
    elif mutation == "criterion":
        metadata["acceptance_continuations"]["items"][0]["criterion"] = "Unrelated optional improvement"
    elif mutation == "unrelated":
        other = kb.create_task(conn, title="unrelated", assignee="default", parents=[parent])
        metadata["acceptance_continuations"]["items"][0]["card"] = other
    else:
        run = None
    with pytest.raises(ValueError, match=why):
        kb.complete_task(conn, parent, summary="done", metadata=metadata, expected_run_id=run)
    assert kb.get_task(conn, parent).status == "running"
    assert kb.get_task(conn, child).status == "triage"
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='completed'", (parent,)).fetchone()


@pytest.mark.parametrize("field,value,why", [
    ("body", "Unsolicited improvement", "child scope changed"),
    ("title", "Now do unrelated work", "child scope changed"),
    ("assignee", "other", "child scope changed"),
    ("human_request", '{"interaction":"physical"}', "human/physical ask"),
    ("block_kind", "needs_input", "not held solely"),
])
def test_changed_scope_and_independent_holds_not_cleared(conn, configured, field, value, why):
    parent, child, run, metadata = _renderer(conn)
    _complete(conn, parent, run, metadata)
    conn.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, child))
    conn.commit()
    result = _admit(conn, child)
    assert not result["applied"] and why in result["rejected"][0]["why"]
    assert kb.get_task(conn, child).status == "triage"
    assert lanes.admitted_today(conn, "hermes") == 0


def test_budget_exhaustion_defers_without_consuming_extra_admission(conn, configured):
    for _ in range(2):
        p, c, r, m = _renderer(conn)
        _complete(conn, p, r, m)
        assert _admit(conn, c)["applied"]
    p, child, r, m = _renderer(conn)
    _complete(conn, p, r, m)
    result = _admit(conn, child)
    assert not result["applied"] and "budget spent (2/2" in result["rejected"][0]["why"]
    assert kb.get_task(conn, child).status == "triage"
    assert lanes.admitted_today(conn, "hermes") == 2


def test_unsatisfied_parent_rechecked_after_completion(conn, configured):
    parent, child, run, metadata = _renderer(conn)
    assert "parent not genuinely complete" in _admit(conn, child)["rejected"][0]["why"]
    _complete(conn, parent, run, metadata)
    other = kb.create_task(conn, title="separate prerequisite", assignee="default")
    kb.link_tasks(conn, other, child)
    assert "unsatisfied" in _admit(conn, child)["rejected"][0]["why"]
    assert kb.complete_task(conn, other, summary="prerequisite complete")
    assert _admit(conn, child)["applied"]


def test_unlinked_child_and_parent_scope_edit_invalidate_proof(conn, configured):
    parent, child, run, metadata = _renderer(conn)
    _complete(conn, parent, run, metadata)
    kb.unlink_tasks(conn, parent, child)
    assert "creator dependency missing" in _admit(conn, child)["rejected"][0]["why"]
    kb.link_tasks(conn, parent, child)
    kb.edit_task(conn, parent, body="Scope changed after completion")
    assert "parent acceptance changed" in _admit(conn, child)["rejected"][0]["why"]


def test_restart_idempotence_preserves_exact_request_and_admission_history(conn, configured):
    from hermes_cli.kanban_db_connect import connect
    parent, child, run, metadata = _renderer(conn)
    _complete(conn, parent, run, metadata)
    before = [tuple(r) for r in conn.execute("SELECT id,kind,payload FROM task_events WHERE task_id=? ORDER BY id", (child,))]
    assert _admit(conn, child)["applied"]
    with connect() as reopened:
        assert not _admit(reopened, child)["applied"]
        assert lanes.admitted_today(reopened, "hermes") == 1
        after = [tuple(r) for r in reopened.execute("SELECT id,kind,payload FROM task_events WHERE task_id=? ORDER BY id", (child,))]
        assert after[:len(before)] == before
        assert sum(kind == lanes.ADMITTED for _, kind, _ in after) == 1


def test_parent_cannot_add_a_new_goal_before_attesting(conn, configured):
    parent, _child, run, metadata = _renderer(conn)
    body = kb.get_task(conn, parent).body + "\nOptional new improvement acceptance."
    kb.edit_task(conn, parent, body=body)
    metadata["acceptance_continuations"]["source_body_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    with pytest.raises(ValueError, match="original creation scope"):
        _complete(conn, parent, run, metadata)


def test_metadata_edit_cannot_rewrite_or_create_attestation(conn, configured):
    from hermes_cli.kanban_completion_receipt import keep_admitted_receipt
    parent, _child, run, metadata = _renderer(conn)
    _complete(conn, parent, run, metadata)
    assert keep_admitted_receipt(conn, run, {})["acceptance_continuations"] == metadata["acceptance_continuations"]
    changed = json.loads(json.dumps(metadata))
    changed["acceptance_continuations"]["items"][0]["card"] = parent
    with pytest.raises(ValueError, match="only kanban complete"):
        keep_admitted_receipt(conn, run, changed)
    with pytest.raises(ValueError, match="only kanban complete"):
        keep_admitted_receipt(conn, None, metadata)


@pytest.mark.parametrize("cause", ["safety_refusal", "scope_stopped", "budget"])
def test_native_independent_decision_evidence_is_never_released(conn, configured, cause):
    parent, child, run, metadata = _renderer(conn)
    _complete(conn, parent, run, metadata)
    with kb.write_txn(conn):
        kb._append_event(conn, child, "spend_budget_triaged", {"cause": cause, "reason": "fixture restriction"})
    before = [tuple(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (child,))]
    result = _admit(conn, child)
    assert not result["applied"] and "scope/safety/spend restriction" in result["rejected"][0]["why"]
    assert [tuple(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (child,))] == before


def test_native_containment_marker_preserved(conn, configured):
    parent, child, run, metadata = _renderer(conn)
    _complete(conn, parent, run, metadata)
    conn.execute("UPDATE tasks SET containment_episode='unobserved-stop' WHERE id=?", (child,))
    conn.commit()
    result = _admit(conn, child)
    assert not result["applied"] and "existing protection" in result["rejected"][0]["why"]
    assert conn.execute("SELECT containment_episode FROM tasks WHERE id=?", (child,)).fetchone()[0] == "unobserved-stop"


def test_unsolicited_sibling_does_not_inherit_attestation(conn, configured):
    parent, child, run, metadata = _renderer(conn)
    sibling = kb.create_task(conn, title="Optional refactoring improvement", assignee="default", created_by="default",
                             creator_task_id=parent, parents=[parent], workspace_path="/src/hermes-agent")
    bootstrap(conn)
    _complete(conn, parent, run, metadata)
    assert "required acceptance evidence missing" in _admit(conn, sibling)["rejected"][0]["why"]
    assert _admit(conn, child)["applied"]


def test_paused_lane_and_never_admit_policy_preserved(conn, configured):
    parent, child, run, metadata = _renderer(conn)
    _complete(conn, parent, run, metadata)
    mapping = json.loads(json.dumps(base.LANES))
    mapping["hermes"]["paused"] = True
    configured(mapping)
    assert "lane paused" in _admit(conn, child)["rejected"][0]["why"]
    mapping["hermes"]["paused"] = False
    mapping["hermes"]["never_admit"] = ["Verify painted"]
    configured(mapping)
    assert "never auto-admits" in _admit(conn, child)["rejected"][0]["why"]


def test_synced_execution_is_not_confused_with_creation_namespace(conn, configured):
    parent, child, run, metadata = _renderer(conn)
    # Actual synced pool execution preserves creation namespace rather than moving ownership.
    conn.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
    conn.execute("UPDATE tasks SET created_on='origin-gateway' WHERE id IN (?,?)", (parent, child))
    conn.commit()
    _complete(conn, parent, run, metadata)
    assert _admit(conn, child)["applied"]


@pytest.mark.parametrize("payload", ["null", "[]", '{"acceptance_continuations":{"version":1,"items":[null]}}'])
def test_malformed_completion_history_is_a_per_card_deferral(conn, configured, payload):
    from hermes_cli.kanban_acceptance_continuations import candidates
    parent, child, run, metadata = _renderer(conn)
    _complete(conn, parent, run, metadata)
    conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='completed'", (payload, parent))
    conn.commit()
    result = next(c for c in candidates(conn) if c["card"] == child)
    assert not result["eligible"]
    assert "attestation missing" in result["reason"]


def test_budget_is_reloaded_under_admission_lock_not_stale_plan_state(conn, configured):
    from hermes_cli.kanban_acceptance_continuations import admit
    for _ in range(2):
        p, c, r, m = _renderer(conn)
        _complete(conn, p, r, m)
        assert _admit(conn, c)["applied"]
    p, child, r, m = _renderer(conn)
    _complete(conn, p, r, m)
    stale = {"now": kb.time.time(), "actor": plan.ACTOR, "used": {"hermes": 0}}
    why = admit(conn, lanes.policy(), {"reason": "required acceptance"}, plan._card(conn, child), stale)
    assert why and "budget spent (2/2" in why


def test_malformed_creator_reference_defers_only_that_card(conn, configured):
    from hermes_cli.kanban_acceptance_continuations import candidates
    parent, child, run, metadata = _renderer(conn)
    _complete(conn, parent, run, metadata)
    conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
                 ('{"creator_task_id":{}}', child))
    conn.commit()
    result = next(c for c in candidates(conn) if c["card"] == child)
    assert not result["eligible"] and "creator dependency missing" in result["reason"]
