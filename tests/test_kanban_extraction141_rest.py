"""E3 PATCH/bulk controls, plus retained create compatibility after mandatory-flag refusal."""

from __future__ import annotations
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

FLAGS = ("pin_mandatory", "model_pin_mandatory", "require_model")
REFUSED_VALUES = (True, 1, 2, "true", "false", "no", "0", "", [], {}, 0.0, 1.0)
ACCEPTED_VALUES = (None, False, 0)
EXACT_CLASSES = ("class:fast", "class:standard", "class:deep", "class:carbon")

OTHER_BOARD = "unconventional-collab"
B1_TITLE = ("Stage B1: run the frozen X/T tuning + confirmation matrix and external-anchor rescoring "
            "on rented GPUs")
DUP_TITLE = "Stage B: GPU calibration, bounded comparison campaign and verified report"
DUP_BODY = ("David explicitly approved: 'approve stage b'. Execute frozen Stage B for "
            "Metta-AI/unconventional-collab. Checked this turn: current PLAN.md read via gh.")
QUOTED_BODY = 'Historical run instruction (superseded and not active): "require_model: true"'


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    route = {"model": "claude-opus-5-5", "provider": "anthropic", "weight": 1,
             "billing": "subscription", "eligible": True, "funding_verified": True,
             "capabilities": {"tools": True, "streaming": True}}
    (h / "config.yaml").write_text(json.dumps({"model_routing": {
        "enabled": True, "routes": {level: [route] for level in ("fast", "standard", "deep")}}}))
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_capability_registry as reg
    monkeypatch.setattr(reg, "self_gateway", lambda: "hub")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return h


@pytest.fixture
def client(home):
    from plugins.kanban.dashboard.plugin_api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


# --- isolated-board helpers ----------------------------------------------------------------------

def _other_board_card() -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_boards as kbb
    from hermes_cli import kanban_db_connect as kbc
    kbb.create_board(OTHER_BOARD)
    with kbc.connect_closing(board=OTHER_BOARD) as conn:
        return kb.create_task(conn, title=B1_TITLE, body="Stage B / B1 of 3 on rented GPUs.",
                              assignee="default", session_id="20261026_154948_ea6cfd")


def _local_task(**kw) -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=kw.pop("title", "plain card"), assignee=kw.pop("assignee", "worker"), **kw)


_TABLES = ("tasks", "task_events", "task_comments", "task_links", "task_attachments", "task_runs")


