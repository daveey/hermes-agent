"""Chat-wait notices name the pool workers that could run a home-waiting card. [daveey]

Defect kanban-e7a33105a4 (hermes-hub, 2026-11-10 23:53Z): a chat told David its build cards "start as hub worker
slots free up" while worker-1 could run them. The only queue facts it saw were the chat-wait notice ("16 card(s)
ahead of it, 7/7 worker slots busy"): ``kanban_queue_position.position`` had computed ``pool_could_run`` and
``local_words`` (kanban_show) printed the re-place command, but ``kanban_parent_unclaimed._queue_words``, which
both chat notices and ``ready_hint`` use, dropped it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kn
from hermes_cli import kanban_parent_unclaimed as pu
from hermes_cli import kanban_queue_position as kqp

_ENV = ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_HOME", "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_KANBAN_WORKER_TASK")
_PLACE = {"lane": "local", "ahead": 16, "running": 7, "limit": 7, "approximate": True, "board": "default"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: 7)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return h


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch):
    """worker-2 carries the card's labels and has a free slot."""
    monkeypatch.setattr(kqp, "_pool_could_run", lambda conn, task_id: ["worker-2"])


def _waiting_card(conn, *, age_s: int = 40 * 60) -> str:
    now = int(time.time())
    tid = kb.create_task(conn, title="waiting", assignee="default")
    kn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="origin-chat", delivery_metadata={"reason": "create"})
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (now - age_s, tid))
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (now - age_s, tid))
    conn.commit()
    return tid


def _notice(conn, tid: str, kind: str) -> dict:
    (payload,) = [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind))]
    return payload


def _assert_pool_hint(text: str, tid: str) -> None:
    assert "worker-2" in text
    assert f"hermes kanban move {tid} --runs-on machine:worker-2" in text


def test_pool_sentence_comes_from_the_shared_helper_without_repeated_advice():
    place = {**_PLACE, "pool_could_run": ["worker-2", "worker-1"], "task_id": "t_x"}
    sentence = pu._pool_sentence(place)
    assert sentence == kqp.pool_words(place, advice=False)
    assert sentence in kqp.local_words(place)  # one wording for kanban_show and the chat notices
    assert "outside the dispatcher" in kqp.local_words(place)  # kanban_show has no advice after it
    assert "outside the dispatcher" not in sentence  # the notices' own advice follows it
    _assert_pool_hint(sentence, "t_x")
    assert "worker-2, worker-1" in sentence
    assert pu._queue_words(place) == "16 card(s) ahead of it, 7/7 worker slots busy"  # a bare queue clause
    assert pu._pool_sentence(dict(_PLACE)) == ""


def test_kanban_show_keeps_the_pool_hint_under_the_admission_census():
    """PR #1821 review: ``local_words`` returned from its ``admission_pools`` branch before ``pool_could_run``."""
    pools = {"regular": 7, "regular_cap": 7, "priority": 0, "priority_cap": 2}
    place = {**_PLACE, "admission_pools": pools, "admission_class": "normal", "pool_could_run": ["worker-2"],
             "task_id": "t_x"}
    words = kqp.local_words(place)
    assert "regular 7/7" in words
    _assert_pool_hint(words, "t_x")
    assert words.count("outside the dispatcher") == 1, words
    skipped = kqp.local_words({**place, "skip": {"reason": "per_profile_capped"}})
    assert skipped.startswith("last dispatcher skip of this ready episode: ")
    _assert_pool_hint(skipped, "t_x")
    unpooled = kqp.local_words({**place, "pool_could_run": None})
    assert "--runs-on" not in unpooled and unpooled.endswith("Do not run its work outside the dispatcher")


