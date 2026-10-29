"""create_overlap: a ``/hermes`` in a URL or path is not the ``hermes`` board. [daveey]

Hub t_01a189b2d186a986. On 2026-10-29 (14:01-15:01 PDT) kanban_create refused seven unrelated cards. The
core repo is daveey/hermes and the config checkout ~/code/hermes, so every CI link and path "named" board
``hermes``; a slash list (``Slack/Discord/BB Inbox``) named board ``bb``; one shared title word ("skill",
"create") or the hostname David-MBP then matched some open card there. One named card (t_d6973c6f) had been
done since 10-26: the hub replica still holds its frozen pre-profile row ``hermes/t_d6973c6f`` = running
next to the current ``default/hermes/t_d6973c6f`` = done.

Titles and the triggering body lines below are the real ones from those creates.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

# Open cards on the MacBook's hermes board that the refusals named (titles as in the hub replica).
TIMEOUTS = ("t_01a188f6e0f05080", "Desktop backend timeouts on David-MBP: 51 in 30 min")
STALLS = ("t_01a189901d50acc8", "Fix recurring local Desktop session GET stalls after 24c3eda: 9.43s at 14:26 PDT")
SKILL_SYNC = ("t_d6973c6f", "Fix: Auto-stash dirty skill files during hermes-sync to prevent fleet sync failures")

RUN_URL = "https://github.com/daveey/hermes/actions/runs/18934051234/job/53995310442"


def _flaky_body(test_id: str) -> str:
    # scripts/ci/flaky_kanban.py::_card_body, as filed by the hub cron ci-flaky-cards.
    return (f"Flaky test: `{test_id}`\n\nIt failed on its first attempt and passed when CI retried it "
            f"({RUN_URL}, branch `main`).\nThe retry keeps the job green, so this card is the only signal.\n\n"
            "Usual cause on the hub runners: a fixed wall-clock deadline that a CPU-throttled container misses.")


FLAKY = [
    "tests/hermes_cli/test_update_stale_dashboard_review.py::test_control_backend_with_no_review_is_stopped_as_today",
    "tests/tools/test_skill_manager_tool.py::TestSkillMutationLock::test_concurrent_patches_keep_both_updates",
    "tests/scripts/desktop_update/test_desktop_update_posix_handoff_protocol.py"
    "::test_line_two_stays_young_through_the_r6_release_wait",
]

OBSERVED = {
    "14:01:14 idle backend CPU": (
        "Desktop backends burn ~40% of a core each with no turn work: request-thread SQL, tui-notif-pollers, "
        "hub-sync (David-MBP 10-29)",
        "Done when the fix is committed and pushed, or a PR is merged per the daveey/hermes AGENTS.md, with tests."),
    "14:40:59 deflake stale dashboard review": (f"Deflake {FLAKY[0]}", _flaky_body(FLAKY[0])),
    "14:40:59 deflake skill manager": (f"Deflake {FLAKY[1]}", _flaky_body(FLAKY[1])),
    "14:46:25 lease live check": (
        "Live check on David-MBP: lease fix f0f483955b stops the 'run_uid required' warnings and logs a lapsed "
        "renewal after a sleep",
        "Fix f0f483955b (card t_01a1893335f58583, PR https://github.com/daveey/hermes/pull/219) is proven in "
        "tests; ~/code/hermes/state holds the probe."),
    "14:51:57 lid-close live check": (
        "Live check on David-MBP: Kanban runs held across a real lid-close (60+ min) survive now that the "
        "awake-time fix is live",
        "- On the `hermes` board, run 108 (t_01a1889b057a1d39) ended `timed_out` at 11:29:14 on 10-29, 37 s "
        "after the wake."),
    "14:52:05 deflake posix handoff": (f"Deflake {FLAKY[2]}", _flaky_body(FLAKY[2])),
}

# This card's own first create (15:01 PDT): refused with 14 matches on board bb.
SELF_TITLE = ("create_overlap guard: '/hermes' in a URL or path counts as naming the hermes board, so unrelated "
              "cards are refused (follow-up to 004623277d)")
SELF_BODY = (
    "In `hermes_cli/kanban_overlap.py`, `_explicit_board_ref(slug, text)` counts `/<slug>` anywhere. The core "
    "repo is daveey/hermes and the config checkout is ~/code/hermes, so ordinary URLs and paths "
    "(`https://github.com/daveey/hermes/actions/runs/...`, `~/code/hermes/...`) name the board called hermes.\n"
    "This card's own first create (15:01 PDT) was refused with 14 matches on the bb board: its body says "
    "\"the bb board\".")
# 14:37 PDT, chat 20261026_170127_f07c32: refused with 8 matches on board bb.
RECON_TITLE = ("PR-approval reconciler can't read PR #1950's ask (checked: 0 every minute): accept the observed "
               "wordings, report unparsed asks, check asks when filed")
RECON_BODY = (
    "- `home/scripts/pr_approval_reconcile.py` (daveey/hermes@state, 8a3e3118) runs every minute on the MacBook.\n"
    "  - action: \"Approve PR #1950 at head 21cb9c1c2 on GitHub, or OK the bb approval ask in Slack/Discord/BB "
    "Inbox. Unblock = queue merges+deploys and I verify; archive = close the PR, findings stay open.\"\n"
    "  - a fixed surface list; this ask says \"slack/discord/bb inbox\".")
# Open bb cards that share exactly one generic title word with those creates.
BB_ONE_WORD = ["Merge-train cards stuck in review on daveey-mini", "BB release refused by the hub gate",
               "Voice create flow drops the wake word", "PR #2041 approval: kb client latency", "Hub ask queue drains"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(reg, "self_gateway", lambda: "hub")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return h


def _replica(tmp_path, rows):
    """hub.db with ``(gw, key, task)`` rows, keys as the hub writes them (legacy ``<board>/<id>`` included)."""
    path = tmp_path / "hub-state" / "hub.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, data TEXT)")
    for gw, key, task in rows:
        db.execute("INSERT INTO replica VALUES (?,?,?,?)", (gw, "tasks", key, json.dumps(task)))
    db.commit()
    db.close()


def _mac(tid, title, status, key=None, board="hermes"):
    return ("macbook", key or f"default/{board}/{tid}",
            {"id": tid, "board": board, "title": title, "status": status, "gateway": "macbook"})


@pytest.fixture
def macbook_hermes(home, tmp_path):
    _replica(tmp_path, [
        _mac(*TIMEOUTS, "todo"), _mac(*STALLS, "ready"),
        _mac(*SKILL_SYNC, "running", key=f"hermes/{SKILL_SYNC[0]}"),  # frozen legacy row
        _mac(*SKILL_SYNC, "done"),
    ])


def _bb_board(titles=BB_ONE_WORD):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_boards as kbb
    from hermes_cli import kanban_db_connect as kbc
    kbb.create_board("bb")
    conn = kbc.connect(board="bb")
    try:
        return [kb.create_task(conn, title=t, assignee="default", session_id="other-chat") for t in titles]
    finally:
        conn.close()


def _create(title, body, **args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_create({"title": title, "body": body, "assignee": "default", "requires": [],
                                         **args}))


@pytest.mark.parametrize("row", sorted(OBSERVED))
def test_observed_refusals_now_create(macbook_hermes, row):
    title, body = OBSERVED[row]
    out = _create(title, body)
    assert out.get("code") != "create_overlap", out
    assert out.get("ok") is True, out


def test_this_cards_own_first_create_is_not_refused(macbook_hermes):
    _bb_board()
    out = _create(SELF_TITLE, SELF_BODY)
    assert out.get("ok") is True, out


def test_slash_list_naming_bb_is_not_a_board_reference(home):
    _bb_board()
    out = _create(RECON_TITLE, RECON_BODY)
    assert out.get("ok") is True, out


@pytest.mark.parametrize(("slug", "text"), [
    ("hermes", "Flaked in https://github.com/daveey/hermes/actions/runs/1 (branch main)"),
    ("hermes", "See ~/code/hermes/home/scripts and /home/ubuntu/.hermes/hermes/x"),
    ("hermes", "per the daveey/hermes AGENTS.md"),
    ("bb", "OK the bb approval ask in Slack/Discord/BB Inbox"),
    ("hermes", "macbook/hermes-agent and hub/hermes/extra/path"),
])
def test_plain_slug_inside_a_url_or_path_is_not_a_reference(slug, text):
    from hermes_cli.kanban_overlap import _names_board
    assert _names_board(slug, "Deflake tests/tools/x.py::test_y", text, {"hub", "macbook", "mini"}) is False


@pytest.mark.parametrize("text", [
    "on the `hermes` board", "board hermes", "board: hermes", "hermes kanban --board hermes create",
    "--board=hermes", "see @task:hermes@macbook/t_01a188f6e0f05080", "macbook/hermes", "macbook/hermes/t_d6973c6f",
])
def test_real_board_references_still_count(text):
    from hermes_cli.kanban_overlap import _names_board
    assert _names_board("hermes", "unrelated title", text, {"hub", "macbook", "mini"}) is True


def test_true_duplicate_naming_a_plain_word_board_is_still_refused(macbook_hermes):
    out = _create("David-MBP desktop backend timeouts again: 40 in 30 min",
                  "Same as before; file it on board hermes.")
    assert out.get("code") == "create_overlap", out
    assert [m["id"] for m in out["matches"]] == [TIMEOUTS[0]]


def test_one_shared_word_with_a_named_plain_board_does_not_refuse(macbook_hermes):
    out = _create("Rotate the Desktop signing key", "Filed for the hermes board.")
    assert out.get("ok") is True, out


def test_a_done_card_is_never_named_through_its_stale_legacy_replica_row(macbook_hermes):
    # Same title as the done card plus an explicit board reference: only an open card could be named.
    out = _create(SKILL_SYNC[1], "On the hermes board.")
    assert SKILL_SYNC[0] not in [m.get("id") for m in out.get("matches") or []], out
    assert out.get("ok") is True, out


def test_legacy_row_without_a_qualified_twin_still_counts(home, tmp_path):
    _replica(tmp_path, [_mac(*STALLS, "ready", key=f"hermes/{STALLS[0]}")])
    out = _create("Desktop session GET stalls recurring after 24c3eda at 14:26 PDT", "")
    assert out.get("code") == "create_overlap", out
    assert [m["id"] for m in out["matches"]] == [STALLS[0]]


def test_a_refusal_matching_only_remote_cards_logs_which_cards(macbook_hermes, caplog):
    """No board event is written when every match is on another gateway; the log line is the audit trail."""
    import logging
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_overlap"):
        out = _create("David-MBP desktop backend timeouts again: 40 in 30 min", "")
    assert out.get("code") == "create_overlap", out
    line = next(r.getMessage() for r in caplog.records if "create overlap: refused" in r.getMessage())
    assert f"macbook/hermes/{TIMEOUTS[0]} (titles share" in line, line
