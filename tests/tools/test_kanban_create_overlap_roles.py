"""create_overlap: board and vocabulary are not work role; a recovery card is not its source. [daveey]

Audits t_01a189b2d186a986 (hub) and t_01a19075b3f1531a (MacBook + mini) after PR #387 found three refusals of
separate work, each replayed here from the ORIGINAL create input (``kanban_overlap_role_cases.json``, recovered
from the creating sessions' state.db / the self-heal pending queue; ``_source`` in that file):

- hub 10-31 17:22:21Z, tool: "Repair Stage B evidence gaps without changing frozen results" (implements future
  unseal gating, timing instrumentation, label preservation) refused against the Stage C verifier and the
  report-sending follow-through. Only "the new card names board 'unconventional-collab'" (``Metta-AI/unconventional-
  collab`` in the body) plus the one shared word "stage" matched; the verifier was already in ``distinct_from``.
- hub 11-01 00:31:48-01:21:18Z, CLI (coach self-heal lane), 8x: "self-heal: kanban card t_01a18aa302529d91 on
  mini/bb failed 2x (crashed,crashed): BB-1156, ..." refused against t_01a18aa302529d91 itself.
- MacBook 11-01 02:06:08Z, tool: "bb-tuner burn-down: 7-day re-measure after the 10/31 failed-fix escalation
  deploy" refused against four mini bb cluster fix/design cards, each only "the new card names board 'bb'".

The true duplicates those audits confirmed stay refused, as do a second recovery card for the same source, a
card redoing the source's work, and same-role cards in the same campaign. Verifying is deliberately not a role apart
from implementing ("Verify green main CI" owns "Fix red main CI", test_kanban_create_overlap_url_slug.py).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

CASES = json.loads((Path(__file__).with_name("kanban_overlap_role_cases.json")).read_text())
STAGE_B, SELFHEAL, REMEASURE = CASES["stage_b"], CASES["selfheal"], CASES["remeasure"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(reg, "self_gateway", lambda: "hub")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    assert str(kb.kanban_db_path()).startswith(str(tmp_path))
    return h


def _replica(tmp_path, cards):
    path = tmp_path / "hub-state" / "hub.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS replica (gw TEXT, coll TEXT, key TEXT, data TEXT)")
    for c in cards:
        db.execute("INSERT INTO replica VALUES (?,?,?,?)",
                   (c["gateway"], "tasks", f"default/{c['board']}/{c['id']}", json.dumps(c)))
    db.commit()
    db.close()


def _local(board, title, body="", status=None, session="other-chat"):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_boards as kbb
    from hermes_cli import kanban_db_connect as kbc
    if board != kb.DEFAULT_BOARD:
        kbb.create_board(board)
    with kbc.connect_closing(board=board) as conn:
        tid = kb.create_task(conn, title=title, body=body, assignee="default", session_id=session)
        if status:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
            conn.commit()
    return tid


def _tool(args):
    from tools import kanban_tools as kt
    keep = ("title", "body", "assignee", "distinct_from", "distinct_reason", "idempotency_key", "model", "provider")
    return json.loads(kt._handle_create({"requires": [], **{k: v for k, v in args.items() if k in keep}}))


def _cli(argv, monkeypatch, *, neutral=True):
    """Replay original non-routing input; the old selector pair is no longer admissible.

    Keep the historical argv fixture intact, and explicitly migrate only its model/provider
    fields to a class route. Skill pairs are omitted because this home has no skills.
    ``neutral=False`` exercises the original pin's pre-write refusal separately.
    """
    import argparse
    from hermes_cli import kanban as kc
    monkeypatch.setattr(kc, "_check_dispatcher_presence", lambda **kw: (True, ""))
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args, it = [], iter(argv[1:])
    for a in it:
        if a == "--skill":
            next(it)
            continue
        args.append(a)
        if neutral and a in ('--model', '--provider'):
            original = next(it)
            assert original == SELFHEAL['args'][a[2:]]
            args.append('class:deep' if a == '--model' else 'auto')
    return kc._cmd_create(root.parse_args(args))


def _ids(out):
    return [m["id"] for m in out.get("matches") or []]


# --- 1. Stage B evidence-gap implementation vs the Stage C verifier and the report sender (tool, hub) ---

def _stage_b_board():
    """The two open unconventional-collab cards at refusal time (event 880 statuses), with their real text."""
    ids = {c["id"]: _local("unconventional-collab", c["title"], c["body"], c["status"]) for c in STAGE_B["cards"]}
    args = dict(STAGE_B["args"], distinct_from=[ids[i] for i in STAGE_B["args"]["distinct_from"]])
    return ids, args


def test_stage_b_evidence_repair_is_not_owned_by_the_verifier_or_the_sender(home):
    """The original call (verifier in distinct_from) creates. Without that override only the verifier is named:
    verifying is not a role apart from implementing (see kanban_overlap._ROLES), so that stays the caller's
    call; the report sender (a delivery) never is."""
    ids, args = _stage_b_board()
    verifier, sender = (ids[c["id"]] for c in STAGE_B["cards"])
    out = _tool(args)
    assert out.get("ok") is True, out
    without_override = _tool({k: v for k, v in args.items() if k not in ("distinct_from", "distinct_reason")}
                             | {"idempotency_key": "stage-b-evidence-gap-repair:replay2"})
    assert without_override.get("code") == "create_overlap", without_override
    assert _ids(without_override) == [verifier] and sender not in _ids(without_override), without_override


def test_stage_b_same_role_cards_in_the_campaign_are_still_refused(home):
    ids, args = _stage_b_board()
    verifier, sender = (ids[c["id"]] for c in STAGE_B["cards"])
    second_verifier = _tool({"title": "Stage C rerun: independent verification of the Stage B matrix and report",
                             "body": "Re-verify from raw records on Metta-AI/unconventional-collab.",
                             "assignee": "default"})
    assert second_verifier.get("code") == "create_overlap" and verifier in _ids(second_verifier), second_verifier
    second_sender = _tool({"title": "Follow-through: send the Stage C report to David's Stage B chats",
                           "body": "After Stage C on Metta-AI/unconventional-collab.", "assignee": "default"})
    assert second_sender.get("code") == "create_overlap" and sender in _ids(second_sender), second_sender
    repair = _local("unconventional-collab", args["title"], args["body"], "running")
    second_repair = _tool({"title": "Fix Stage B evidence gaps: unseal gate, timing and per-image labels",
                           "body": "Metta-AI/unconventional-collab PR3 follow-up.", "assignee": "default"})
    assert second_repair.get("code") == "create_overlap" and repair in _ids(second_repair), second_repair


def test_original_stage_b_duplicate_campaign_is_still_refused(home):
    """004623277d's incident: "Stage B: ..." vs running "Stage B1: run ..." (no role either side)."""
    b1 = _local("unconventional-collab", "Stage B1: run the frozen X/T tuning + confirmation matrix and "
                "external-anchor rescoring on rented GPUs", "Stage B / B1 of 3 on rented GPUs.")
    out = _tool({"title": "Stage B: GPU calibration, bounded comparison campaign and verified report",
                 "body": "David explicitly approved: 'approve stage b'. Execute frozen Stage B for "
                         "Metta-AI/unconventional-collab. Checked this turn: current PLAN.md read via gh.",
                 "assignee": "default"})
    assert out.get("code") == "create_overlap" and b1 in _ids(out), out


