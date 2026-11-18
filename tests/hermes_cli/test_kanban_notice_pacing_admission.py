"""A stall notice for a pool card no worker imported names the LLM pacing hold this gateway reads. [daveey]

Defect t_01a1db9d9538716b (hub, 2026-11-14): ``chat_wait_unclaimed`` for a ready ``pool:worker`` card said
"admission reason unknown" and "re-place on a capable gateway" while every pool worker's dispatcher was held by the
LLM pacing governor (anthropic paced, openai-codex held until 11-20 22:40Z). Workers import other gateways' cards in
priority order and this one ranked 62 of 66, so no dispatcher able to lease it ever recorded a verdict; the only skip
on it was the hub's own ``skipped_ineligible: labels_mismatch`` ("this machine cannot lease it"). The hub's own
governor file (``llm_pacing.json``) covers the same accounts, because pool workers draw theirs from the hub's relay.

Only a positive read of that file (every candidate provider paced or held) changes the notice. An absent, stale,
unreadable, failing or OPEN read leaves today's payload and words, "admission reason unknown" included.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import time

import pytest

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_dispatch_skip as skips
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli import kanban_parent_unclaimed as pu
from hermes_cli import kanban_queue_position as kqp
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import _placement_helpers as helpers
from tests.hermes_cli import test_kanban_label_backlog_wait as pool_wait
from tests.hermes_cli import test_kanban_model_tier as model_tier

hub = pool_wait.hub  # a hub that cannot lease ``pool:worker`` cards: the Board registry names two pool workers
board = model_tier.board  # one unsynced gateway: every card is its own to lease


@pytest.fixture(autouse=True)
def _pace_starts_mode(monkeypatch):
    """This file describes the opt-in mode (``kanban.llm_pacing.pace_starts: true``), where a paced pool holds
    starts. The default, where it holds nothing and is no hold to describe, is in
    ``test_kanban_pacing_default_reads.py``."""
    real = lp.config_from
    monkeypatch.setattr(lp, "config_from", lambda section: dataclasses.replace(real(section), pace_starts=True))
LABELS = pool_wait.LABELS
OPUS = "claude-opus-5-5"
DAY = 86400
UNKNOWN = {"reason": None, "status": "unknown"}
READ_INTERVAL = lp.PacingConfig().read_interval
# Verdicts a dispatcher able to lease the card records: the notice never talks over them.
CAPABLE_VERDICTS = [("claim_deferred", "worker_capacity_full"), ("claim_deferred", "lease_held"),
                    ("skipped_ineligible", "waiting_for_worker_capacity"), ("llm_paced", "anthropic paced"),
                    ("owner_down", "offline")]  # last: a reason outside the eligibility pair, with one of its details
# Skips that only say the RECORDING machine cannot lease the card (its own eligibility check, or the Board's).
CANNOT_LEASE = [(reason, detail) for reason in ("skipped_ineligible", "claim_deferred")
                for detail in ("labels_mismatch", "pinned_only", "offline", "held")]


def _store(*pools, age=0.0):
    """This gateway's own governor file as its dispatcher leaves it: ``(provider, state[, until])`` per pool."""
    now = time.time()
    data = {}
    for provider, state, *until in pools:
        pace = lp.PoolPace(provider, state, 0.0299 if state == lp.PACED else 0.0, until[0] if until else None,
                           "6 of 7 accounts at 100% on the 7-day window")
        data[provider] = {"read_at": now - age, "pace": pace.as_dict(), "running": [], "samples": []}
    pd.state_path().write_text(json.dumps(data), encoding="utf-8")


def _pool_card(conn, *, age=40 * 60, labels=None, title="waits on the pool", **kw):
    """A ready card pinned to the pool's labels and to anthropic, owned by a chat, ready for ``age`` seconds, with
    two older same-label cards ahead of it."""
    now = int(time.time())
    for i in range(2):
        pool_wait._card(conn, f"older {i}", now - 3600 + i)
    kw.setdefault("model_override", OPUS)
    kw.setdefault("provider_override", "anthropic")
    tid = kb.create_task(conn, title=title, assignee="default", runs_on=list(labels or LABELS),
                         session_id="owner-chat", **kw)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - age, tid))
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'created'", (now - age, tid))
    return tid