def test_kanban_show_keeps_the_recorded_skip_with_a_pool_hint_without_the_census():
    """PR #1829 review: without ``admission_pools``, ``local_words`` returned the pool hint before rendering the
    recorded skip, so the skip reason vanished there while the census branch kept it. One shape for both: the skip
    framing first, then the skip's own advice after the pool re-place (PR #1835 review: the notices' rule)."""
    skip = {"reason": "owner_down", "detail": "default:inactive"}
    place = {**_PLACE, "skip": skip, "pool_could_run": ["worker-2"], "task_id": "t_x"}
    from hermes_cli import kanban_dispatch_skip as skips
    tail = kqp.pool_words(place, advice=False) + ". " + skips.sentence_advice(skip)
    words = kqp.local_words(place)
    assert words.startswith("last dispatcher skip of this ready episode: " + skips.words(skip)), words
    assert "(waiting for a local slot: ~16 ahead, 7/7 running)" in words, words
    assert words.endswith(tail), words  # the re-place, then the skip's advice; the warning said once
    _assert_pool_hint(words, "t_x")
    pools = {"regular": 7, "regular_cap": 7, "priority": 0, "priority_cap": 2}
    census = kqp.local_words({**place, "admission_pools": pools, "admission_class": "normal"})
    assert census.startswith("last dispatcher skip of this ready episode: " + skips.words(skip)), census
    assert census.endswith(tail), census  # same shape on both branches
    unskipped = kqp.local_words({**place, "skip": None})
    assert unskipped.startswith("waiting for a local slot") and unskipped.endswith(kqp.pool_words(place))
    unpooled = kqp.local_words({**place, "pool_could_run": None})
    assert unpooled.endswith(skips.sentence_advice(skip)) and "--runs-on" not in unpooled


_UNRECORDED_CLAUSE = "before changing anything"


def test_options_defer_to_the_pool_hint_when_the_skip_is_unrecorded():
    """PR #1829 review: after "Re-place it ...", the unrecorded-skip advice said "Read the live verdict ... before
    changing anything; priority changes are not the fix". With a pool hint that clause is dropped."""
    unrecorded = {"skip_unrecorded": True}
    plain = pu._options("t_x", None, unrecorded)
    assert _UNRECORDED_CLAUSE in plain and "priority changes are not the fix" in plain
    pooled = pu._options("t_x", None, {**unrecorded, "pool_hint": "Home-only ... Re-place it"})
    assert _UNRECORDED_CLAUSE not in pooled and "priority changes are not the fix" not in pooled
    assert "dispatcher skip reason unrecorded" in pooled and pooled.endswith("outside the dispatcher")
    assert pu._options("t_x", None, {**unrecorded, "pool_hint": "  "}) == plain  # a blank hint is no hint
    assert pu._options("t_x", None, {"pool_hint": "Re-place it"}) == pu._options("t_x", None, {})  # no skip fact
    recorded = {"dispatch_skip": {"reason": "pinned_quota_wall", "detail": "x"}, "pool_hint": "Re-place it"}
    assert pu._options("t_x", None, recorded) == pu._options("t_x", None, {"dispatch_skip": recorded["dispatch_skip"]})


def test_ready_hint_defers_the_unrecorded_advice_to_the_pool_hint(home, pool, monkeypatch):
    """The ready-wait hint carries the pool hint into ``_options`` (its payload has no ``pool_hint`` otherwise)."""
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: 9)  # free local slots: the skip is unrecorded
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        hint = pu.ready_hint(conn, kb.get_task(conn, tid))
    assert hint is not None
    _assert_pool_hint(hint, tid)
    assert "dispatcher skip reason unrecorded" in hint, hint
    assert _UNRECORDED_CLAUSE not in hint, hint
    assert hint.count("outside the dispatcher") == 1, hint


def test_ready_hint_without_pool_keeps_the_unrecorded_advice(home, monkeypatch):
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: 9)
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        hint = pu.ready_hint(conn, kb.get_task(conn, tid))
    assert hint is not None and _UNRECORDED_CLAUSE in hint, hint


_CENSUS = {"admission_pools": {"regular": 7, "regular_cap": 7, "priority": 0, "priority_cap": 2},
           "admission_class": "normal"}


def _record_skip(conn, tid: str, reason: str, detail: str, until=None) -> None:
    from hermes_cli import kanban_dispatch_skip as skips
    assert skips.record(conn, tid, reason, detail=detail, until=until)


