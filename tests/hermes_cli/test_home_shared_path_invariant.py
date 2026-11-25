"""Single transfer-isolation gate: mixed states plus every prior reviewer regression."""
from __future__ import annotations

import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as dbc, kanban_db_sync as sync
from hermes_cli import kanban_home_transfer as transfer
from hermes_cli import kanban_lanes as lanes, kanban_triage_sweep as sweep
from hermes_cli import kanban_block_policy as policy, kanban_shared_blockers as blockers
from hermes_cli import kanban_spend_decision as decision, kanban_spend_reviewer as spend_review
from hermes_cli.kanban_db_dispatch_queries import lane_rows
from hermes_cli.kanban_db_dispatch_quota import clear_quota_error
from tests.hermes_cli import test_kanban_home_source_stop as support
from tests.hermes_cli import test_home_review_contracts as contracts
from tests.hermes_cli import test_review_daily_transfer as daily
from tests.hermes_cli import test_home_review_boundaries as boundaries
from tests.hermes_cli import test_review_4300_nonce as nonce
from tests.hermes_cli import test_review_4300_ask as ask
from tests.hermes_cli import test_review_home_feed_isolation as feed_review
from tests.hermes_cli import test_home_label_presend as presend
from tests.hermes_cli import test_home_release_operators as operator_cases
from tests.hermes_cli import test_home_release_auxiliary as auxiliary_cases
from tests.hermes_cli import test_home_release_sweeps as sweep_cases
from tests.hermes_cli import test_home_feed_entrypoints as entry_cases
from tests.hermes_cli import test_home_review_custody_repros as custody_review
from tests.hermes_cli import test_home_probe_writer_custody as writer_cases
from tests.hermes_cli import test_home_shared_consumer_custody as consumer_cases
from tests.hermes_cli import test_home_shared_consumer_io as consumer_io

consumer_board = consumer_cases.consumer_board
test_frozen_ready_does_not_suppress_shared_probe = consumer_cases.test_frozen_ready_does_not_suppress_shared_probe
test_followup_writer_race_keeps_healthy_sweep = consumer_cases.test_followup_writer_race_keeps_healthy_sweep
test_caused_review_captures_at_writer_after_fallible_work = consumer_cases.test_caused_review_captures_at_writer_after_fallible_work
test_attachment_custody_skips_io_and_late_bookkeeping = consumer_cases.test_attachment_custody_skips_io_and_late_bookkeeping
test_review_journal_writer_entry_checks_task_column = consumer_cases.test_review_journal_writer_entry_checks_task_column
test_existing_review_wire_is_not_enriched_or_replaced = consumer_cases.test_existing_review_wire_is_not_enriched_or_replaced
test_caused_review_late_settlement_preserves_custody = consumer_cases.test_caused_review_late_settlement_preserves_custody
test_attachment_writer_entry_custody_and_single_slot_peer = consumer_cases.test_attachment_writer_entry_custody_and_single_slot_peer
test_network_callback_real_other_connection_cannot_abort_peer_or_stamp_rebound = consumer_io.test_network_callback_real_other_connection_cannot_abort_peer_or_stamp_rebound
test_publication_holds_real_writer_reservation = consumer_io.test_publication_holds_real_writer_reservation
test_epoch_envelope_keeps_semantic_receipt_digest_and_exact_replay = consumer_io.test_epoch_envelope_keeps_semantic_receipt_digest_and_exact_replay
test_review_target_is_captured_not_just_source = consumer_io.test_review_target_is_captured_not_just_source

board = support.board
live = contracts.live
conn = daily.conn
configured = daily.configured
person = daily.person