def _events(conn, tid, kind):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind))]


def _chat_wait(conn, tid):
    """The ``chat_wait_unclaimed`` event the dispatcher tick's sweep appends for ``tid``, and its chat line."""
    assert cw.sweep(conn) == 1
    (payload,) = _events(conn, tid, cw.NOTICE_KIND)
    return payload, cw.notice_text(payload)


def _parent_unclaimed(conn, parent):
    """The ``dependency_parent_unclaimed`` event the same tick appends to a chat-owned child of ``parent``."""
    child = kb.create_task(conn, title="child", assignee="default", parents=[parent], session_id="owner-chat")
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'dependency_wait', "
                     "'{}', ?)", (child, int(time.time()) - 40 * 60))
    assert pu.sweep(conn) == 1
    (payload,) = _events(conn, child, pu.NOTICE_KIND)
    return payload, pu.notice_text(payload)


NOTICES = {"chat_wait": _chat_wait, "parent_unclaimed": _parent_unclaimed}


def _hub_card(conn):
    """The live shape: only the hub's own ``skipped_ineligible: labels_mismatch``, written 5 s after creation."""
    tid = _pool_card(conn)
    assert skips.record(conn, tid, "skipped_ineligible", detail="labels_mismatch")
    return tid


def _post(conn, tid, **extra):
    """One ``chat_wait_unclaimed`` notice for ``tid`` posted directly, with the sweep's ``extra`` fields."""
    assert pu.post_notice(conn, tid, tid, wait_key="ready@1", waited_seconds=2400, ready_seconds=2400,
                          kind=cw.NOTICE_KIND, extra=extra or None)
    (payload,) = _events(conn, tid, cw.NOTICE_KIND)
    return payload


def _assert_names_no_re_placement(text):
    assert "admission reason unknown" not in text and "re-place on a capable gateway" not in text, text
    assert "re-place" not in text and "bump" not in text, text


def _assert_queue_words_as_before(payload, text, render):
    """Out of scope and pinned elsewhere: the queue count and the lease clause read exactly as the same notice with
    today's "unknown" admission reads them."""
    before = render({**payload, "admission": UNKNOWN})
    assert text.split(" [board")[0] == before.split(" [board")[0], (text, before)
    assert "this machine cannot lease it" in text, text


# --- card cases 1 and 2: a paced pool is named, by both notices -----------------------------------------------------


def test_chat_wait_notice_for_a_never_imported_pool_card_names_the_paced_pool(hub):
    _store(("anthropic", lp.PACED))
    tid = _hub_card(hub.conn)
    payload, text = _chat_wait(hub.conn, tid)
    assert payload["admission"] == {"reason": "llm_pacing", "status": "held",
                                    "providers": [{"provider": "anthropic", "state": "paced"}]}, payload
    assert "admission reason llm_pacing: held: LLM quota pacing (anthropic paced pool)" in text, text
    assert "it starts on its own once its provider pool is back on pace" in text, text
    _assert_names_no_re_placement(text)
    _assert_queue_words_as_before(payload, text, cw.notice_text)
    assert "2 card(s) with matching labels ahead of it" in text, text
    # Not suppressed, and still once per ready episode.
    assert cw.sweep(hub.conn) == 0 and len(_events(hub.conn, tid, cw.NOTICE_KIND)) == 1


def test_parent_unclaimed_notice_for_a_never_imported_pool_card_names_the_paced_pool(hub):
    _store(("anthropic", lp.PACED))
    parent = _hub_card(hub.conn)
    payload, text = _parent_unclaimed(hub.conn, parent)
    assert payload["admission"] == {"reason": "llm_pacing", "status": "held",
                                    "providers": [{"provider": "anthropic", "state": "paced"}]}, payload
    assert "admission reason llm_pacing: held: LLM quota pacing (anthropic paced pool)" in text, text
    assert "it starts on its own once its provider pool is back on pace" in text, text
    _assert_names_no_re_placement(text)
    _assert_queue_words_as_before(payload, text, pu.notice_text)
    assert pu.sweep(hub.conn) == 0  # still once per wait


