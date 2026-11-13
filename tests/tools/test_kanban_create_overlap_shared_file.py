"""kanban_create refuses a second owner whose title names the same code file as an open card's. [daveey]

Incident (2026-11-13, t_01a1d290dc768ba1): hub card t_01a1d25511610669 ("State suite: ... runs tests/run_ci.py on a
pool worker ...") was open when a worker-5 card created t_01a1d2586e1e5515 ("Local full state suite on pool workers:
run_ci.py fails ...") 3.5 min later. Same work; the worker ran a 644 s suite before it was stopped as superseded. The
titles share 8 significant words of 19, under the half-of-the-longer-title rule, and nothing else matched. A title
naming the same code file (``run_ci.py``) plus enough shared words is the same job, the way a shared failing test is.

Replay over 14 days of replica cards (8112 creates, 482 naming a code file): the rule adds 2 refusals, this pair and
two ``sync-handoff.test.ts`` deflake cards for the same timed-out test (10-30), both true duplicates. Three more pairs
it fires on cite the open card by id and are admitted like a failure-only match that cites its owner. The negatives
are real pairs that name one file for different jobs.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_overlap as ko
from tests.tools import test_kanban_create_overlap as helpers

home = helpers.home

HUB_CARD = ("State suite: scripts/run-state-suite-local runs tests/run_ci.py on a pool worker the way the "
            "hermes-sync tests CI job does (green on unchanged origin/state)")
WORKER_DUP = ("Local full state suite on pool workers: run_ci.py fails 4 modules vs the worker's live core "
              "(hermes_state_lockguard missing), not core-fixture as CI uses")


def remote(tmp_path, title, *, cid="t_01a1d25511610669", body=""):
    (tmp_path / "hub-state" / "hub.db").unlink(missing_ok=True)
    helpers._fake_replica(tmp_path / "hub-state" / "hub.db", [
        ("hub", {"id": cid, "board": "default", "title": title, "body": body, "status": "running",
                 "gateway": "hub", "sessions": [{"session": "20261112_055658_bb1bdd", "rel": "created"}]})])


def check(title, body=""):
    with kbc.connect_closing() as conn:
        refusal, _ = ko.check(conn, {"body": body}, courier=False, title=title, session_id="worker-5-run",
                              self_tid=None, parents=[])
    return json.loads(refusal) if refusal else None


@pytest.fixture
def worker(home, monkeypatch):
    from hermes_cli import kanban_capability_registry as reg
    monkeypatch.setattr(reg, "self_gateway", lambda: "worker-5")
    return home


def test_run_ci_duplicate_from_another_gateway_is_refused_naming_the_hub_card(worker, tmp_path):
    remote(tmp_path, HUB_CARD)
    out = check(WORKER_DUP)
    assert out and out["code"] == "create_overlap", out
    (m,) = out["matches"]
    assert m["id"] == "t_01a1d25511610669" and m["gateway"] == "hub"
    assert "run_ci.py" in m["why"], m["why"]


def test_shared_file_with_too_few_shared_words_is_not_a_match(worker, tmp_path):
    """Real pair (11-12): one file, two jobs (fix a validator vs read back an earlier proof); 5 shared words."""
    remote(tmp_path, "Hub readback + live read-only proof of ci_run_metadata.py (state 1b7bc2d)")
    assert check("Fix ci_run_metadata.py token validator rejecting real relay-minted ghs_ tokens (contain '.'), "
                 "then re-run live proof") is None


def test_same_words_without_a_shared_file_stay_under_the_half_rule(worker, tmp_path):
    """The file is the strong signal: the same titles with the file name dropped share too little."""
    remote(tmp_path, HUB_CARD.replace("tests/run_ci.py", "the state tests"))
    assert check(WORKER_DUP.replace("run_ci.py", "the runner")) is None


def test_a_different_file_of_the_same_kind_is_not_a_match(worker, tmp_path):
    remote(tmp_path, HUB_CARD)
    assert check(WORKER_DUP.replace("run_ci.py", "run_tests.py")) is None


def test_two_dotted_files_sharing_only_their_suffix_are_not_a_match(worker, tmp_path):
    """``sync-handoff.test.ts`` and ``theme-store.test.ts`` are two files; ``test.ts`` is not a file both name."""
    old = "Deflake apps/desktop sync-handoff.test.ts: wall-clock ticks race slow store imports under CI load"
    new = ("Deflake apps/desktop theme-store.test.ts: persisted palette migration reorders keys after rehydrate when "
           "the renderer boots twice under CI load")
    assert len(ko.words(new) & ko.words(old)) >= ko.MIN_FILE_SHARED  # enough shared words; only the file differs
    remote(tmp_path, old)
    assert check(new) is None
    assert ko.code_files("see src/lib/sync-handoff.test.ts") == {"sync-handoff.test.ts"}


def test_a_card_that_cites_the_owner_by_id_is_admitted(worker, tmp_path):
    """Real pair (11-13 01:00): the hub deflake card names worker-5's open card t_01a1d23f8c1a6284 in its body."""
    remote(tmp_path, "Deflake tests/test_core_drain_idle.py under load (idle_path_gateway_drain_wait..., "
                     "ioreg_failure_means_no_window) on worker-5", cid="t_01a1d23f8c1a6284")
    new = ("Deflake the three windowed cases in tests/test_core_drain_idle.py: their real 0.2 s claim lease "
           "expires under a 0.3 s host stall")
    assert check(new, body="Follow-up of t_01a1d23f8c1a6284: the windowed cases still fail.") is None
    out = check(new, body="The windowed cases fail on a loaded host.")
    assert out and out["code"] == "create_overlap", out


def test_distinct_target_still_admits_a_shared_file_match(worker, tmp_path):
    remote(tmp_path, HUB_CARD + " on daveey-mini")
    assert check(WORKER_DUP + " on David-MBP") is None
