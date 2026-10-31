"""scripts/ci/nightly_e2e.py: one open red card per alert scope, with the failing-set delta in each comment.

The previous contract filed a card per exact failing-test set, so a subset/superset/disjoint failure on main opened a
second (third, ...) open red card for the same incident. Now each scope (main, ref:<ref>, acceptance:<marker>) has
one red card; later red runs comment the run URL plus newly failing / no longer failing / still failing IDs, and a
red run after that card is done or archived files the next generation. State written by the fingerprint version
(``failed-<sha1>`` records, no IDs) is migrated: an open legacy card is reused, its baseline recovered from its
body when that body hashes to the legacy fingerprint, and labelled unknown otherwise.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from tests.ci.test_nightly_e2e import OWNER, T0, FakeBoard, FakeGitHub, _dispatch, _log, _raise, ne

A, B, C, D = (f"tests/e2e/{n}.py::t" for n in "abcd")


@pytest.fixture
def env():
    return FakeGitHub(), FakeBoard(), ne.load_state(Path("/nonexistent/state.json"))


def _red(gh, board, state, ids, *, i, ref="main", marker=""):
    """Dispatch run ``100 + i`` at ``ref`` (optionally an acceptance run), fail it with ``ids`` and report it."""
    entry = _dispatch(gh, state, ref=ref, now=T0 + i * 600)
    if marker:
        entry["acceptance_marker"], entry["acceptance_owner"] = marker, OWNER
    gh.finish(entry["run_id"], "failure", _log(*ids))
    return ne.report(gh, board, state, now=T0 + i * 600 + 10)


def _green(gh, board, state, *, i, ref="main"):
    entry = _dispatch(gh, state, ref=ref, now=T0 + i * 600)
    gh.finish(entry["run_id"], "success")
    return ne.report(gh, board, state, now=T0 + i * 600 + 10)


def _section(comment: str, name: str) -> list[str]:
    m = re.search(rf"^{name} \((\d+)\):\n((?:- .*(?:\n|$))*)", comment, re.MULTILINE)
    assert m, f"no {name!r} section in:\n{comment}"
    ids = re.findall(r"^- `([^`]+)`$", m.group(2), re.MULTILINE)
    assert len(ids) == int(m.group(1)), comment
    return ids


def _delta(comment: str) -> tuple[list[str], list[str], list[str]]:
    return (_section(comment, "Newly failing"), _section(comment, "No longer failing"),
            _section(comment, "Still failing"))


@pytest.mark.parametrize(
    "first,second,delta",
    [
        ([A, B], [A], ([], [B], [A])),  # subset
        ([A], [A, B], ([B], [], [A])),  # superset
        ([A], [C], ([C], [A], [])),  # disjoint
        ([A, B], [A, B], ([], [], [A, B])),  # identical
    ],
    ids=["subset", "superset", "disjoint", "identical"],
)
def test_any_later_red_comments_on_the_one_open_card_with_exact_delta(env, first, second, delta):
    gh, board, state = env
    _red(gh, board, state, first, i=0)
    lines = _red(gh, board, state, second, i=1)
    assert list(board.cards) == ["t_1"] and "updated t_1" in lines[0]
    (comment,) = board.cards["t_1"]["comments"]
    assert "https://gh/run/101" in comment
    assert _delta(comment) == delta


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("outcome", ["red", "green"])
def test_unreadable_owner_is_retried_not_replaced_or_forgotten(env, legacy, outcome):
    gh, board, state = env
    if legacy:
        _legacy(board, state, [A])
    else:
        _red(gh, board, state, [A], i=0)
    status = board.status
    board.status = lambda cid: None
    lines = (_red(gh, board, state, [B], i=1) if outcome == "red"
             else _green(gh, board, state, i=1))
    assert "retrying" in lines[0]
    assert len(board.cards) == 1 and len(state["pending"]) == 1
    board.status = status
    ne.report(gh, board, state, now=T0 + 620)
    assert len(board.cards) == 1 and state["pending"] == []
    assert len(board.cards["t_1"]["comments"]) == 1


def test_latest_failing_ids_are_the_next_baseline(env):
    gh, board, state = env
    _red(gh, board, state, [A], i=0)
    _red(gh, board, state, [A, B], i=1)
    _red(gh, board, state, [B, C], i=2)
    assert list(board.cards) == ["t_1"]
    assert _delta(board.cards["t_1"]["comments"][1]) == ([C], [A], [B])
    assert state["cards"]["red"]["failing"] == [B, C]


@pytest.mark.parametrize("closed", ["done", "archived"])
def test_red_after_the_card_is_closed_files_the_next_generation(env, closed):
    gh, board, state = env
    _red(gh, board, state, [A], i=0)
    board.cards["t_1"]["status"] = closed
    _red(gh, board, state, [B], i=1)
    assert list(board.cards) == ["t_1", "t_2"] and board.cards["t_1"]["comments"] == []
    assert [k for _, k in board.created] == ["nightly-e2e:red:1", "nightly-e2e:red:2"]
    assert B in board.cards["t_2"]["body"] and "https://gh/run/101" in board.cards["t_2"]["body"]
    _red(gh, board, state, [B, C], i=2)  # the new generation is the open card now
    assert len(board.cards) == 2 and _delta(board.cards["t_2"]["comments"][0]) == ([C], [], [B])


def test_main_historical_and_acceptance_scopes_keep_separate_red_cards(env):
    gh, board, state = env
    _red(gh, board, state, [A], i=0)
    _red(gh, board, state, [B], i=1, ref="29953696677a")
    _red(gh, board, state, [C], i=2, marker="scope-check")
    assert len(board.cards) == 3
    _red(gh, board, state, [D], i=3)  # disjoint from every scope's set: still only main's card hears of it
    assert _delta(board.cards["t_1"]["comments"][0]) == ([D], [A], [])
    assert board.cards["t_2"]["comments"] == [] and board.cards["t_3"]["comments"] == []
    _red(gh, board, state, [A], i=4, ref="29953696677a")
    assert len(board.cards["t_1"]["comments"]) == 1
    (ref_comment,) = board.cards["t_2"]["comments"]
    assert _delta(ref_comment) == ([A], [B], []) and "does not establish a current main incident" in ref_comment


def test_did_not_run_keeps_its_own_card_and_leaves_the_red_baseline(env):
    gh, board, state = env
    _red(gh, board, state, [A], i=0)
    entry = _dispatch(gh, state, now=T0 + 600)
    gh.finish(entry["run_id"], "cancelled")
    ne.report(gh, board, state, now=T0 + 610)
    assert len(board.cards) == 2 and "did not run" in board.cards["t_2"]["title"].lower()
    assert board.cards["t_1"]["comments"] == []
    _red(gh, board, state, [A, B], i=2)
    assert len(board.cards) == 2 and _delta(board.cards["t_1"]["comments"][0]) == ([B], [], [A])


def test_green_lists_what_stopped_failing_and_resets_the_baseline(env):
    gh, board, state = env
    _red(gh, board, state, [A, B], i=0)
    _green(gh, board, state, i=1)
    (green,) = board.cards["t_1"]["comments"]
    assert "GREEN" in green and "https://gh/run/101" in green
    assert _section(green, "No longer failing") == [A, B]
    _red(gh, board, state, [A], i=2)
    assert _delta(board.cards["t_1"]["comments"][1]) == ([A], [], [])


# --- failed comment retry ----------------------------------------------------------------------------------------


class FlakyCommentBoard(FakeBoard):
    def __init__(self):
        super().__init__()
        self.fail_next = 0

    def comment(self, cid, body, board=None):
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("hermes kanban comment failed (1): db locked")
        super().comment(cid, body, board)


def test_failed_red_comment_keeps_run_pending_and_baseline_until_it_lands():
    gh, board, state = FakeGitHub(), FlakyCommentBoard(), ne.load_state(Path("/nonexistent/state.json"))
    _red(gh, board, state, [A], i=0)
    board.fail_next = 1
    lines = _red(gh, board, state, [B], i=1)
    assert "retrying" in lines[0] and [e["run_id"] for e in state["pending"]] == [101]
    assert board.cards["t_1"]["comments"] == [] and state["cards"]["red"]["failing"] == [A]
    ne.report(gh, board, state, now=T0 + 600 + 20)  # next tick
    assert state["pending"] == [] and list(board.cards) == ["t_1"]
    (comment,) = board.cards["t_1"]["comments"]
    assert "https://gh/run/101" in comment and _delta(comment) == ([B], [A], [])
    assert state["cards"]["red"]["failing"] == [B]


def test_failed_green_comment_does_not_reset_the_baseline():
    gh, board, state = FakeGitHub(), FlakyCommentBoard(), ne.load_state(Path("/nonexistent/state.json"))
    _red(gh, board, state, [A], i=0)
    board.fail_next = 1
    lines = _green(gh, board, state, i=1)
    assert "retrying" in lines[0] and state["cards"]["red"]["failing"] == [A]
    ne.report(gh, board, state, now=T0 + 600 + 20)
    (green,) = board.cards["t_1"]["comments"]
    assert _section(green, "No longer failing") == [A] and state["cards"]["red"]["failing"] == []


# --- migration of fingerprint state ------------------------------------------------------------------------------


def _legacy(board, state, ids, *, ref="main", marker="", scope_field=True, body=None):
    """A card and state record exactly as the fingerprint version filed them (``failed-<sha1>``, no IDs)."""
    entry = {"ref": ref, "sha": "a" * 40, "dispatched_at": T0 - 86400, "url": "https://gh/run/1"}
    if marker:
        entry["acceptance_marker"], entry["acceptance_owner"] = marker, OWNER
    key = ne._scoped_key(entry, ne.failure_key(ids))
    cid = board.create(ne._title(entry, ids, ""), body if body is not None else
                       ne._card_body("failed", entry, ids, "=== Summary ==="), f"nightly-e2e:{key}:1")
    state["cards"][key] = {"id": cid, "n": 1, **({"scope": ne.scope_of(entry)} if scope_field else {})}
    return cid, key


@pytest.mark.parametrize("scope_field", [True, False], ids=["scoped-record", "pre-scope-record"])
def test_open_legacy_card_is_reused_with_baseline_recovered_from_its_body(env, scope_field):
    gh, board, state = env
    cid, key = _legacy(board, state, [A, B], scope_field=scope_field)
    _red(gh, board, state, [A, C], i=0)
    assert list(board.cards) == [cid]
    (comment,) = board.cards[cid]["comments"]
    assert "https://gh/run/100" in comment and _delta(comment) == ([C], [B], [A])
    assert key not in state["cards"] and state["cards"]["red"]["id"] == cid
    assert state["cards"]["red"]["failing"] == [A, C]
    assert state["cards"]["red"]["idempotency_key"] == f"nightly-e2e:{key}:1"
    _red(gh, board, state, [B], i=1)
    assert state["cards"]["red"]["idempotency_key"] == f"nightly-e2e:{key}:1"


def test_legacy_baseline_known_from_fingerprint_when_ids_match(env):
    gh, board, state = env
    cid, _key = _legacy(board, state, [A], body="(body lost)")
    _red(gh, board, state, [A], i=0)
    assert list(board.cards) == [cid] and _delta(board.cards[cid]["comments"][0]) == ([], [], [A])


@pytest.mark.parametrize("body", ["(body lost)", "Failing tests (2):\n- `tests/e2e/x.py::t`\n- `tests/e2e/y.py::t`\n"],
                         ids=["no-list", "list-not-matching-fingerprint"])
def test_unrecoverable_legacy_baseline_is_labelled_unknown(env, body):
    gh, board, state = env
    cid, _key = _legacy(board, state, [A, B], body=body)
    _red(gh, board, state, [C], i=0)
    (comment,) = board.cards[cid]["comments"]
    assert "baseline unknown" in comment.lower() and "https://gh/run/100" in comment
    assert _section(comment, "Failing now") == [C]
    assert "Newly failing" not in comment and "No longer failing" not in comment
    _red(gh, board, state, [C, D], i=1)  # the comment recorded a real baseline for the next run
    assert _delta(board.cards[cid]["comments"][1]) == ([D], [], [C])


def test_several_open_legacy_cards_collapse_to_one_and_green_comments_once(env):
    gh, board, state = env
    first, _ = _legacy(board, state, [A, B])
    second, _ = _legacy(board, state, [A])
    _red(gh, board, state, [A], i=0)
    assert len(board.cards) == 2  # nothing new filed
    # the card whose fingerprint matches is reused; the duplicate is pointed at it and dropped from state
    assert _delta(board.cards[second]["comments"][0]) == ([], [], [A])
    (superseded,) = board.cards[first]["comments"]
    assert second in superseded and "superseded" in superseded.lower()
    assert [k for k in state["cards"]] == ["red"]
    _green(gh, board, state, i=1)
    assert sum("GREEN" in c for c in board.cards[second]["comments"]) == 1
    assert not any("GREEN" in c for c in board.cards[first]["comments"])


def test_green_before_any_migrating_red_comments_each_open_legacy_card_once(env):
    gh, board, state = env
    cid, _ = _legacy(board, state, [A])
    _green(gh, board, state, i=0)
    assert [c.count("GREEN") for c in board.cards[cid]["comments"]] == [1]


def test_closed_legacy_card_is_not_reused_and_next_generation_is_filed(env):
    gh, board, state = env
    cid, key = _legacy(board, state, [A])
    board.cards[cid]["status"] = "done"
    _red(gh, board, state, [A], i=0)
    assert len(board.cards) == 2 and board.cards[cid]["comments"] == []
    assert key not in state["cards"] and state["cards"]["red"]["failing"] == [A]


def test_legacy_cards_migrate_only_within_their_own_scope(env):
    gh, board, state = env
    acc, acc_key = _legacy(board, state, [A], marker="scope-check")
    hist, hist_key = _legacy(board, state, [A], ref="29953696677a")
    _red(gh, board, state, [A], i=0)
    assert len(board.cards) == 3 and board.cards[acc]["comments"] == [] and board.cards[hist]["comments"] == []
    assert acc_key in state["cards"] and hist_key in state["cards"]
    _red(gh, board, state, [B], i=1, ref="29953696677a")
    assert len(board.cards) == 3 and _delta(board.cards[hist]["comments"][0]) == ([B], [A], [])
    assert board.cards[acc]["comments"] == []


def test_failed_migration_comment_keeps_legacy_record_and_retries_on_the_same_card():
    gh, board, state = FakeGitHub(), FlakyCommentBoard(), ne.load_state(Path("/nonexistent/state.json"))
    cid, key = _legacy(board, state, [A])
    board.fail_next = 1
    lines = _red(gh, board, state, [B], i=0)
    assert "retrying" in lines[0] and key in state["cards"] and "red" not in state["cards"]
    ne.report(gh, board, state, now=T0 + 20)
    assert list(board.cards) == [cid] and _delta(board.cards[cid]["comments"][0]) == ([B], [A], [])
    assert key not in state["cards"] and state["cards"]["red"]["id"] == cid


def test_red_card_state_survives_the_cli_state_file(tmp_path, monkeypatch):
    gh, board, path = FakeGitHub(), FakeBoard(), tmp_path / "state.json"
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr(ne, "RestGitHub", lambda *a: gh)
    monkeypatch.setattr(ne, "NightlyBoard", lambda *a: board)
    for rid, ids in [(100, [A]), (101, [B])]:
        assert ne.main(["dispatch", "--state", str(path)]) == 0
        gh.finish(rid, "failure", _log(*ids))
        assert ne.main(["report", "--state", str(path)]) == 0
    assert list(board.cards) == ["t_1"] and _delta(board.cards["t_1"]["comments"][0]) == ([B], [A], [])
    assert ne.load_state(path)["cards"]["red"]["failing"] == [B]


def test_persistent_board_error_on_red_update_past_deadline_still_never_drops_the_run():
    gh, board, state = FakeGitHub(), FakeBoard(), ne.load_state(Path("/nonexistent/state.json"))
    _red(gh, board, state, [A], i=0)
    entry = _dispatch(gh, state, now=time.time() - 5 * 3600)
    gh.finish(entry["run_id"], "failure", _log(B))
    board.comment = _raise(RuntimeError("db locked"))
    board.create = _raise(RuntimeError("db locked"))
    lines = ne.report(gh, board, state, now=time.time())
    assert ne.BOARD_FAILED in lines[0] and [e["run_id"] for e in state["pending"]] == [101]
    assert state["cards"]["red"]["failing"] == [A]