# --- card case 3: a held pool carries its until time ----------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(NOTICES))
def test_held_pool_notice_carries_the_until_time(hub, path):
    until = int(time.time()) + 5 * DAY
    _store(("anthropic", lp.HELD, until + 0.7))  # the governor stores a float; the notice carries whole seconds
    tid = _hub_card(hub.conn)
    payload, text = NOTICES[path](hub.conn, tid)
    assert payload["admission"] == {"reason": "llm_pacing", "status": "held",
                                    "providers": [{"provider": "anthropic", "state": "held", "until": until}]}, payload
    stamp = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(until))
    assert f"held: LLM quota pacing (anthropic held pool) until {stamp}" in text, text
    _assert_names_no_re_placement(text)


@pytest.mark.parametrize("state", [lp.PACED, lp.HELD])
@pytest.mark.parametrize("path", sorted(NOTICES))
def test_ready_hint_matches_the_notice_pacing_admission(hub, state, path):
    until = int(time.time()) + 3600
    _store(("anthropic", state, until))
    tid = _hub_card(hub.conn)
    payload, text = NOTICES[path](hub.conn, tid)
    hint = pu.ready_hint(hub.conn, kb.get_task(hub.conn, tid), now=payload["observed_at"])
    assert hint is not None
    assert pu._observation(payload) in hint
    assert pu._options(tid, payload=payload) in hint
    assert "it starts on its own once its provider pool is back on pace" in hint
    assert "this machine cannot lease it" in hint
    _assert_names_no_re_placement(hint)
    assert "LLM quota pacing" in text


# --- card case 4: no positive read, today's payload and words -------------------------------------------------------


@pytest.mark.parametrize("read", ["open", "unknown", "absent", "unreadable", "stale", "failing"])
@pytest.mark.parametrize("path", sorted(NOTICES))
def test_without_a_positive_governor_read_the_notice_is_as_before(hub, monkeypatch, path, read):
    {"open": lambda: _store(("anthropic", lp.OPEN)),
     "unknown": lambda: _store(("anthropic", lp.UNKNOWN)),
     "absent": lambda: None,
     "unreadable": lambda: pd.state_path().write_text("{not json", encoding="utf-8"),
     "stale": lambda: _store(("anthropic", lp.PACED), age=10 * READ_INTERVAL),
     "failing": lambda: _store(("anthropic", lp.PACED))}[read]()
    asked, real = [], getattr(pd, "paced_pools", None)

    def governor(conn, task_id, *args, **kw):
        asked.append(task_id)
        if read == "failing":
            raise RuntimeError("governor read failed")
        return real(conn, task_id, *args, **kw)

    monkeypatch.setattr(pd, "paced_pools", governor, raising=False)
    tid = _hub_card(hub.conn)
    payload, text = NOTICES[path](hub.conn, tid)
    assert asked == [tid], asked  # the emitting gateway asked its own governor, once, for this card
    assert payload["admission"] == UNKNOWN, payload
    assert "admission reason unknown" in text and "re-place on a capable gateway with free slots" in text, text
    assert "pacing" not in json.dumps(payload) and "pacing" not in text, (payload, text)
    task, now = kb.get_task(hub.conn, tid), payload["observed_at"]
    hint = pu.ready_hint(hub.conn, task, now=now)
    assert hint is not None
    assert asked == [tid, tid]
    monkeypatch.setattr(pu, "_pacing_admission", lambda *_a: None)
    assert hint == pu.ready_hint(hub.conn, task, now=now)
    assert "re-place on a capable gateway with free slots" in hint
    assert "admission reason" not in hint


# --- every condition the rule adds ----------------------------------------------------------------------------------


def test_a_card_this_gateway_can_lease_keeps_its_own_dispatchers_words(board):
    _store(("anthropic", lp.PACED))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="local", assignee="default", model_override=OPUS, provider_override="anthropic",
                             session_id="owner-chat")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (int(time.time()) - 40 * 60, tid))
            conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ?", (int(time.time()) - 40 * 60, tid))
        payload, text = _chat_wait(conn, tid)
    assert payload["admission"] == UNKNOWN and "LLM quota pacing" not in text, (payload, text)