# --- 2. self-heal recovery of a crashed card vs that card (CLI, hub; source on mini/bb) ---

def test_original_selfheal_pin_is_rejected_without_mutating_home_or_replica(home, tmp_path, monkeypatch, capsys):
    from hermes_cli.kanban_db_connect import connect_closing

    _replica(tmp_path, [SELFHEAL['source']])
    replica = (tmp_path / 'hub-state' / 'hub.db').read_bytes()
    with connect_closing() as conn:
        before = tuple(conn.iterdump())
    assert _cli(SELFHEAL['argv'], monkeypatch, neutral=False) == 2
    assert 'model-neutral' in capsys.readouterr().err
    with connect_closing() as conn:
        assert tuple(conn.iterdump()) == before
    assert (tmp_path / 'hub-state' / 'hub.db').read_bytes() == replica


def test_selfheal_recovery_card_is_not_refused_by_its_source_card(home, tmp_path, monkeypatch, capsys):
    _replica(tmp_path, [SELFHEAL["source"]])
    rc = _cli(SELFHEAL["argv"], monkeypatch)
    captured = capsys.readouterr()
    assert rc == 0, (rc, captured.out, captured.err)
    assert "create_overlap" not in captured.out + captured.err
    assert json.loads(captured.out).get("ok", True) is not False


def test_selfheal_recovery_card_is_not_refused_by_its_source_card_via_the_tool(home, tmp_path):
    _replica(tmp_path, [SELFHEAL["source"]])
    out = _tool(dict(SELFHEAL["args"], model='class:deep', provider='auto'))
    assert out.get("ok") is True, out


