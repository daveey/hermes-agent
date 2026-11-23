"""MANIFEST130 S1/S2: direct standing promotion validates every emitted representation.

Only ``_ctx_standing`` (worker context) and ``_standing_wait_guard`` (kanban_block hold) opt in. The
compactor, retirement/forwarding and the default helpers keep their existing behavior; this module does
not claim the retained historical A129 compactor observation is fixed. Cards live on an isolated board.
"""
from __future__ import annotations
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import json

import pytest

from agent import standing_instructions as si
from hermes_cli.kanban_model_neutral import validate_text

MODEL = "claude-opus-5-5"
SEPARATORS = ["\n", "\r\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
ASK = {"action": "Pick when to restart the mini gateways. Unblock = restart; archive = skip.",
       "why_human": "restart needs consent", "interaction": "reply", "scope": "mini launcher migration",
       "evidence": "card comments"}
BLOCK = {"reason": "1) Choose now, when idle or later for the restart. 2) Then unblock this card",
         "kind": "needs_input", "human_request": ASK}
# assessment121 counterexample 1: the catalog name ends exactly at character 400, suffix beyond it.
SUFFIX_WORDS = "Do not wait " + "p" * 363 + f" must use {MODEL}xyz"
SUFFIX_TRUNC = " ".join(SUFFIX_WORDS.split())[:400]
# assessment121 counterexample 2: two physical lines that only refuse once joined.
CR_TEXT = f"standing: Do not wait, must use\r{MODEL}"
JOINED = f"Do not wait, must use {MODEL}"
# Natural single-stage refusals (preconditions asserted below with the unchanged recognizer).
RAW_ONLY = f"*standing: dont wait, {MODEL}* required"            # markdown pairing joins only the raw line
RAW_ONLY_WORDS = f"dont wait, {MODEL}* required"
WORDS_ONLY = f"standing: independent {MODEL} review, dont wait"   # bare named-review fullmatch on words
WORDS_ONLY_WORDS = f"independent {MODEL} review, dont wait"
COLLAPSED_ONLY = f"standing: dont wait, must use exact  head {MODEL}"  # double space defeats the qualifier
COLLAPSED_ONLY_WORDS = f"dont wait, must use exact  head {MODEL}"


def _refused(text: str) -> bool:
    try:
        validate_text(text)
    except ValueError:
        return True
    return False


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    def card(body: str = "", comments: tuple[str, ...] = ()) -> str:
        conn = kbc.connect()
        try:
            tid = kb.create_task(conn, title="migrate launchers", body=body or None, assignee="test-worker")
            kb.claim_task(conn, tid)
            run_id = kb._current_run_id(conn, tid)
            for text in comments:  # stored history: comments carry no ingress text policy
                kb.add_comment(conn, tid, "coordinator", text)
        finally:
            conn.close()
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        return tid

    return card


def _context(tid: str) -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return kb.build_worker_context(conn, tid)
    finally:
        conn.close()


def _standing(ctx: str) -> list[str]:
    """The generated standing section's emitted lines, not raw body/comment text shown elsewhere."""
    if "## Standing instructions" not in ctx:
        return []
    section = ctx.split("## Standing instructions", 1)[1].split("(These still govern", 1)[0]
    return [line for line in section.split("\n")[1:] if line.startswith("standing: ")]


def _block(tid: str):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_block(dict(BLOCK)))
    conn = kbc.connect()
    try:
        status = kb.get_task(conn, tid).status
        holds = [e.payload for e in kb.list_events(conn, tid) if e.kind == "standing_wait_hold"]
    finally:
        conn.close()
    return out, status, holds


def _source(tid: str):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return kb.get_task(conn, tid).body, [c.body for c in kb.list_comments(conn, tid)]
    finally:
        conn.close()