def test_an_unplaceable_card_keeps_the_re_place_advice(hub):
    _store(("anthropic", lp.PACED))
    tid = _pool_card(hub.conn)
    with kb.write_txn(hub.conn):  # create_task refuses labels no machine carries; an old card can still hold them
        hub.conn.execute("UPDATE tasks SET runs_on = ? WHERE id = ?",
                         (json.dumps(["capability:no-machine-carries-this"]), tid))
    payload, text = _chat_wait(hub.conn, tid)
    assert payload["admission"] == UNKNOWN and "unplaceable: no machine can run it" in text, (payload, text)
    assert "LLM quota pacing" not in text and "re-place on a capable gateway" in text, text


def _machine_state(kind):
    return {"held": {"hold": True}, "draining": {"metrics": {"at": int(time.time() * 1000), "drain_active": 1}}}[kind]


def _set_registry(states):
    """The pool registry with ``{worker: "held" | "draining"}`` applied (the hub fetches it fresh on every read)."""
    machines = copy.deepcopy(pool_wait.POOL)
    for name, kind in states.items():
        machines[name].update(_machine_state(kind))
    wn.set_registry_fetch(lambda: {"me": pool_wait.HUB, "machines": helpers.seen_now(machines.values())})


@pytest.mark.parametrize("states", [("held", "held"), ("draining", "draining"), ("held", "draining")])
def test_a_pool_whose_every_matching_machine_is_held_or_draining_keeps_that_explanation(hub, states):
    """It starts when an operator releases the hold or the drain ends, not "once its provider pool is back on pace"."""
    _store(("anthropic", lp.PACED))
    _set_registry(dict(zip(("worker-3", "worker-4"), states)))
    tid = _hub_card(hub.conn)
    place = kqp.placement(hub.conn, tid)  # the registry says what it should, or this test proves nothing
    assert place["lane"] == "machine" and sorted((place.get("held") or []) + (place.get("draining") or [])) == [
        "worker-3", "worker-4"], place
    payload, text = _chat_wait(hub.conn, tid)
    assert payload["admission"] == UNKNOWN and "LLM quota pacing" not in text, (payload, text)


@pytest.mark.parametrize("kind", ["held", "draining"])
def test_one_machine_able_to_lease_it_is_enough_for_the_pacing_notice(hub, kind):
    _store(("anthropic", lp.PACED))
    _set_registry({"worker-3": kind})  # worker-4 is neither: the live case had 1 of 8 workers draining
    tid = _hub_card(hub.conn)
    place = kqp.placement(hub.conn, tid)
    assert place["lane"] == "machine" and place.get(kind) == ["worker-3"], place
    payload, text = _chat_wait(hub.conn, tid)
    assert payload["admission"]["reason"] == "llm_pacing" and "LLM quota pacing" in text, (payload, text)


@pytest.mark.parametrize("verdict", CAPABLE_VERDICTS)
def test_a_verdict_recorded_by_a_dispatcher_able_to_lease_the_card_keeps_todays_notice(hub, verdict):
    _store(("anthropic", lp.PACED))
    tid = _hub_card(hub.conn)
    assert skips.record(hub.conn, tid, verdict[0], detail=verdict[1])
    payload, text = _chat_wait(hub.conn, tid)
    if verdict[0] == "llm_paced":
        assert payload["admission"]["reason"] == "llm_paced" and "LLM quota pacing" in text, (payload, text)
    else:
        assert payload["admission"] == UNKNOWN and "LLM quota pacing" not in text, (payload, text)


@pytest.mark.parametrize("cannot_lease", CANNOT_LEASE)
def test_a_skip_that_only_says_the_recording_gateway_cannot_lease_the_card_is_no_verdict(hub, cannot_lease):
    _store(("anthropic", lp.PACED))
    tid = _pool_card(hub.conn)
    assert skips.record(hub.conn, tid, cannot_lease[0], detail=cannot_lease[1])
    payload, text = _chat_wait(hub.conn, tid)
    assert payload["admission"]["reason"] == "llm_pacing", payload
    assert "held: LLM quota pacing (anthropic paced pool)" in text, text


def test_a_card_without_any_skip_is_named_too(hub):
    _store(("anthropic", lp.PACED))
    payload, text = _chat_wait(hub.conn, _pool_card(hub.conn))
    assert payload["admission"]["reason"] == "llm_pacing" and "LLM quota pacing" in text, (payload, text)


