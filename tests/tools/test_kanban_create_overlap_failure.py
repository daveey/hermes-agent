"""kanban_create refuses a second owner of one red-CI failure: same failing test or same Actions run. [daveey]

Incident (2026-11-02, t_01a19ea9f1739df6): main went red at ac613a6e on
``test_busy_board_open_does_not_block_session_poll[True-True]`` (run 41516070553) and four cards owned that one failure
within 23 minutes, all admitted because their titles share few words. On 11-03 a worker card and the ci-main-watch
card for run 41555531824 (``test_termux_assembly_permissions``) were created in the same second. The cases below are
the real titles, bodies and idempotency keys (``kanban_overlap_failure_cases.json``); the two ci-main-watch owners carry
the body ci_main_watch writes since it lists ``FAILED <node id>`` lines (``body_deployed`` is what it wrote before).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_overlap as ko
from tests.tools import test_kanban_create_overlap as helpers

home = helpers.home

CASES = json.loads((Path(__file__).with_name("kanban_overlap_failure_cases.json")).read_text(encoding="utf-8"))
OWNER_1102 = "t_01a19dfe46a7b2a3"  # ci-main-watch, 12:40 PST
LATER_1102 = ["t_01a19e02884dee02", "t_01a19e082c8721b1", "t_01a19e12c1eba138"]  # 12:45, 12:51, 13:03
CMW_1103, WORKER_1103 = "t_01a19f0a4d82213d", "t_01a19f0a4cc8b092"


def owner(cid: str, *, status: str = "ready", body_field: str = "body") -> str:
    """Create the real card ``cid`` on this gateway's default board (another chat's session); returns the new id."""
    c = CASES[cid]
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=c["title"], body=c[body_field], assignee="default",
                             session_id=f"chat-{cid}", idempotency_key=c["key"])
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        conn.commit()
    return tid


def remote(tmp_path, cids, status="ready"):
    """The real cards as other-gateway rows of the hermes-hub replica, under their real ids."""
    (tmp_path / "hub-state" / "hub.db").unlink(missing_ok=True)
    helpers._fake_replica(tmp_path / "hub-state" / "hub.db", [
        ("macbook", {"id": cid, "board": "default", "title": CASES[cid]["title"], "body": CASES[cid]["body"],
                     "status": status, "gateway": "macbook", "sessions": [{"session": f"chat-{cid}", "rel": "created"}]})
        for cid in cids])


def check(cid: str | None = None, *, title: str | None = None, body: str | None = None, **args):
    c = CASES[cid] if cid else {"title": title, "body": body or "", "key": None}
    args.setdefault("body", c["body"] if body is None else body)
    if c.get("key"):
        args.setdefault("idempotency_key", c["key"])
    with kbc.connect_closing() as conn:
        refusal, _ = ko.check(conn, args, courier=False, title=title or c["title"], session_id="new-chat",
                              self_tid=None, parents=[])
    return json.loads(refusal) if refusal else None


def failure_whys(result) -> list[str]:
    return [m["why"] for m in (result or {}).get("matches", []) if "same failing test" in m["why"]
            or "same CI run" in m["why"]]


# --- Acceptance 1: the 11-02 sequence, in creation order -------------------------------------------------------


@pytest.mark.parametrize("where", ["local", "replica"])
def test_1102_each_later_card_is_refused_naming_an_open_earlier_card(home, tmp_path, where):
    earlier = [OWNER_1102]
    for cid in LATER_1102:
        if where == "local":
            with kbc.connect_closing() as conn:  # the board as history had it: every earlier card open
                conn.execute("DELETE FROM tasks")
                conn.commit()
            made = {owner(e): e for e in earlier}
        else:
            remote(tmp_path, earlier)
            made = {e: e for e in earlier}
        result = check(cid)
        assert result and result["code"] == "create_overlap", cid
        named = [made.get(m["id"]) for m in result["matches"]]
        assert named and None not in named, (cid, result["matches"])  # only earlier open cards
        assert failure_whys(result), result
        earlier.append(cid)


def test_1102_ci_main_watch_card_names_the_failing_test_only_with_the_new_body():
    new = ko.failure_identity(CASES[OWNER_1102]["title"], CASES[OWNER_1102]["body"])
    old = ko.failure_identity(CASES[OWNER_1102]["title"], CASES[OWNER_1102]["body_deployed"])
    assert new.tests == {"test_busy_board_open_does_not_block_session_poll"}
    assert not old.tests and "41516070553" in old.runs
    later = ko.failure_identity(CASES[LATER_1102[0]]["title"], CASES[LATER_1102[0]]["body"])
    assert ko.same_failure(later, new) == "same failing test test_busy_board_open_does_not_block_session_poll"
    assert ko.same_failure(later, old) is None  # deployed body: a test vs a bare run is not one failure


# --- The 11-03 termux pair (created in the same second) ---------------------------------------------------------


@pytest.mark.parametrize("where", ["local", "replica"])
@pytest.mark.parametrize("first,second", [(CMW_1103, WORKER_1103), (WORKER_1103, CMW_1103)])
def test_1103_second_card_of_the_pair_is_refused_naming_the_first(home, tmp_path, where, first, second):
    if where == "local":
        tid = owner(first)
    else:
        remote(tmp_path, [first])
        tid = first
    result = check(second)
    assert result and [m["id"] for m in result["matches"]] == [tid]
    # The worker's title names the module (test_termux_assembly_permissions); the watcher's body has the node id.
    assert result["matches"][0]["why"].startswith("same failing test ")
    assert "test_termux_assembly_permissions" in result["matches"][0]["why"]


# --- Acceptance 2: real non-duplicate pairs stay admitted --------------------------------------------------------


@pytest.mark.parametrize("new,old", [
    # flaky test that passed on retry vs the red-main card of the same run (the run failed on other tests)
    ("t_62ce7dc1", "t_bdb6c497"),
    # TestIsContainer failure vs the red-main card that lists only runs: a test vs a whole run is not one failure
    ("t_e37a86df", "t_bdb6c497"),
    # red-main card vs a deflake card of a flake seen in the same run
    ("t_fa55aeb4", "t_47d51eb4"),
    # "merge PR #272 once CI green" vs a deflake card whose flake was seen in that PR's run
    ("t_01a18c20993d4e87", "t_01a18c14b16fa4ec"),
    # two ci-flaky-cards deflake cards for different tests (each body's FAILED excerpt names the other test):
    # same key family (deflake:), the creator's own fan-out
    ("t_01a184c1fae6295e", "t_01a184c1ebcc0557"),
])
@pytest.mark.parametrize("where", ["local", "replica"])
def test_real_non_duplicate_pairs_have_no_failure_match(home, tmp_path, new, old, where):
    if where == "local":
        owner(old)
    else:
        remote(tmp_path, [old])
    assert failure_whys(check(new)) == []


def test_a_test_named_only_as_a_verification_step_is_not_a_failure(home):
    """t_01a18e0f85fa5317 (real) names ``test_under_the_hood_screen_reader_labels_are_translated`` as the regression
    test that checks its work. An open card about that test failing must not refuse it."""
    body = CASES["t_01a18e0f85fa5317"]["body"]
    assert "test_under_the_hood_screen_reader_labels_are_translated" in body
    assert not ko.failure_identity(CASES["t_01a18e0f85fa5317"]["title"], body).tests
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="Ask Eve CI red: translation check fails", assignee="default", session_id="x",
                       body="tests/test_untranslated_english.py::test_under_the_hood_screen_reader_labels_are_translated "
                            "fails on main.")
    assert check("t_01a18e0f85fa5317") is None


# --- Rule details --------------------------------------------------------------------------------------------------


def test_identity_sources():
    fi = ko.failure_identity("Fix tests/a/test_mod.py::TestX::test_alpha_case[1] and test_beta_case_x", "")
    assert fi.tests == {"test_alpha_case", "test_beta_case_x"} and fi.modules == {"test_mod"}
    assert fi.bare == {"test_beta_case_x"}
    lead = "Main fails tests/a/test_mod.py::test_gamma_case. Verified by tests/a/test_mod.py::test_delta_case."
    assert ko.failure_identity("t", lead).tests == {"test_gamma_case"}
    later = "Context.\n\nThis tests/a/test_mod.py::test_gamma_case fails too."
    assert not ko.failure_identity("t", later).tests  # past the opening paragraph: context, not the failure
    assert ko.failure_identity("t", "x\n\nlog:\n  FAILED tests/a/test_mod.py::test_eps_case - E").tests == {"test_eps_case"}
    # A card that names its own test pastes a job tail: the tail's FAILED lines are other tests of that job.
    tail = "Flaky test: `tests/a/test_mod.py::test_own_case`\n\n```\nFAILED tests/a/test_mod.py::test_other_case - E\n```"
    assert ko.failure_identity("Deflake tests/a/test_mod.py::test_own_case", tail).tests == {"test_own_case"}
    assert ko.failure_identity("test_main is short", "").tests == frozenset()
    assert ko.failure_identity("t", "see https://github.com/o/r/actions/runs/123456789/job/9").runs == {"123456789"}


def test_same_run_with_different_named_tests_is_two_failures():
    run = "https://github.com/daveey/hermes/actions/runs/41516070553"
    a = ko.failure_identity("Fix tests/a/test_m.py::test_one_thing", f"{run}")
    b = ko.failure_identity("Fix tests/a/test_m.py::test_two_thing", f"{run}")
    whole = ko.failure_identity("Fix red main CI", f"{run}")
    assert ko.same_failure(a, b) is None
    assert ko.same_failure(a, whole) is None
    assert ko.same_failure(whole, ko.failure_identity("main red again", run)) == "same CI run actions/runs/41516070553"


def test_closed_owner_never_matches(home):
    owner(OWNER_1102, status="done")
    assert check(LATER_1102[0]) is None


def test_distinct_from_admits_and_records(home):
    tid = owner(OWNER_1102)
    assert check(LATER_1102[0], distinct_from=[tid], distinct_reason="a separate flake of the same test") is None


def test_a_card_citing_the_owner_by_id_is_admitted(home):
    tid = owner(OWNER_1102)
    body = CASES[LATER_1102[0]]["body"] + f"\n\nSplit from {tid}: only the warm same-board case."
    assert failure_whys(check(LATER_1102[0], body=body)) == []


def test_exact_key_retry_is_not_a_second_card(home):
    owner(LATER_1102[0])
    assert check(LATER_1102[0]) is None


# --- Acceptance 4: entry parity (tool, CLI, REST) -------------------------------------------------------------------


NEW = CASES[LATER_1102[0]]


def test_tool_refuses(home):
    from tools import kanban_tools as kt
    tid = owner(OWNER_1102)
    out = json.loads(kt._handle_create({"title": NEW["title"], "body": NEW["body"], "assignee": "default",
                                        "requires": [], "idempotency_key": NEW["key"]}))
    assert out.get("code") == "create_overlap" and [m["id"] for m in out["matches"]] == [tid]
    assert "same failing test" in out["matches"][0]["why"]


def test_cli_refuses(home, monkeypatch, capsys):
    import argparse
    from hermes_cli import kanban as kc
    tid = owner(OWNER_1102)
    monkeypatch.setattr(kc, "_check_dispatcher_presence", lambda **kw: (True, ""))
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    rc = kc._cmd_create(root.parse_args(["kanban", "create", NEW["title"], "--body", NEW["body"], "--assignee",
                                         "default", "--idempotency-key", NEW["key"]]))
    err = capsys.readouterr().err
    assert rc == 2 and "create_overlap" in err and tid in err and "same failing test" in err


def test_rest_refuses(home):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard.plugin_api import router
    tid = owner(OWNER_1102)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        r = client.post("/tasks", json={"title": NEW["title"], "body": NEW["body"], "assignee": "default",
                                        "idempotency_key": NEW["key"]})
    payload = r.json()
    detail = payload.get("detail", payload)
    detail = json.loads(detail) if isinstance(detail, str) else detail
    assert detail.get("code") == "create_overlap" and tid in [m["id"] for m in detail["matches"]]