# Explicit exports keep the original independently reproduced reviewer probes
# unchanged and collect all five rounds in this single first-run gate.
test_prepared_probe_does_not_starve_due_neighbour = boundaries.test_prepared_probe_does_not_starve_due_neighbour
test_owner_rejects_unsupported_identity_before_fencing_or_network = boundaries.test_owner_rejects_unsupported_identity_before_fencing_or_network
test_stop_accepts_wire_millisecond_timestamp = contracts.test_stop_accepts_wire_millisecond_timestamp
test_stop_rejects_real_timestamp_change_and_rolls_back = contracts.test_stop_rejects_real_timestamp_change_and_rolls_back
test_old_local_park_does_not_become_silent_at_home_transition = contracts.test_old_local_park_does_not_become_silent_at_home_transition
test_transfer_liveness_grace_is_bounded_and_survives_reopen = contracts.test_transfer_liveness_grace_is_bounded_and_survives_reopen
test_post_transfer_alive_extends_observation_not_baseline = contracts.test_post_transfer_alive_extends_observation_not_baseline
test_explicit_remote_probe_keeps_existing_silence_contract = contracts.test_explicit_remote_probe_keeps_existing_silence_contract
test_new_park_cannot_borrow_historical_transfer_grace = contracts.test_new_park_cannot_borrow_historical_transfer_grace
test_pending_transfer_never_installs_liveness_grace = contracts.test_pending_transfer_never_installs_liveness_grace
test_pending_renewal_uses_public_refusal_contract = contracts.test_pending_renewal_uses_public_refusal_contract
test_pending_renewal_command_returns_structured_refusal = contracts.test_pending_renewal_command_returns_structured_refusal
test_deleted_label_target_terminates_delivery = contracts.test_deleted_label_target_terminates_delivery
test_aborted_nonce_cannot_be_reused_for_new_version = nonce.test_aborted_nonce_cannot_be_reused_for_new_version
test_transferred_hostless_ask_gets_fresh_observation_window = ask.test_transferred_hostless_ask_gets_fresh_observation_window
test_daily_release_skips_prepared = daily.test_daily_release_skips_prepared
test_healthy_feed_progress_during_handoff = feed_review.test_healthy_feed_progress_during_handoff
test_actual_delivery_retry_distinguishes_presend_failure_from_legacy = presend.test_actual_delivery_retry_distinguishes_presend_failure_from_legacy
# Auxiliary boundaries are part of the same gate, not a later reviewer surprise.
test_quota_and_hold_release_refuse_before_external_reads = operator_cases.test_quota_and_hold_release_refuse_before_external_reads
test_review_preparation_skips_before_reading_park = operator_cases.test_review_preparation_skips_before_reading_park
test_triage_cap_and_contained_prelude_create_no_children = operator_cases.test_triage_cap_and_contained_prelude_create_no_children
test_containment_resume_refuses_before_bootstrap = operator_cases.test_containment_resume_refuses_before_bootstrap
test_notice_never_reads_placement_or_writes_frozen_target = auxiliary_cases.test_notice_never_reads_placement_or_writes_frozen_target
test_stranded_producers_do_not_refile_or_escalate = auxiliary_cases.test_stranded_producers_do_not_refile_or_escalate
test_spend_promote_and_review_queue_refuse_before_accounting = auxiliary_cases.test_spend_promote_and_review_queue_refuse_before_accounting
test_stop_ended_preserves_transfer_custody = auxiliary_cases.test_stop_ended_preserves_transfer_custody
test_recovery_requester_skips_before_creating_any_child = auxiliary_cases.test_recovery_requester_skips_before_creating_any_child
test_preclaim_accounting_is_write_free_for_transfers = sweep_cases.test_preclaim_accounting_is_write_free_for_transfers
test_spend_reviewer_due_and_grant_exclude_transfer = sweep_cases.test_spend_reviewer_due_and_grant_exclude_transfer
test_poison_or_slow_replay_cannot_abort_independent_incoming_page = entry_cases.test_poison_or_slow_replay_cannot_abort_independent_incoming_page
test_admin_repair_and_keep_filtered_apply_never_replay_unrelated_custody = entry_cases.test_admin_repair_and_keep_filtered_apply_never_replay_unrelated_custody
test_real_budget_wrapper_defers_certified_prepared_containment_before_any_marker_write = entry_cases.test_real_budget_wrapper_defers_certified_prepared_containment_before_any_marker_write
test_pending_drops_inflight_result_without_probe_mutation = custody_review.test_pending_drops_inflight_result_without_probe_mutation
test_backfill_skips_pending_ask_and_restores_neighbor = custody_review.test_backfill_skips_pending_ask_and_restores_neighbor
test_reconcile_rechecks_custody_after_selection = custody_review.test_reconcile_rechecks_custody_after_selection
test_late_results_preserve_all_local_tables_and_healthy_neighbor = writer_cases.test_late_results_preserve_all_local_tables_and_healthy_neighbor
test_reconcile_writer_entry_race_keeps_local_tables = writer_cases.test_reconcile_writer_entry_race_keeps_local_tables
test_backfill_respects_completed_historical_host_and_unknown_custody = writer_cases.test_backfill_respects_completed_historical_host_and_unknown_custody
test_late_probe_followup_cannot_record_decision_recovery = writer_cases.test_late_probe_followup_cannot_record_decision_recovery