def test_a_busy_pool_and_a_core_update_hold_still_come_first(hub):
    _store(("anthropic", lp.PACED))
    busy = _pool_card(hub.conn, title="behind three claims")
    now = int(time.time())
    for i in range(3):  # three same-label cards claimed by the pool within 30 min
        claimed = pool_wait._card(hub.conn, f"claimed {i}", now - 4000 + i)
        with kb.write_txn(hub.conn):
            hub.conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (claimed,))
            kb._append_event(hub.conn, claimed, "claimed", {"holder": f"worker-{3 + i % 2}", "run_uid": f"r{i}"})
    payload = _post(hub.conn, busy)
    assert payload["admission"] == {"reason": "matching_pool_busy", "status": "queued"}, payload
    hint = pu.ready_hint(hub.conn, kb.get_task(hub.conn, busy))
    assert hint is not None and "Not a placement problem" in hint
    assert "LLM quota pacing" not in hint
    # This host's update hold comes first only for a card this host leases; a pool card keeps its own reason, here
    # the busy pool (t_01a1e01b9645d70c: 41 pool notices blamed the hub's own update swap).
    held = kb.create_task(hub.conn, title="hub-leasable, held for an update", assignee="default", session_id="owner-chat")
    assert (kqp.position(hub.conn, kb.get_task(hub.conn, held)) or {}).get("lane") == "local"
    assert _post(hub.conn, held, core_update_hold=True)["admission"] == {"reason": "core_update_hold", "status": "held"}
    pooled = _post(hub.conn, _pool_card(hub.conn, title="queued, not held"), core_update_hold=True)
    assert pooled["admission"] == {"reason": "matching_pool_busy", "status": "queued"}, pooled
    assert "core_update_hold" not in pooled, pooled


def _claimed_by_the_pool(conn, n=3):
    """``n`` same-label cards the pool claimed within 30 min: what makes a notice say matching_pool_busy."""
    now = int(time.time())
    for i in range(n):
        claimed = pool_wait._card(conn, f"claimed {i}", now - 4000 + i)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (claimed,))
            kb._append_event(conn, claimed, "claimed", {"holder": f"worker-{3 + i % 2}", "run_uid": f"r{i}"})


@pytest.mark.parametrize("recorded", ["hub_only", "stale_paced_verdict"])
@pytest.mark.parametrize("path", sorted(NOTICES))
def test_a_held_pool_beats_same_label_claims_on_other_providers(hub, path, recorded):
    """Defect agent-b460ba9d37 (hub, 2026-11-15 17:17Z): openai-codex went ``held`` (all accounts at 100% of the
    7-day window, limit 0 on every worker) at 16:44Z, yet the stall notice for default-routed t_01a1e01b9645d70c said
    "admission reason matching_pool_busy ... Not a placement problem ... Check current admission holds", because
    workers had claimed two same-label cards routed to anthropic. The owner chat forecast pickup "as slots free up";
    the card could not start until the codex reset days later. Claims on other providers prove nothing about a pool
    that admits nothing: a held pool is named, with its until, ahead of the claim count. ``stale_paced_verdict``: the
    card's newest capable skip still says ``paced`` (recorded once, before the hold), as on #3206 that morning."""
    until = int(time.time()) + 5 * DAY
    _store(("anthropic", lp.HELD, until))
    tid = _hub_card(hub.conn)
    if recorded == "stale_paced_verdict":
        assert skips.record(hub.conn, tid, "llm_paced", detail="anthropic paced",
                            extra={"providers": [{"provider": "anthropic", "state": "paced", "limit": 1,
                                                  "running": 1}]})
    _claimed_by_the_pool(hub.conn)
    payload, text = NOTICES[path](hub.conn, tid)
    assert payload["admission"]["status"] == "held", payload
    assert payload["admission"]["providers"] == [{"provider": "anthropic", "state": "held", "until": until}], payload
    stamp = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(until))
    assert f"(anthropic held pool) until {stamp}" in text, text
    assert "matching_pool_busy" not in text and "Not a placement problem" not in text, text
    hint = pu.ready_hint(hub.conn, kb.get_task(hub.conn, tid), now=payload["observed_at"])
    assert hint is not None and f"(anthropic held pool) until {stamp}" in hint, hint
    assert "Not a placement problem" not in hint, hint


