"""A mid-pass slot release must not let lower-priority work jump a capacity refusal."""

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_runs_on_dispatch as support
from tests.hermes_cli.kanban_review_fixture import create_review

dispatch_gws = support.gws
svc = support.svc


@pytest.fixture
def gws(dispatch_gws):
    from tests.hermes_cli.kanban_review_fixture import completed_gateway_source
    hub, _ = dispatch_gws
    completed_gateway_source(hub)
    return dispatch_gws


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("inherited", [False, True])
def test_capacity_release_restarts_in_priority_order(gws, monkeypatch, lane, inherited):
    hub, _ = gws
    hub.enter()
    cards = {}
    for priority in (0, 530, 150, 900, 460):
        create = create_review if lane == "review" else kb.create_task
        tid = create(hub.conn, title=f"priority {priority}", assignee="default",
                             priority=0 if inherited and priority == 900 else priority)
        cards[priority] = tid
    if inherited:
        kb.create_task(hub.conn, title="urgent waiter", assignee="default", priority=900, parents=[cards[900]])
    hub.round()
    acquire = client.HubLeaseClient.acquire
    attempts = []

    def release_after_refusal(self, **kwargs):
        attempts.append(kwargs["task_uid"])
        if len(attempts) == 1:
            # The slot frees just after the highest-priority row was refused.
            return sync.Lease(granted=False, run_uid=kwargs["run_uid"], reason="capacity")
        return acquire(self, **kwargs)

    monkeypatch.setattr(client.HubLeaseClient, "acquire", release_after_refusal)
    first = hub.tick(max_in_progress=3)
    assert first.claim_deferred == [(cards[900], "capacity")]
    assert support._spawned(first) == []
    assert attempts == [cards[900]]
    # Reopen persistent state as after restart: no in-memory lane cursor is retained.
    hub.conn.close()
    hub.conn = kbc.connect(hub.home / "kanban.db")
    second = hub.tick(max_in_progress=3)
    assert support._spawned(second) == [cards[p] for p in (900, 530, 460)]
    assert attempts == [cards[900], cards[900], cards[530], cards[460]]


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("reason", ["held", "labels_mismatch", "not_claimable", "offline"])
def test_card_specific_refusals_do_not_fence_other_candidates(gws, monkeypatch, lane, reason):
    hub, _ = gws
    hub.enter()
    create = create_review if lane == "review" else kb.create_task
    high = create(hub.conn, title="higher", assignee="default", priority=900)
    low = create(hub.conn, title="lower", assignee="default", priority=100)
    hub.round()
    acquire = client.HubLeaseClient.acquire

    def refuse_high(self, **kwargs):
        if kwargs["task_uid"] == high:
            return sync.Lease(granted=False, run_uid=kwargs["run_uid"], reason=reason)
        return acquire(self, **kwargs)

    monkeypatch.setattr(client.HubLeaseClient, "acquire", refuse_high)
    result = hub.tick(max_in_progress=1)
    assert result.claim_deferred == [(high, reason)]
    assert support._spawned(result) == [low]


@pytest.mark.parametrize("refused_lane", ["ready", "review"])
def test_capacity_fence_covers_combined_ranked_pass(gws, monkeypatch, refused_lane):
    """Ready and review rows form one ranked pass; a capacity refusal fences everything ranked after it."""
    hub, _ = gws
    hub.enter()
    ready = [kb.create_task(hub.conn, title=f"ready {p}", assignee="default", priority=p)
             for p in (900, 530, 100)]
    review = create_review(hub.conn, title="ranked review", assignee="default", priority=700)
    # This exercises Board capacity, not the per-tick admission budget. Pre-admit
    # the review row so all four candidates can reach the lease boundary.
    from hermes_cli import kanban_hub_admission_claim as admission
    counts = admission.screen(hub.conn)
    assert counts.judged == 3
    counts = admission.screen(hub.conn)
    assert counts.judged == 1
    hub.round()
    acquire = client.HubLeaseClient.acquire
    refused = ready[0] if refused_lane == "ready" else review
    attempts = []

    def refuse_once(self, **kwargs):
        attempts.append(kwargs["task_uid"])
        if kwargs["task_uid"] == refused:
            return sync.Lease(granted=False, run_uid=kwargs["run_uid"], reason="capacity")
        return acquire(self, **kwargs)

    monkeypatch.setattr(client.HubLeaseClient, "acquire", refuse_once)
    result = hub.tick(max_in_progress=3)
    assert result.claim_deferred == [(refused, "capacity")]
    # Combined score order: ready 900, review 700, ready 530, ready 100.
    if refused_lane == "ready":
        assert support._spawned(result) == []
        assert attempts == [ready[0]]
    else:
        assert support._spawned(result) == [ready[0]]
        assert attempts == [ready[0], review]