def _payload(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


# --- recognizer preconditions: each case refuses at exactly the stage it names ------------------------

def test_counterexample_preconditions_use_unchanged_recognizer():
    assert not _refused(f"standing: {SUFFIX_WORDS}") and not _refused(SUFFIX_WORDS)
    assert len(SUFFIX_TRUNC) == 400 and SUFFIX_TRUNC.endswith(MODEL) and _refused(SUFFIX_TRUNC)
    assert not _refused(CR_TEXT) and _refused(JOINED)
    assert _refused(RAW_ONLY) and not _refused(RAW_ONLY_WORDS)
    assert not _refused(WORDS_ONLY) and _refused(WORDS_ONLY_WORDS)
    assert not _refused(COLLAPSED_ONLY) and not _refused(COLLAPSED_ONLY_WORDS)
    assert _refused(" ".join(COLLAPSED_ONLY_WORDS.split()))


def test_default_helpers_remain_behavior_identical():
    """Baseline control: the compactor/default extraction still sees the legacy view."""
    assert si.explicit_lines(CR_TEXT) == [JOINED]
    assert si.explicit_lines(f"standing: {SUFFIX_WORDS}") == [SUFFIX_TRUNC]
    assert si.explicit_lines(RAW_ONLY) == [RAW_ONLY_WORDS]
    assert si.extract(CR_TEXT, user_authored=True)[0] == JOINED
    assert si.dedupe(["a", "A.", "b"]) == ["a", "b"]
    assert si.render_section([JOINED]).count(f"standing: {JOINED}") == 1


# --- restricted helper contract --------------------------------------------------------------------

def test_restricted_helper_passes_raw_line_with_every_representation():
    seen = []
    out = si.restricted_explicit_lines("> standing:  keep   going \nother", lambda raw, c: seen.append((raw, c)) or True)
    assert out == [("> standing:  keep   going ", "keep going")]
    assert [c for _, c in seen] == ["keep   going", "keep going", "keep going", "standing: keep going"]
    assert {raw for raw, _ in seen} == {"> standing:  keep   going "}


def test_injected_rendered_line_refusal_drops_candidate():
    """INJECTED keep double: refuses only the rendered ``standing:`` form to prove it is checked."""
    out = si.restricted_explicit_lines("standing: keep going", lambda raw, c: not c.startswith("standing: "))
    assert out == []


# --- S1 worker context through real create -> build_worker_context ----------------------------------

def test_suffix_truncation_is_not_promoted_to_worker_context(board):
    tid = board(body=f"standing: {SUFFIX_WORDS}")  # ingress accepts the raw line
    ctx = _context(tid)
    assert f"standing: {SUFFIX_TRUNC}" not in _standing(ctx)
    assert "## Standing instructions" not in ctx
    assert SUFFIX_WORDS in ctx  # raw body stays visible, unfiltered
    assert _source(tid)[0] == f"standing: {SUFFIX_WORDS}"


@pytest.mark.parametrize("sep", SEPARATORS, ids=[repr(s) for s in SEPARATORS])
def test_no_candidate_crosses_any_splitlines_boundary(board, sep):
    text = f"standing: Do not wait, must use{sep}{MODEL}"
    control = f"standing: keep going{sep}standing: no check-ins"
    tid = board(body=text, comments=(control,))
    emitted = _standing(_context(tid))
    assert f"standing: {JOINED}" not in emitted
    assert all(MODEL not in line for line in emitted)
    assert emitted == ["standing: Do not wait, must use", "standing: keep going", "standing: no check-ins"]
    assert _source(tid) == (text, [control])


@pytest.mark.parametrize("raw", [RAW_ONLY, WORDS_ONLY, COLLAPSED_ONLY, f"standing: {SUFFIX_WORDS}"],
                         ids=["raw-only", "words-only", "collapsed-only", "truncated-only"])
def test_each_refusal_stage_is_dropped_from_context(board, raw):
    tid = board(comments=(raw, "standing: keep the PR small"))
    emitted = _standing(_context(tid))
    # The refused candidate is dropped entirely (not merely re-rendered into a passing form).
    assert emitted == ["standing: keep the PR small"]
    assert _source(tid)[1] == [raw, "standing: keep the PR small"]


def test_refused_candidates_never_evict_a_permitted_line(board):
    refused = tuple(f"*standing: note {i} {MODEL}* required" for i in range(21))
    assert all(_refused(r) and not _refused(r.split(": ", 1)[1]) for r in refused)
    tid = board(comments=("standing: keep the PR small", *refused, "standing: no check-ins"))
    assert _standing(_context(tid)) == ["standing: keep the PR small", "standing: no check-ins"]


def test_limit_semantics_unchanged_for_permitted_lines(board):
    """Control: newest-20 limit over permitted candidates is the existing dedupe contract."""
    tid = board(comments=tuple(f"standing: keep item {i}" for i in range(25)))
    assert _standing(_context(tid)) == [f"standing: keep item {i}" for i in range(5, 25)]


def test_dedupe_keys_never_change_emitted_bytes_and_follow_filtering(board):
    permitted_twin = RAW_ONLY.lstrip("*")  # same words, raw line without the refusing markdown pair
    assert not _refused(permitted_twin)
    tid = board(body="standing: Keep item 3.", comments=("standing: keep item 3", RAW_ONLY, permitted_twin))
    # The body spelling is emitted unchanged once; the refused raw line does not hold the dedupe key.
    assert _standing(_context(tid)) == ["standing: Keep item 3.", f"standing: {RAW_ONLY_WORDS}"]


# --- S2 block hold through real kanban_block ----------------------------------------------------------

def test_suffix_truncation_does_not_hold_block(board):
    tid = board(body=f"standing: {SUFFIX_WORDS}")
    out, status, holds = _block(tid)
    assert "error" not in out, out
    assert status == "blocked" and holds == []


def test_lone_cr_holds_only_the_physical_line(board):
    tid = board(comments=(CR_TEXT,))
    out, status, holds = _block(tid)
    blob = json.dumps(out)
    assert "error" in out and status == "running"
    assert MODEL not in blob
    assert [_payload(h)["standing"] for h in holds] == [["Do not wait, must use"]]
    assert _source(tid)[1] == [CR_TEXT]


@pytest.mark.parametrize("raw", [RAW_ONLY, WORDS_ONLY, COLLAPSED_ONLY], ids=["raw-only", "words-only", "collapsed"])
def test_refused_dont_wait_stage_lands_block_without_event(board, raw):
    tid = board(comments=(raw,))
    out, status, holds = _block(tid)
    assert "error" not in out, out
    assert status == "blocked" and holds == []


def test_first_three_selected_after_refusal(board):
    refused = tuple(f"*standing: dont wait {i}, {MODEL}* required" for i in range(3))
    safe = ("standing: dont wait on me", "standing: never stop to ask", "standing: no check-ins",
            "standing: do not pause")
    tid = board(comments=(*refused, *safe))
    out, status, holds = _block(tid)
    assert status == "running" and len(holds) == 1
    words = ["dont wait on me", "never stop to ask", "no check-ins"]
    assert _payload(holds[0])["standing"] == words
    assert '"dont wait on me"; "never stop to ask"; "no check-ins"' in out["error"]
    assert MODEL not in json.dumps(out)
    again, status, holds = _block(tid)  # existing once-per-run semantics: second call lands
    assert "error" not in again and status == "blocked" and len(holds) == 1


def test_quoted_join_keeps_separators_between_checked_lines(board):
    tid = board(comments=("standing: dont wait, must use", f"standing: {MODEL} dont wait"))
    out, status, holds = _block(tid)
    assert status == "running"
    assert _payload(holds[0])["standing"] == ["dont wait, must use", f"{MODEL} dont wait"]
    message = out["error"]
    assert f'"dont wait, must use"; "{MODEL} dont wait"' in message
    assert f"must use {MODEL}" not in message


def test_injected_emitted_message_refusal_skips_candidate(board, monkeypatch):
    """INJECTED: a hold message that would refuse once a word is quoted drops that word before the event."""
    from tools import kanban_tools as kt
    real = kt._standing_hold_message
    monkeypatch.setattr(kt, "_standing_hold_message",
                        lambda words: real(words) + (f" must use {MODEL}" if "dont wait, bad" in words else ""))
    tid = board(comments=("standing: dont wait, bad", "standing: dont wait, good"))
    out, status, holds = _block(tid)
    assert status == "running"
    assert [_payload(h)["standing"] for h in holds] == [["dont wait, good"]]
    assert "bad" not in out["error"] and MODEL not in out["error"]


def test_ordinary_standing_dont_wait_still_holds(board):
    tid = board(body="standing: dont block, dont wait")
    out, status, holds = _block(tid)
    assert "error" in out and status == "running"
    assert [_payload(h)["standing"] for h in holds] == [["dont block, dont wait"]]