def test_a_paced_pool_with_same_label_claims_stays_a_queue_wait(hub):
    """The guard's boundary: a paced pool still admits starts (the claims show it), so the busy queue words stand."""
    _store(("anthropic", lp.PACED))
    tid = _hub_card(hub.conn)
    _claimed_by_the_pool(hub.conn)
    payload, text = _chat_wait(hub.conn, tid)
    assert payload["admission"] == {"reason": "matching_pool_busy", "status": "queued"}, payload
    assert "Not a placement problem" in text, text


def test_this_hosts_update_hold_does_not_hide_a_pool_cards_pacing_wait(hub):
    """The incident shape (t_01a1e01b9645d70c): a pool card paced by the governor while the hub swaps its own
    core gets the pacing notice, not the hub's hold sentence."""
    _store(("anthropic", lp.PACED))
    payload = _post(hub.conn, _pool_card(hub.conn), core_update_hold=True, core_update_started="x")
    assert payload["admission"]["reason"] == "llm_pacing", payload
    assert not any(k.startswith("core_update_") for k in payload), payload
    text = cw.notice_text(payload)
    assert "LLM quota pacing" in text and "claims are held for a pending" not in text, text


def test_a_card_missing_from_the_shared_board_is_not_a_pacing_wait(hub):
    """No worker can import a card the Board does not hold: the sync repair is the advice, not the pool's pace."""
    _store(("anthropic", lp.PACED))
    tid = _hub_card(hub.conn)
    assert _post(hub.conn, tid, missing_shared_board=True, sync_reject_reason="rejected")["admission"] == UNKNOWN


def test_an_ungoverned_provider_and_a_mandatory_pin_always_start_so_they_are_not_named(hub):
    _store(("anthropic", lp.PACED), ("zai", lp.PACED))
    for kw in ({"model_override": "glm-5", "provider_override": "zai"}, {"pin_mandatory": True}):
        assert _post(hub.conn, _pool_card(hub.conn, **kw))["admission"] == UNKNOWN, kw


# --- the governor read itself ---------------------------------------------------------------------------------------


@pytest.fixture
def card(board):
    with kbc.connect() as conn:
        yield conn, kb.create_task(conn, title="pinned", assignee="default", model_override=OPUS,
                                   provider_override="anthropic")


def test_the_read_returns_every_candidate_pool_when_all_are_paced_or_held(card, monkeypatch):
    conn, tid = card
    until = time.time() + DAY
    monkeypatch.setattr(pd, "candidate_providers", lambda *_a: ["anthropic", "openai-codex"])
    _store(("anthropic", lp.PACED), ("openai-codex", lp.HELD, until))
    paces = pd.paced_pools(conn, tid)
    assert [(p.provider, p.state, p.until) for p in paces] == [("anthropic", lp.PACED, None),
                                                              ("openai-codex", lp.HELD, until)]


@pytest.mark.parametrize("other", [("openai-codex", lp.OPEN), ("openai-codex", lp.UNKNOWN), None])
def test_one_candidate_pool_that_is_not_paced_or_held_means_no_positive_read(card, monkeypatch, other):
    """The card may run there, so the governor does not hold it: ``_verdicts`` returns None at the first admitting pool."""
    conn, tid = card
    monkeypatch.setattr(pd, "candidate_providers", lambda *_a: ["anthropic", "openai-codex"])
    _store(("anthropic", lp.PACED), *([other] if other else []))
    assert pd.paced_pools(conn, tid) is None


def test_no_candidate_provider_means_no_positive_read(card, monkeypatch):
    conn, tid = card
    _store(("anthropic", lp.PACED))
    monkeypatch.setattr(pd, "candidate_providers", lambda *_a: [])
    assert pd.paced_pools(conn, tid) is None


@pytest.mark.parametrize("cfg", [lp.PacingConfig(enabled=False), lp.PacingConfig(providers=("openai-codex",))],
                         ids=["disabled", "provider-not-governed"])
def test_the_read_follows_the_governors_own_start_rules(card, monkeypatch, cfg):
    conn, tid = card
    _store(("anthropic", lp.PACED))
    assert [p.state for p in pd.paced_pools(conn, tid)] == [lp.PACED]  # the control: the card is judged
    monkeypatch.setattr(pd, "load_config", lambda: cfg)  # enabled: false restores ungoverned claims; others always start
    assert pd.paced_pools(conn, tid) is None