def test_a_second_recovery_card_for_the_same_source_is_still_refused(home, tmp_path, monkeypatch, capsys):
    _replica(tmp_path, [SELFHEAL["source"]])
    first = _local("default", SELFHEAL["args"]["title"], SELFHEAL["args"]["body"])  # unkeyed, from another chat
    rc = _cli(SELFHEAL["argv"], monkeypatch)
    captured = capsys.readouterr()
    assert rc == 2, (rc, captured.out, captured.err)
    refusal = json.loads(captured.out)  # --json: the refusal is the JSON result
    assert refusal["code"] == "create_overlap" and _ids(refusal) == [first], refusal


def test_a_card_redoing_the_source_cards_work_is_still_refused(home, tmp_path):
    src = SELFHEAL["source"]
    _replica(tmp_path, [src])
    out = _tool({"title": src["title"], "body": "Refiled by bb_bridge on mini/bb.", "assignee": "default"})
    assert out.get("code") == "create_overlap" and src["id"] in _ids(out), out


# --- 3. burn-down 7-day re-measure vs four bb cluster fix/design cards (tool, MacBook; cards on mini/bb) ---

def test_remeasure_is_not_owned_by_the_cluster_fix_and_design_cards(home, tmp_path, monkeypatch):
    from hermes_cli import kanban_capability_registry as reg
    monkeypatch.setattr(reg, "self_gateway", lambda: "macbook")
    _replica(tmp_path, REMEASURE["cards"])
    out = _tool(REMEASURE["args"])
    assert out.get("ok") is True, out


def test_a_second_remeasure_and_a_same_cluster_repair_are_still_refused(home, tmp_path, monkeypatch):
    from hermes_cli import kanban_capability_registry as reg
    monkeypatch.setattr(reg, "self_gateway", lambda: "macbook")
    args = REMEASURE["args"]
    first = dict(REMEASURE["cards"][0], id="t_0123456789abcdef", title=args["title"], body=args["body"])
    _replica(tmp_path, [*REMEASURE["cards"], first])
    again = _tool(dict(args, idempotency_key="burndown-remeasure-20261107-b"))
    assert again.get("code") == "create_overlap" and _ids(again) == [first["id"]], again
    nsurl = next(c for c in REMEASURE["cards"] if "nsurl-timeout" in c["title"])
    repair = _tool({"title": "Fix burn-down nsurl-timeout: NSURLErrorDomain -1001 timeouts on the bb board",
                    "body": "", "assignee": "default"})
    assert repair.get("code") == "create_overlap" and nsurl["id"] in _ids(repair), repair


# --- the true duplicates the audits confirmed stay refused ---

@pytest.mark.parametrize("case", CASES["true_duplicates"], ids=lambda c: c["title"][:40])
def test_audited_true_duplicates_are_still_refused(home, tmp_path, monkeypatch, case):
    from hermes_cli import kanban_capability_registry as reg
    monkeypatch.setattr(reg, "self_gateway", lambda: case["self"])
    _replica(tmp_path, [case["card"]])
    out = _tool({"title": case["title"], "body": case.get("body", ""), "assignee": "default"})
    assert out.get("code") == "create_overlap" and case["card"]["id"] in _ids(out), out


# --- unit seams ---

@pytest.mark.parametrize(("title", "body", "role"), [
    ("Repair Stage B evidence gaps without changing frozen results", "", ("implement", "title")),
    ("Stage C: independent verification of the Stage B matrix and final report", "", (None, None)),
    ("Verify green main CI: daveey/hermes (after 992410c3)", "", (None, None)),  # owns "Fix red main CI"
    ("Follow-through: send the verified Stage C report to David's Stage B chats", "", ("deliver", "title")),
    ("bb-tuner burn-down: 7-day re-measure after the 10/31 failed-fix escalation deploy", "", ("measure", "title")),
    ("Design: burn-down nsurl-timeout (6 failed fixes): NSURLErrorDomain -1001 timeouts", "", ("implement", "title")),
    ("Burn-down g-cd77dccf: hub-runtime: deploy failed", "Follow the `bb-self-fix` skill: smallest fix, verify",
     ("implement", "body")),
    ("Stage B: GPU calibration, bounded comparison campaign and verified report", "", (None, None)),
    ("Send failing on Discord: fix the retry", "", (None, None)),  # two roles: ambiguous
    ("Gaps", "this card is not a second verification campaign; it repairs", ("implement", "body")),
    ("Discord send retries drop messages", "", (None, None)),  # a noun, not the job
    ("Telegram delivery receipts missing", "", (None, None)),
    ("Don't send the report until Stage C passes", "", (None, None)),
    ("No. Fix the unseal gate", "", ("implement", "title")),  # negation stops at the clause
    ("Main CI red", "When green, notify David", (None, None)),  # a closing step in a body is not the job
    ("Gateway mini config broken", "Investigate and deliver a fix", (None, None)),  # two roles: ambiguous
    ("Send failing: fix", "smallest fix", (None, None)),  # an ambiguous title never falls back to its body
])
def test_work_role(title, body, role):
    from hermes_cli import kanban_overlap as ko
    assert ko.work_role(title, body) == role