def mixed(conn, status, setup=None):
    """One board containing every interval, not separate homogeneous fixtures."""
    from hermes_cli.kanban_home_generation import mirror_feed
    cards = {}
    for phase in ('prepared', 'frozen', 'aborted', 'committed', 'pending', 'ordinary'):
        tid = kb.create_task(conn, title=phase)
        with dbc.write_txn(conn):
            conn.execute('UPDATE tasks SET status=?,created_on=\'worker-a3\',sync_version=7 WHERE id=?', (status, tid))
            conn.execute("UPDATE kanban_sync_state SET gateway_id='worker-a3'")
            if setup is not None:
                setup(conn, tid)
            conn.execute('DELETE FROM kanban_sync_outbox')
            record = support.record(conn, tid)
            record['nonce'] = 'invariant-' + tid
            if phase not in ('ordinary', 'pending'):
                transfer.prepare(conn, tid, source='worker-a3', destination='hub', nonce=record['nonce'], expected_version=7)
            actions = {
                'aborted': lambda: transfer.abort_source(conn, {**record, 'phase': 'aborted', 'rows': []}),
                'committed': lambda: transfer.finalize_source(conn, record),
                'pending': lambda: mirror_feed(conn, tid, {'home_generation': record['nonce']}, 8),
                'frozen': lambda: corrupt_marker(conn, tid),
            }
            actions.get(phase, lambda: None)()
        cards[phase] = tid
    return cards


