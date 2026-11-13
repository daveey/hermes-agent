"""One isolated negative per guard of the same-creator overlap check and its same-run re-issue. [daveey]

PR #2329's post-merge mutant run (card t_01a1d5e5bf3ccad9) found 12 guards no test needed: dropping any one
left all 41 tests green. The re-issue guards were each redundant because the only negative case (the 2 h later
round-3 nit t_01a1d4e145f389a5) fails several of them at once. Each test here starts from one passing case and
changes exactly one input, so dropping its guard turns a refusal into a silent reuse of the first card (or an
admitted card into a refusal), with no other guard to catch it.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_overlap as ko
from hermes_cli import kanban_overlap_own as own

CREATOR = "t_0c0ea7000000aaaa"
OTHER = "t_0c0ea7000000bbbb"
RUN_STARTED = 1_794_000_000
GATEWAYS = {"hub", "hermes-hub"}
TITLE = "Quit-waiter partial prune: restore stale cache entries during drain"
# Three rare snake_case identifiers both bodies name: a shared fix site (kanban_overlap_own.shared_code_reason).
IDS = "Touches drain_quit_waiter, prune_partial_entries and restore_stale_cache."


def _card(id_: str, title: str, body: str = "", *, creator: str = CREATOR, local: bool = True,
          has_key: bool = False, created_at=RUN_STARTED + 60, session_id=None, assignee="default") -> dict:
    return {"id": id_, "board": "default", "gateway": "hub", "status": "running", "title": title, "body": body,
            "assignee": assignee, "created_at": created_at, "session_id": session_id, "creator_task_id": creator,
            "local": local, "key_family": None, "has_key": has_key, "placed_as": None, "awaits": None, "chats": []}


FIRST = "t_0c0ea70000001111"


def _find(card: dict, *, title: str = TITLE, body: str = "", session_id=None, assignee="default",
          shared_code: bool = True, run_started=RUN_STARTED):
    matches, _ = ko._find(title=title, body=body, target_board="default", session_id=session_id,
                          self_tid=CREATOR, parents=[], family=None, self_gw="hub", cards=[card],
                          gateways=set(GATEWAYS), assignee=assignee, run_started=run_started,
                          shared_code=shared_code)
    return next((m for m in matches if m["id"] == card["id"]), None)


# -- the same-run re-issue (kanban_overlap_own.reissue) ------------------------------------------------------

def test_baseline_same_run_keyless_local_reissue_deduplicates():
    match = _find(_card(FIRST, TITLE))
    assert match is not None and match["_reissue"] is True
    assert ko.verdict([match], [], "")["deduplicated"] is True


@pytest.mark.parametrize("change", [
    pytest.param({"has_key": True}, id="first-card-had-an-idempotency-key"),
    pytest.param({"created_at": RUN_STARTED - 60}, id="first-card-from-an-earlier-run"),
    pytest.param({"local": False}, id="first-card-is-a-replica-row-not-on-this-board"),
])
def test_one_failed_reissue_condition_refuses_instead_of_reusing(change):
    """Each card differs from the baseline in one field only; that field alone must make it a refusal."""
    match = _find(_card(FIRST, TITLE, **change))
    assert match is not None, "the own card still matches"
    assert match["_reissue"] is False
    out = ko.verdict([match], [], "")
    assert out["ok"] is False and out["code"] == ko.EVENT and not out.get("deduplicated")


def test_a_reworded_title_below_the_reissue_score_refuses_instead_of_reusing():
    reworded = "Quit-waiter partial prune: restore stale cache entries during drain on hub worker gateway"
    match = _find(_card(FIRST, reworded))
    assert match is not None, "the titles still share most words"
    assert match["_score"] < own.REISSUE_SCORE
    assert match["_reissue"] is False and ko.verdict([match], [], "")["ok"] is False


def test_a_shared_code_only_match_is_refused_not_deduplicated():
    """Same-run, keyless, local and a near-identical short title, but the only reason is the shared fix site:
    too weak a signal to hand back the first card silently."""
    short = "Drain prune"  # 2 words: too few for the title rule, a perfect title score
    match = _find(_card(FIRST, short, IDS), title=short, body=IDS)
    assert match is not None and own.is_reason(match["why"])
    assert match["_score"] >= own.REISSUE_SCORE
    assert match["_reissue"] is False and ko.verdict([match], [], "")["ok"] is False


# -- the shared-code rule ------------------------------------------------------------------------------------

def test_shared_identifiers_with_unrelated_titles_are_not_a_fix_site():
    match = _find(_card(FIRST, "Telemetry exporter: batch spans per tenant", IDS),
                  title="Mail relay: retry bounced digests", body=IDS)
    assert match is None


def test_the_in_transaction_recheck_skips_the_shared_code_rule():
    card = _card(FIRST, "Drain prune", IDS)
    assert _find(card, title="Drain prune", body=IDS) is not None
    assert _find(card, title="Drain prune", body=IDS, shared_code=False) is None


def test_a_shared_code_match_the_new_card_cites_by_id_is_admitted():
    card = _card(FIRST, "Drain prune", IDS)
    assert _find(card, title="Drain prune", body=IDS) is not None
    assert _find(card, title="Drain prune", body=f"{IDS} Follow-up to {FIRST}.") is None


# -- which own cards are compared, and the admits ------------------------------------------------------------

def test_an_own_card_from_the_same_chat_session_is_still_compared():
    """The incident shape: the worker's own cards carry its chat session as their ``created`` session."""
    assert _find(_card(FIRST, TITLE, session_id="sess-1"), session_id="sess-1") is not None
    # Another creator in the same chat keeps the session exemption.
    assert _find(_card(FIRST, TITLE, creator=OTHER, session_id="sess-1"), session_id="sess-1") is None


def test_a_review_and_its_implement_card_from_one_creator_are_admitted():
    implement = "PR #2110 quit-waiter drain prune stale cache entries"
    review = "Review PR #2110 quit-waiter drain prune stale cache entries"
    assert _find(_card(FIRST, implement), title=implement + " again") is not None
    assert _find(_card(FIRST, implement), title=review) is None


# -- through kanban_create, as a dispatcher worker -----------------------------------------------------------

@pytest.fixture
def worker(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    monkeypatch.setattr(reg, "self_gateway", lambda: "hub")
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: True)
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="Review PR #2110 at exact head", assignee="default")
        assert kb.claim_task(conn, creator) is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)
    return creator


def _tool_create(**args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_create({"requires": [], "placement": "local", **args}))


def test_tool_path_judges_the_resolved_assignee(worker):
    """No ``assignee`` argument: the tool resolves the profile, and the check must see that one, so a same-title
    card for another assignee is a different job."""
    first = _tool_create(title=TITLE, assignee="reviewer")
    assert first["ok"] and first["task_id"]
    second = _tool_create(title=TITLE)
    assert second["ok"] and not second.get("deduplicated") and second["task_id"] != first["task_id"]