def _snapshot() -> dict:
    """Every relevant row on both isolated boards plus the overlap log."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_overlap
    boards = [None] + ([OTHER_BOARD] if kb.board_exists(OTHER_BOARD) else [])
    snap: dict = {}
    for board in boards:
        with kbc.connect_closing(board=board) as conn:
            conn.row_factory = sqlite3.Row
            for table in _TABLES:
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                snap[(board, table)] = [tuple(r) for r in rows]
    path = kanban_overlap.log_path()
    snap["overlap_log"] = path.read_text() if path.exists() else ""
    return snap


def _task_row(tid: str) -> dict:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        t = kb.get_task(conn, tid)
        return {"status": t.status, "assignee": t.assignee, "model_override": t.model_override,
                "provider_override": t.provider_override, "model_pin_mandatory": t.model_pin_mandatory,
                "title": t.title, "body": t.body, "priority": t.priority}


def _overlap_events(task_id: str) -> list:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing(board=OTHER_BOARD) as conn:
        return [e.payload for e in kb.list_events(conn, task_id) if e.kind == "create_overlap"]


def _record_create_kwargs(monkeypatch) -> list:
    from hermes_cli import kanban_db as kb
    real, seen = kb.create_task, []

    def spy(conn, **kwargs):
        seen.append(dict(kwargs))
        return real(conn, **kwargs)
    monkeypatch.setattr(kb, "create_task", spy)
    return seen


def _record_board_conn(monkeypatch) -> list:
    """Recording spy delegating to the real connection: a refusal that wrongly opens the board is
    reported by an ordinary assertion in the test body, never a BaseException inside the ASGI portal."""
    from plugins.kanban.dashboard import plugin_api as api
    real, opened = api._board_conn, []

    def spy(*args, **kwargs):
        opened.append((args, kwargs))
        return real(*args, **kwargs)
    monkeypatch.setattr(api, "_board_conn", spy)
    return opened


# --- REST create compatibility controls ------------------------------------------------------------
# Mandatory-flag refusal is covered in test_kanban_create_flags265; preserve overlap,
# idempotency and the existing prefix-class behavior independently of that refusal.

def test_create_baseline_overlap_still_refused_and_recorded(client):  # baseline control
    b1 = _other_board_card()
    r = client.post("/tasks", json={"title": DUP_TITLE, "body": DUP_BODY, "assignee": "default"})
    assert r.status_code == 409, r.text
    assert len(_overlap_events(b1)) == 1


def test_create_baseline_idempotency_unchanged(client):  # baseline control
    body = {"title": "idem card", "assignee": "worker", "idempotency_key": "entry142-idem"}
    first, second = client.post("/tasks", json=body), client.post("/tasks", json=body)
    assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
    assert first.json()["task"]["id"] == second.json()["task"]["id"]


def test_create_baseline_prefix_class_unchanged_with_inert_flags(client, monkeypatch):
    seen = _record_create_kwargs(monkeypatch)
    r = client.post("/tasks", json={"title": "baseline card", "assignee": "worker",
                                    "model_override": "class:bogus", **{flag: False for flag in FLAGS}})
    assert r.status_code == 200, r.text
    [kwargs] = seen
    assert not set(FLAGS) & set(kwargs), kwargs
    row = _task_row(r.json()["task"]["id"])
    assert (row["model_override"], row["model_pin_mandatory"]) == ("class:bogus", None)















# --- C4 PATCH --------------------------------------------------------------------------------------

STATUS_BRANCHES = (None, "todo", "ready", "triage", "blocked", "scheduled", "review", "done", "archived", "running")


@pytest.mark.parametrize("status", STATUS_BRANCHES)
@pytest.mark.parametrize("flag", FLAGS)
def test_patch_top_level_flag_refused_without_mutation(client, status, flag):
    tid = _local_task()
    before = _snapshot()
    body = {flag: True, "priority": 7, "assignee": "other"}
    if status:
        body["status"] = status
    r = client.patch(f"/tasks/{tid}", json=body)
    assert r.status_code == 400, r.text
    assert "model-neutral" in r.text
    assert _snapshot() == before


@pytest.mark.parametrize("status", STATUS_BRANCHES)
@pytest.mark.parametrize("key,value", [("pin_mandatory", True), ("require_model", "yes"),
                                       ("pin_mandatory", "false"), ("require_model", 1)])
def test_patch_metadata_flag_refused_for_every_status(client, status, key, value):
    tid = _local_task()
    before = _snapshot()
    body = {"metadata": {key: value}, "summary": "s", "priority": 3}
    if status:
        body["status"] = status
    r = client.patch(f"/tasks/{tid}", json=body)
    assert r.status_code == 400, r.text
    assert "model-neutral" in r.text
    assert _snapshot() == before


def test_patch_metadata_falsy_flags_accepted_on_done(client):
    tid = _local_task()
    r = client.patch(f"/tasks/{tid}", json={"status": "done", "summary": "finished",
                                            "metadata": {"pin_mandatory": False, "require_model": 0}})
    assert r.status_code == 200, r.text
    assert _task_row(tid)["status"] == "done"


@pytest.mark.parametrize("value", ACCEPTED_VALUES, ids=repr)
def test_patch_falsy_top_level_flags_accepted(client, value):
    tid = _local_task()
    r = client.patch(f"/tasks/{tid}", json={"priority": 5, **{flag: value for flag in FLAGS}})
    assert r.status_code == 200, r.text
    assert _task_row(tid)["priority"] == 5


def test_patch_review_model_metadata_validated_only_on_review(client):  # baseline control
    tid = _local_task()
    before = _snapshot()
    r = client.patch(f"/tasks/{tid}", json={"status": "review", "summary": "s",
                                            "metadata": {"review_model": "claude-opus-5-5"}})
    assert r.status_code == 400, r.text
    assert _snapshot() == before
    # Non-review metadata is completion evidence, not a newly active review route.
    r = client.patch(f"/tasks/{tid}", json={"status": "done", "summary": "s",
                                            "metadata": {"review_model": "claude-opus-5-5",
                                                         "review_provider": "anthropic"}})
    assert r.status_code == 200, r.text
    assert _task_row(tid)["status"] == "done"


@pytest.mark.parametrize("model", EXACT_CLASSES)
def test_patch_review_exact_class_metadata_accepted(client, model):  # positive control
    tid = _local_task()
    r = client.patch(f"/tasks/{tid}", json={"status": "review", "summary": "s",
                                            "metadata": {"review_model": model, "review_provider": "auto"}})
    assert r.status_code == 200, r.text
    assert _task_row(tid)["status"] == "review"


@pytest.mark.parametrize("bulk", [False, True], ids=["patch", "bulk"])
def test_non_review_unknown_class_review_model_stays_inert(client, bulk):
    """E3: review_model is only policy on status=review; elsewhere it is inert completion evidence."""
    tid = _local_task()
    body = {"status": "done", "summary": "s", "metadata": {"review_model": "class:bogus"}}
    r = (client.post("/tasks/bulk", json={"ids": [tid], **body}) if bulk
         else client.patch(f"/tasks/{tid}", json=body))
    assert r.status_code == 200, r.text
    assert _task_row(tid)["status"] == "done"


def test_patch_legacy_whole_body_refusal_and_quoted_accept(client):  # baseline control
    tid = _local_task()
    before = _snapshot()
    r = client.patch(f"/tasks/{tid}", json={"body": "do it\nrequire_model: true"})
    assert r.status_code == 400, r.text
    assert _snapshot() == before
    r = client.patch(f"/tasks/{tid}", json={"body": QUOTED_BODY})
    assert r.status_code == 200, r.text
    assert _task_row(tid)["body"] == QUOTED_BODY


def test_metadata_only_classification_ignores_falsy_flags(client):
    """Retained metadata_only rule: an inert false flag is not an 'other change'."""
    tid = _local_task()
    plain = client.patch(f"/tasks/{tid}", json={"metadata_only": True, "status": "done"})
    flagged = client.patch(f"/tasks/{tid}", json={"metadata_only": True, "pin_mandatory": False})
    assert plain.status_code == 400 and "cannot accompany" in plain.text
    assert flagged.status_code == 400 and "requires human_request" in flagged.text, flagged.text


# --- C4 bulk ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("patch", [
    {"pin_mandatory": True}, {"model_pin_mandatory": "no"}, {"require_model": 1},
    {"metadata": {"pin_mandatory": True}, "status": "done"},
    {"metadata": {"require_model": "true"}, "status": "todo"},
    {"model_override": "claude-opus-5-5"},
], ids=["pin", "model_pin", "require", "meta-pin-done", "meta-require-todo", "concrete"])
def test_bulk_invalid_shared_patch_touches_no_id(client, patch):
    ids = [_local_task(title="a"), _local_task(title="b")]
    before = _snapshot()
    r = client.post("/tasks/bulk", json={"ids": ids + ["t_missing"], "priority": 9, "status": "todo", **patch})
    assert r.status_code == 400, r.text
    assert "results" not in r.text
    assert _snapshot() == before


def test_bulk_valid_partial_outcomes_preserved(client):  # baseline control (+ falsy flags accepted)
    a = _local_task(title="a")
    r = client.post("/tasks/bulk", json={"ids": [a, "t_missing"], "priority": 4,
                                         "pin_mandatory": False, "require_model": None,
                                         "model_pin_mandatory": 0,
                                         "model_override": "class:fast", "provider_override": "auto"})
    assert r.status_code == 200, r.text
    results = {e["id"]: e for e in r.json()["results"]}
    assert results[a]["ok"] is True
    assert results["t_missing"] == {"id": "t_missing", "ok": False, "error": "not found"}
    row = _task_row(a)
    assert (row["priority"], row["model_override"], row["provider_override"]) == (4, "class:fast", "auto")


# --- C7 model options ------------------------------------------------------------------------------

def test_model_options_is_auto_group_of_exact_classes(client, monkeypatch):
    from hermes_cli import inventory

    def boom(*a, **k):  # concrete catalog must not be consulted or offered
        raise AssertionError("concrete catalog consulted")
    monkeypatch.setattr(inventory, "build_models_payload", boom, raising=False)
    r = client.get("/model-options")
    assert r.status_code == 200, r.text
    data = r.json()
    assert list(data) == ["providers"]
    [group] = data["providers"]
    assert set(group) == {"slug", "label", "models"}
    assert group["slug"] == "auto" and group["label"]
    assert group["models"] == list(EXACT_CLASSES)


def test_every_model_option_pair_patches_and_empty_clears(client):
    tid = _local_task()
    [group] = client.get("/model-options").json()["providers"]
    for model in group["models"]:
        # Exactly what the bundled control sends for a picked pair (dist/index.js onPick).
        r = client.patch(f"/tasks/{tid}", json={"provider_override": group["slug"], "model_override": model})
        assert r.status_code == 200, (model, r.text)
        row = _task_row(tid)
        assert (row["model_override"], row["provider_override"]) == (model, "auto")
    r = client.patch(f"/tasks/{tid}", json={"clear_model_override": True})  # the control's empty option
    assert r.status_code == 200, r.text
    row = _task_row(tid)
    assert (row["model_override"], row["provider_override"]) == (None, None)
    for model in group["models"]:
        r = client.post("/tasks/bulk", json={"ids": [tid], "provider_override": "auto", "model_override": model})
        assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text


@pytest.mark.parametrize('bulk', [False, True], ids=['patch', 'bulk'])
@pytest.mark.parametrize('flag', FLAGS)
@pytest.mark.parametrize('value', REFUSED_VALUES, ids=repr)
def test_strict_flags_refuse_before_connection(client, monkeypatch, bulk, flag, value):
    from plugins.kanban.dashboard import plugin_api as api
    tid = _local_task()
    before = _snapshot()
    opened = _record_board_conn(monkeypatch)
    body = {flag: value, 'priority': 7, 'assignee': 'other'}
    response = (client.post('/tasks/bulk', json={'ids': [tid], **body}) if bulk
                else client.patch(f'/tasks/{tid}', json=body))
    assert response.status_code == 400, response.text
    assert 'model-neutral' in response.text
    assert opened == [], 'refused request opened board'
    assert _snapshot() == before


@pytest.mark.parametrize('bulk', [False, True], ids=['patch', 'bulk'])
@pytest.mark.parametrize('metadata', [False, True], ids=['top-level', 'review'])
def test_unknown_class_refuses_before_connection(client, monkeypatch, bulk, metadata):
    from plugins.kanban.dashboard import plugin_api as api
    tid = _local_task()
    before = _snapshot()
    opened = _record_board_conn(monkeypatch)
    body = ({'status': 'review', 'metadata': {'review_model': 'class:bogus'}} if metadata
            else {'model_override': 'class:bogus'})
    body.update(priority=7, assignee='other', summary='s')
    response = (client.post('/tasks/bulk', json={'ids': [tid], **body}) if bulk
                else client.patch(f'/tasks/{tid}', json=body))
    assert response.status_code == 400, response.text
    assert 'model-neutral' in response.text
    assert opened == [], 'refused request opened board'
    assert _snapshot() == before


@pytest.mark.parametrize('flag', ['pin_mandatory', 'require_model'])
@pytest.mark.parametrize('value', (*ACCEPTED_VALUES, *REFUSED_VALUES), ids=repr)
@pytest.mark.parametrize('bulk', [False, True], ids=['patch', 'bulk'])
def test_metadata_flags_have_strict_rest_contract(client, monkeypatch, flag, value, bulk):
    from plugins.kanban.dashboard import plugin_api as api
    tid = _local_task()
    before = _snapshot()
    accepted = value is None or value is False or type(value) is int and value == 0
    opened = _record_board_conn(monkeypatch)
    body = {'metadata': {flag: value}, 'priority': 7}
    response = (client.post('/tasks/bulk', json={'ids': [tid], **body}) if bulk
                else client.patch(f'/tasks/{tid}', json=body))
    assert response.status_code == (200 if accepted else 400), response.text
    if not accepted:
        assert opened == [], 'refused request opened board'
        assert _snapshot() == before


@pytest.mark.parametrize('bulk', [False, True], ids=['patch', 'bulk'])
def test_absent_and_falsy_flags_not_stored(client, bulk):
    tid = _local_task()
    for extra in ({}, *({key: value for key in FLAGS} for value in ACCEPTED_VALUES)):
        body = {'priority': 6, **extra}
        response = (client.post('/tasks/bulk', json={'ids': [tid], **body}) if bulk
                    else client.patch(f'/tasks/{tid}', json=body))
        assert response.status_code == 200, response.text
        assert _task_row(tid)['model_pin_mandatory'] is None