def test_recorded_skip_with_a_pool_hint_keeps_both_on_every_surface(home, pool):
    """PR #1835 review: for a recorded non-ordering skip plus a pool hint, ``kanban show`` replaced the skip advice
    with the re-place while the chat notices and ``ready_hint`` kept both. One rule now: a skip whose advice does not
    contradict a re-place keeps it after the pool sentence, on every surface, and the dispatcher warning is said
    once."""
    from hermes_cli import kanban_dispatch_skip as skips
    skip = {"reason": "owner_down", "detail": "default:inactive"}
    tail = skips.advice(skip)
    place = {**_PLACE, "skip": skip, "pool_could_run": ["worker-2"], "task_id": "t_x"}
    for shown in (kqp.local_words(place), kqp.local_words({**place, **_CENSUS})):
        assert shown.startswith("last dispatcher skip of this ready episode: " + skips.words(skip)), shown
        assert kqp.pool_words(place, advice=False) + ". " + skips.sentence_advice(skip) in shown, shown
        assert shown.endswith(skips.sentence_advice(skip)) and shown.count("outside the dispatcher") == 1, shown
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        _record_skip(conn, tid, "owner_down", "default:inactive")
        hint = pu.ready_hint(conn, kb.get_task(conn, tid))
        assert cw.sweep(conn) == 1
        notice = cw.notice_text(_notice(conn, tid, cw.NOTICE_KIND))
    assert hint is not None
    for text in (hint, notice):
        _assert_pool_hint(text, tid)
        assert text.endswith(tail + "."), text
        assert text.count("outside the dispatcher") == 1, text


def test_pinned_quota_wall_skip_keeps_its_advice_and_drops_the_pool_re_place(home, pool):
    """Historical wall receipts suppress relocation until expiry; advice must not recreate a pin."""
    from hermes_cli import kanban_dispatch_skip as skips
    skip = {"reason": skips.PINNED_QUOTA_WALL, "detail": "claude-opus-5-5 (anthropic)"}
    place = {**_PLACE, "skip": skip, "pool_could_run": ["worker-2"], "task_id": "t_x"}
    for shown in (kqp.local_words(place), kqp.local_words({**place, **_CENSUS})):
        assert shown.startswith("last dispatcher skip of this ready episode: " + skips.words(skip)), shown
        assert shown.endswith(skips.sentence_advice(skip)), shown
        assert "--runs-on" not in shown and "pool worker(s)" not in shown, shown
    assert pu._pool_sentence(place) == ""
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        _record_skip(conn, tid, skips.PINNED_QUOTA_WALL, "claude-opus-5-5 (anthropic)", until=time.time() + 3600)
        hint = pu.ready_hint(conn, kb.get_task(conn, tid))
        assert cw.sweep(conn) == 1
        payload = _notice(conn, tid, cw.NOTICE_KIND)
    assert "pool_hint" not in payload and hint is not None
    for text in (hint, cw.notice_text(payload)):
        assert "--runs-on" not in text and "pool worker(s)" not in text, text
        assert "starts on its own when an eligible class route becomes available" in text, text
        assert "set-model" not in text and "re-pin" not in text, text


def test_unrecorded_advice_variants_share_one_fact_sentence():
    """PR #1835 review: ``UNRECORDED_POOL_ADVICE`` repeated ``UNRECORDED_ADVICE``'s fact sentence verbatim."""
    from hermes_cli import kanban_dispatch_skip as skips
    assert skips.UNRECORDED_FACT.startswith(skips.UNRECORDED + ": ")
    for text in (skips.UNRECORDED_ADVICE, skips.UNRECORDED_POOL_ADVICE):
        assert text.startswith(skips.UNRECORDED_FACT + " "), text
        assert text.endswith("Do not run its work outside the dispatcher"), text
    assert skips.UNRECORDED_POOL_ADVICE == skips.UNRECORDED_FACT + " Do not run its work outside the dispatcher"


def _assert_pool_sentence_outside_parens(text: str) -> None:
    """The multi-sentence pool hint follows the parenthesised queue clause, and the dispatcher warning is said once
    (by the advice that ends the notice). PR #1821 review nit."""
    start = text.index("with no worker (") + len("with no worker (")
    depth, end = 1, start
    for end in range(start, len(text)):
        depth += {"(": 1, ")": -1}.get(text[end], 0)
        if depth == 0:
            break
    assert "pool worker(s)" not in text[start:end], text
    assert "pool worker(s)" in text[end:], text
    assert text.count("outside the dispatcher") == 1, text


def test_queue_words_without_pool_are_unchanged():
    assert pu._queue_words(dict(_PLACE)) == "16 card(s) ahead of it, 7/7 worker slots busy"
    assert pu._queue_words({**_PLACE, "pool_could_run": []}) == "16 card(s) ahead of it, 7/7 worker slots busy"