def test_a_body_only_role_never_decides_against_another_body_only_role():
    from hermes_cli import kanban_overlap as ko
    assert ko.distinct_target("GOTA capture", "GOTA capture", new_body="measure the movie",
                              card_body="fix the harness") is None
    assert ko.distinct_target("Re-measure the GOTA capture", "GOTA capture", card_body="fix the harness") == \
        "role measure != implement"


_BB_BODY = "Follow the `bb-self-fix` skill: smallest fix, verify"


@pytest.mark.parametrize(("new", "card", "new_body", "card_body"), [
    ("Fix red main CI: daveey/hermes", "Verify green main CI: daveey/hermes", "",
     "Send David the run link when it is green."),
    ("Fix red main CI: daveey/hermes", "Main CI red: daveey/hermes", "", "When green, notify David"),
    ("Gateway mini config broken", "Fix gateway mini config", "Investigate and deliver a fix", ""),
    ("Discord send retries drop messages", "Discord retries drop messages", "", _BB_BODY),
    ("Telegram delivery receipts missing", "Telegram receipts missing", "", _BB_BODY),
    # second review: a role noun as the first word is the subject the other card fixes
    ("Fix Telegram delivery receipts missing", "Delivery receipts missing on Telegram", "", ""),
    ("Fix send button disabled in BB client", "Send button disabled in BB client", "", _BB_BODY),
    ("Fix measurement drift in GOTA capture harness", "Measurement drift in GOTA capture harness", "", ""),
    ("Repair relay: drops messages from mini gateway", "Relay drops messages from mini gateway", "", ""),
])
def test_role_words_that_are_not_the_job_never_split_a_duplicate(new, card, new_body, card_body):
    """Review of the first cut: a role word in a body's closing step or a noun in a title made these distinct."""
    from hermes_cli import kanban_overlap as ko
    assert ko.distinct_target(new, card, new_body=new_body, card_body=card_body) is None
    assert ko.distinct_target(card, new, new_body=card_body, card_body=new_body) is None


def test_fix_red_main_ci_is_still_refused_by_a_verify_card_whose_body_sends_a_link(home):
    verify = _local("default", "Verify green main CI: daveey/hermes (after 992410c3)",
                    "Send David the run link when it is green. Do not mark done until main is green.")
    out = _tool({"title": "Fix red main CI: daveey/hermes", "body": "", "assignee": "default"})
    assert out.get("code") == "create_overlap" and verify in _ids(out), out


@pytest.mark.parametrize(("title", "card", "hit"), [
    ("self-heal: kanban card t_01a18aa302529d91 on mini/bb failed 2x (crashed,crashed): BB-1156",
     "t_01a18aa302529d91", True),
    ("kanban cards t_01a18aa302529d91+3 on mini/bb failed 6x (4 siblings 'load #N'): same error",
     "t_01a18aa302529d91", True),
    ("self-heal: kanban card t_01a18aa302529d91 on mini/bb failed 2x", "t_01a18aa302529d92", False),
    ("BB-1156: display on laptop failed (see t_01a18aa302529d91)", "t_01a18aa302529d91", False),
    # a title that says it does the source's work is a second owner, not a diagnosis
    ("Retry BB-1156 display on laptop blank after wake: card t_01a18aa302529d91 crashed, take over",
     "t_01a18aa302529d91", False),
    ("card t_01a18aa302529d91 crashed: finish its BB-1156 work", "t_01a18aa302529d91", False),
    # ...but the source title self-heal quotes after the colon is not read for that
    ("self-heal: kanban card t_01a18aa302529d91 on mini/bb failed 2x (crashed,crashed): Retry logic drops",
     "t_01a18aa302529d91", True),
])
def test_recovers(title, card, hit):
    from hermes_cli import kanban_overlap as ko
    assert ko.recovers(card, title, "Retry logic drops messages on wake") is hit