def snapshot(conn, ids):
    return {tid: (tuple(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()),
                  [tuple(r) for r in conn.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY id', (tid,))],
                  {table: rows for table, rows in writer_cases.snapshot(conn, tid).items()
                   if rows and (table.startswith('task_wake_') or table == 'kanban_home_probe_watch1')})
            for tid in ids}


def corrupt_marker(conn, tid):
    conn.execute('DROP TRIGGER IF EXISTS home_prepared1_marker_update')
    conn.execute("UPDATE kanban_home_prepared1 SET source='' WHERE task_id=?", (tid,))


def setup_spend(conn, tid):
    kb._append_event(conn, tid, decision.EVENT, {'reason': 'ordinary budget stop'})
    episode = spend_review.episode_of(decision.pending_episode(conn, tid))
    kb._append_event(conn, tid, spend_review.QUEUED, {'episode': episode})


def case_feed(board, monkeypatch, cards, normal, frozen, before, operation):
    cursor = sync.sync_state(board)['cursor']
    rows = [feed_review.change(board, tid, 9, cursor+i+1, title='native feed edit') for i, tid in enumerate(cards.values())]
    from hermes_cli.kanban_sync_apply_budget import apply_page
    result = apply_page(board, rows, new_cursor=cursor+len(rows), observers=False)
    assert result.deferred == 2
    assert sync.sync_state(board)['cursor'] == cursor+len(rows)
    for tid in normal:
        assert kb.get_task(board, tid).title == 'native feed edit'
    assert snapshot(board, frozen[:2]) == {tid: before[tid] for tid in frozen[:2]}


def case_readiness(board, monkeypatch, cards, normal, frozen, before, operation):
    kb.recompute_ready(board)


def case_dispatch(board, monkeypatch, cards, normal, frozen, before, operation):
    assert {r['id'] for r in lane_rows(board, 'ready')} == set(normal)


def case_promote(board, monkeypatch, cards, normal, frozen, before, operation):
    calls = {
        'promote': lambda tid: kb.promote_task(board, tid, actor='test')[0],
        'unblock': lambda tid: kb.unblock_task(board, tid),
        'reopen': lambda tid: kb.reopen_review_task(board, tid),
        'admit': lambda tid: lanes.admit(board, tid, actor='test', reason='capacity'),
    }
    for tid in cards.values():
        assert bool(calls[operation](tid)) == (tid in normal)


def case_stale(board, monkeypatch, cards, normal, frozen, before, operation):
    assert set(sweep.sweep_stale_triage(board, older_than=0)) == set(normal)


def case_agent_block(board, monkeypatch, cards, normal, frozen, before, operation):
    with dbc.write_txn(board):
        for tid in normal:
            board.execute("UPDATE tasks SET block_kind='transient',human_request=? WHERE id=?",
                          ('{"action":"old ask"}' if operation == 'standing' else None, tid))
    if operation == 'standing':
        monkeypatch.setattr(policy, 'standing_ask_error', lambda *a: ('test', 'premise gone', {}))
        assert set(policy.sweep_standing_asks(board)) == set(normal)
    else:
        assert set(policy.sweep_agent_owned_blocks(board, grace=0)) == set(normal)


def case_quota(board, monkeypatch, cards, normal, frozen, before, operation):
    for tid in cards.values():
        clear_quota_error(board, tid, 'quota wall')
    assert all(kb.get_task(board, tid).last_failure_error is None for tid in normal)


def case_spend_review(board, monkeypatch, cards, normal, frozen, before, operation):
    monkeypatch.setattr(spend_review, 'settings', lambda: (True, 5))
    assert {tid for tid, _, _ in spend_review._due(board, 9999999999, set())} == set(normal)


def case_spend_hold(board, monkeypatch, cards, normal, frozen, before, operation):
    from hermes_cli import kanban_spend_budget as budget, kanban_no_progress as progress
    seen = []
    target = progress if operation == 'no_progress' else budget
    seam = 'decide' if operation == 'no_progress' else 'card_limits'
    original = getattr(target, seam)
    def observed(conn, tid, *args, **kwargs):
        seen.append(tid)
        return original(conn, tid, *args, **kwargs)
    monkeypatch.setattr(target, seam, observed)
    for tid in cards.values():
        target.hold(board, tid)
    assert set(seen) == set(normal)


def case_recovery(board, monkeypatch, cards, normal, frozen, before, operation):
    from hermes_cli import kanban_resume_requester as requester, kanban_resume_exec as rx
    seen = []
    original = rx.exhaustion_refusal
    def eligible(conn, tid, *args, **kwargs):
        seen.append(tid)
        return original(conn, tid, *args, **kwargs)
    monkeypatch.setattr(rx, 'exhaustion_refusal', eligible)
    requester.ensure_recovery_cards(board)
    assert set(seen) == set(normal)


def case_release(board, monkeypatch, cards, normal, frozen, before, operation):
    from hermes_cli import kanban_release as release
    for tid in cards.values():
        result = release.release(board, tid, writer=('human', None, 'human'), runs=1)
        assert (result.get('refusal') == 'home_transfer') == (tid in frozen)


def case_renew(board, monkeypatch, cards, normal, frozen, before, operation):
    from hermes_cli import kanban_scope_renewal as renewal
    for tid in cards.values():
        with pytest.raises(renewal.RenewalRefused) as exc:
            renewal.renew_local(board, tid, episode='absent', comment_id=1, reason='mandate',
                                actor=renewal.Actor('human', None, 'human', None))
        assert (exc.value.code == 'home_transfer') == (tid in frozen)


def case_shared_clear(board, monkeypatch, cards, normal, frozen, before, operation):
    state = blockers.BlockerState(blocker_id='fixture', cleared_by=('other', 1), cleared_at=10,
                                 cleared_via='run', behind=dict.fromkeys(cards.values(), 1))
    monkeypatch.setattr(blockers, 'blocker_states', lambda *a, **k: {'fixture': state})
    blockers.release_cleared_blockers(board)
    assert {r[0] for r in board.execute('SELECT task_id FROM task_events WHERE kind=?',
                                      (blockers.CLEARED_EVENT,))} == set(normal)


CASES = {
    'feed': case_feed, 'readiness': case_readiness, 'dispatch': case_dispatch,
    'promote': case_promote, 'unblock': case_promote, 'reopen': case_promote, 'admit': case_promote,
    'stale': case_stale, 'agent_block': case_agent_block, 'standing': case_agent_block,
    'quota': case_quota, 'spend_review': case_spend_review, 'spend_hold': case_spend_hold,
    'no_progress': case_spend_hold, 'recovery': case_recovery, 'release': case_release,
    'renew': case_renew, 'shared_clear': case_shared_clear,
}


@pytest.mark.parametrize('operation', CASES)
def test_mixed_states_never_abort_or_delay_normal_cards(board, monkeypatch, operation):
    status = {'readiness': 'todo', 'dispatch': 'ready', 'reopen': 'review', 'unblock': 'blocked',
              'agent_block': 'blocked', 'standing': 'blocked', 'quota': 'ready', 'shared_clear': 'ready'}.get(operation, 'triage')
    setup = {
        'spend_review': setup_spend,
        'recovery': lambda conn, tid: kb._append_event(conn, tid, 'continuation_exhausted', {}),
        'quota': lambda conn, tid: conn.execute("UPDATE tasks SET last_failure_error='quota wall' WHERE id=?", (tid,)),
        'admit': lambda conn, tid: conn.execute('UPDATE tasks SET block_kind=? WHERE id=?', (lanes.GATE_KIND, tid)),
    }.get(operation)
    cards = mixed(board, status, setup)
    frozen = [cards[k] for k in ('prepared', 'frozen', 'committed', 'pending')]
    normal = [cards[k] for k in ('ordinary', 'aborted')]
    before = snapshot(board, frozen)
    CASES[operation](board, monkeypatch, cards, normal, frozen, before, operation)
    if operation != 'feed':
        assert snapshot(board, frozen) == before
    if operation in ('readiness', 'promote', 'unblock', 'reopen', 'stale', 'admit'):
        assert all(kb.get_task(board, tid).status == 'ready' for tid in normal)


def test_daily_reset_on_mixed_board_releases_both_normal_controls(conn, configured):
    board = conn
    daily.limits(configured, cards_per_day=100)
    def setup(conn, tid):
        conn.execute('UPDATE tasks SET tenant=?,block_kind=? WHERE id=?', ('babybear', lanes.GATE_KIND, tid))
        kb._append_event(conn, tid, lanes.GATED, {'reason': 'limit:cards_per_day'})
    cards = mixed(board, 'triage', setup)
    frozen = [cards[k] for k in ('prepared', 'frozen', 'committed', 'pending')]
    before = snapshot(board, frozen)
    kb.recompute_ready(board)
    assert snapshot(board, frozen) == before
    for phase in ('ordinary', 'aborted'):
        assert kb.get_task(board, cards[phase]).status == 'ready'