def test_direct_chat_wait_notice_names_the_pool_worker(home, pool):
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        assert cw.sweep(conn) == 1
        payload = _notice(conn, tid, cw.NOTICE_KIND)
    _assert_pool_hint(cw.notice_text(payload), tid)
    _assert_pool_sentence_outside_parens(cw.notice_text(payload))


def test_parent_unclaimed_notice_names_the_pool_worker(home, pool):
    with kbc.connect_closing() as conn:
        parent = _waiting_card(conn)
        child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
        kn.add_notify_sub(conn, task_id=child, platform="tui", chat_id="origin-chat",
                          delivery_metadata={"reason": "create"})
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'dependency_wait', "
                     "'{}', ?)", (child, int(time.time()) - 40 * 60))
        conn.commit()
        assert pu.sweep(conn) == 1
        payload = _notice(conn, child, pu.NOTICE_KIND)
    _assert_pool_hint(pu.notice_text(payload), parent)
    _assert_pool_sentence_outside_parens(pu.notice_text(payload))


def test_ready_hint_names_the_pool_worker(home, pool):
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        hint = pu.ready_hint(conn, kb.get_task(conn, tid))
    assert hint is not None
    _assert_pool_hint(hint, tid)
    assert hint.count("outside the dispatcher") == 1, hint


def test_notices_without_pool_name_no_worker(home):
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        assert cw.sweep(conn) == 1
        text = cw.notice_text(_notice(conn, tid, cw.NOTICE_KIND))
    assert "--runs-on machine:" not in text and "pool worker(s)" not in text


@pytest.mark.parametrize("reason,detail,starts", [
    ("owner_down", "default:inactive", "Resolve that dispatcher skip reason; "),
    ("pinned_quota_wall", "claude-opus-5-5 (anthropic)", "It starts on its own when an eligible class route becomes available; "),
])
@pytest.mark.parametrize("pooled", [True, False])
@pytest.mark.parametrize("census", [True, False])
def test_kanban_show_sentence_cases_the_skip_advice_after_a_full_stop(reason, detail, starts, pooled, census):
    """PR #1846 review nit: ``local_words`` joins the preceding sentence and ``advice(skip)`` with '. ', and
    ``advice`` starts lowercase, so ``kanban show`` read '... instead of waiting for a local slot. resolve that ...'
    (and '... 7/7 running). it starts on its own ...' without a pool). The notices keep the lowercase clause after
    their ``NO_FIX: `` prefix."""
    until = time.time() + 3600 if reason == "pinned_quota_wall" else None
    skip = {"reason": reason, "detail": detail, "until": until}
    place = {**_PLACE, "skip": skip, "pool_could_run": ["worker-2"] if pooled else None, "task_id": "t_x",
             **(_CENSUS if census else {})}
    shown = kqp.local_words(place)
    assert ". " + starts in shown, shown
    assert ". resolve that" not in shown and ". it starts" not in shown, shown
    from hermes_cli import kanban_dispatch_skip as skips
    assert skips.advice(skip)[0].islower()  # the notices' clause after "NO_FIX: " is unchanged


@pytest.mark.parametrize("until,helps", [
    (-60, True),  # the reset passed and no later tick replaced the skip yet: a pool re-place may run it now
    (3600, False),
    (None, False),  # no reset recorded: keep suppressing (conservative)
    ("soon", False),  # unparseable: keep suppressing
])
def test_pool_replace_is_suppressed_only_while_the_quota_wall_lasts(until, helps):
    """PR #1846 review nit: ``pool_replace_helps`` dropped the pool re-place for any ``pinned_quota_wall`` skip
    returned by ``skips.latest``, including one whose ``until`` had already passed."""
    from hermes_cli import kanban_dispatch_skip as skips
    now = 1_800_000_000.0
    at = now + until if isinstance(until, int) else until
    skip = {"reason": skips.PINNED_QUOTA_WALL, "detail": "claude-opus-5-5 (anthropic)", "until": at}
    assert skips.pool_replace_helps(skip, now=now) is helps
    assert skips.pool_replace_helps({**skip, "reason": "owner_down"}, now=now) is True
    assert skips.pool_replace_helps(None, now=now) is True