def test_a_card_that_is_not_on_the_board_is_no_positive_read(card):
    conn, _tid = card
    _store(("anthropic", lp.PACED))
    assert pd.paced_pools(conn, "t_missing") is None


def test_the_read_ages_out_a_stored_pace_after_a_few_read_intervals(card):
    conn, tid = card
    bound = pd.STALE_READ_INTERVALS * READ_INTERVAL
    _store(("anthropic", lp.PACED), age=bound - 30)
    assert [p.state for p in pd.paced_pools(conn, tid)] == [lp.PACED]
    _store(("anthropic", lp.PACED), age=bound + 30)
    assert pd.paced_pools(conn, tid) is None
    _store(("anthropic", lp.PACED), age=-3600)  # stamped in the future: not a read this gateway can age
    assert pd.paced_pools(conn, tid) is None
    data = json.loads(pd.state_path().read_text(encoding="utf-8"))
    data["anthropic"]["read_at"] = "yesterday"
    pd.state_path().write_text(json.dumps(data), encoding="utf-8")
    assert pd.paced_pools(conn, tid) is None


def test_the_read_makes_no_usage_read_and_writes_no_state(card, monkeypatch):
    conn, tid = card
    _store(("anthropic", lp.PACED))
    before = pd.state_path().read_bytes()

    def refused(*_a, **_kw):
        raise AssertionError("a notice must not read account usage")

    monkeypatch.setattr(pd, "read_pool", refused)
    monkeypatch.setattr(pd, "_snapshots", refused)
    assert [p.state for p in pd.paced_pools(conn, tid)] == [lp.PACED]
    assert pd.state_path().read_bytes() == before


# --- wording from a stored payload ----------------------------------------------------------------------------------


def _line(admission, render=cw.notice_text):
    return render({"parent": "t_1", "observed_at": 1_800_000_000, "board": "default", "queue": "q",
                   "admission": admission})


def test_a_stored_pacing_admission_renders_with_the_earliest_until():
    soon, late = 1_800_100_000, 1_800_400_000
    held = {"reason": "llm_pacing", "status": "held", "providers": [
        {"provider": "anthropic", "state": "held", "until": late}, {"provider": "openai-codex", "state": "held",
                                                                   "until": soon}]}
    stamp = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(soon))
    for render in (cw.notice_text, pu.notice_text):
        text = _line(held, render)
        assert f"admission reason llm_pacing: held: LLM quota pacing (anthropic held, openai-codex held pool) until " \
               f"{stamp}]" in text, text
        assert "re-place" not in text and "bump" not in text, text
    mixed = {**held, "providers": [{"provider": "anthropic", "state": "paced"}, held["providers"][1]]}
    assert f"(anthropic paced, openai-codex held pool) until {stamp}]" in _line(mixed)


@pytest.mark.parametrize("admission", [
    {"reason": "llm_pacing"}, {"reason": "llm_pacing", "providers": "x"},
    {"reason": "llm_pacing", "providers": [None, 3, {"provider": "anthropic"}]},
    {"reason": "llm_pacing", "providers": [{"state": "held"}, {"provider": "", "state": "paced"}]},  # no pool named
    {"reason": "llm_pacing", "providers": 5},  # not a list, and not iterable
    {"reason": "other_reason", "providers": [{"provider": "anthropic", "state": "paced"}]}, "llm_pacing", None,
    UNKNOWN])
def test_a_malformed_or_foreign_admission_does_not_render_pacing_words(admission):
    for render in (cw.notice_text, pu.notice_text):
        line = _line(admission, render)
        assert "admission reason " in line and "LLM quota pacing" not in line, (admission, line)
        assert "re-place on a capable gateway" in line, line  # the generic advice stands


def test_an_until_that_is_not_a_time_is_left_out_of_the_words():
    bad = {"reason": "llm_pacing", "status": "held", "providers": [
        {"provider": "anthropic", "state": "held", "until": "soon"}, {"provider": "x", "state": "held", "until": True}]}
    line = _line(bad)
    assert "admission reason llm_pacing: held: LLM quota pacing (anthropic held, x held pool)]" in line, line