def test_an_expired_quota_wall_skip_offers_the_pool_re_place_on_every_surface(home, pool):
    """The default clock is ``time.time()``: an expired wall re-offers the re-place in ``kanban show`` and notices."""
    from hermes_cli import kanban_dispatch_skip as skips
    skip = {"reason": skips.PINNED_QUOTA_WALL, "detail": "claude-opus-5-5 (anthropic)", "until": time.time() - 60}
    place = {**_PLACE, "skip": skip, "pool_could_run": ["worker-2"], "task_id": "t_x"}
    for shown in (kqp.local_words(place), kqp.local_words({**place, **_CENSUS})):
        _assert_pool_hint(shown, "t_x")
    assert pu._pool_sentence(place)
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        _record_skip(conn, tid, skips.PINNED_QUOTA_WALL, "claude-opus-5-5 (anthropic)", until=time.time() - 60)
        hint = pu.ready_hint(conn, kb.get_task(conn, tid))
    assert hint is not None
    _assert_pool_hint(hint, tid)


_WALL_RESETS = "starts on its own when an eligible class route becomes available"


@pytest.mark.parametrize("census", [True, False])
@pytest.mark.parametrize("offset", [-60, 0])  # the gate keeps the wall only while until > now: until == now passed
def test_kanban_show_marks_an_expired_quota_wall_reset_passed(census, offset):
    """PR #1865 review nit 2: once a quota wall's ``until`` has passed (no later tick replaced the skip),
    ``kanban show`` offers the pool re-place, so the same text must not still say the card "starts on its own when
    that wall resets". Both halves read one ``now``."""
    from hermes_cli import kanban_dispatch_skip as skips
    now = 1_800_000_000.0
    skip = {"reason": skips.PINNED_QUOTA_WALL, "detail": "claude-opus-5-5 (anthropic)", "until": now + offset}
    place = {**_PLACE, "skip": skip, "pool_could_run": ["worker-2"], "task_id": "t_x", **(_CENSUS if census else {})}
    shown = kqp.local_words(place, now=now)
    _assert_pool_hint(shown, "t_x")
    assert _WALL_RESETS not in shown, shown
    assert "(reset passed)" in shown, shown
    assert shown.count("outside the dispatcher") == 1, shown


@pytest.mark.parametrize("until", [3600, None, "soon"])
def test_a_lasting_or_unreadable_quota_wall_keeps_its_wording(until):
    """A future wall, or one with a missing/unparseable ``until``, keeps "starts on its own" and no marker."""
    from hermes_cli import kanban_dispatch_skip as skips
    now = 1_800_000_000.0
    at = now + until if isinstance(until, int) else until
    skip = {"reason": skips.PINNED_QUOTA_WALL, "detail": "claude-opus-5-5 (anthropic)", "until": at}
    place = {**_PLACE, "skip": skip, "pool_could_run": ["worker-2"], "task_id": "t_x"}
    shown = kqp.local_words(place, now=now)
    assert _WALL_RESETS in shown and "(reset passed)" not in shown and "--runs-on" not in shown, shown
    assert _WALL_RESETS in skips.advice(skip, now=now)
    assert "(reset passed)" not in skips.words(skip, now=now)


def test_an_expired_quota_wall_drops_the_reset_clause_on_every_surface(home, pool):
    """The notices and ``ready_hint`` follow ``kanban show``: with the re-place offered, no "starts on its own"."""
    from hermes_cli import kanban_dispatch_skip as skips
    skip = {"reason": skips.PINNED_QUOTA_WALL, "detail": "claude-opus-5-5 (anthropic)", "until": time.time() - 60}
    shown = kqp.local_words({**_PLACE, "skip": skip, "pool_could_run": ["worker-2"], "task_id": "t_x"})
    assert _WALL_RESETS not in shown and "(reset passed)" in shown, shown
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        _record_skip(conn, tid, skips.PINNED_QUOTA_WALL, "claude-opus-5-5 (anthropic)", until=time.time() - 60)
        hint = pu.ready_hint(conn, kb.get_task(conn, tid))
        assert cw.sweep(conn) == 1
        notice = cw.notice_text(_notice(conn, tid, cw.NOTICE_KIND))
    assert hint is not None
    for text in (hint, notice):
        _assert_pool_hint(text, tid)
        assert _WALL_RESETS not in text and "(reset passed)" in text, text
        assert text.count("outside the dispatcher") == 1, text
