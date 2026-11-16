"""A refused legacy turn is not a completion delivery receipt."""
import logging
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from tui_gateway import prompt_turn, session_notifications
from tui_gateway.method_ctx import rebind
from tui_gateway.session_lifecycle import _session_turn_admission


@pytest.mark.parametrize('kind', ['async_delegation', 'completion'])
def test_refused_notification_retains_result_without_spending_attempts(tmp_path, kind):
    from tests.gateway.test_completion_admission import pending
    from tools import async_delegation as delegation
    from tools.process_registry import process_registry

    event = pending('owned', 'refused-notification') if kind == 'async_delegation' else {
        'type': 'completion', 'session_id': 'owned-process', 'session_key': 'owned'}
    session = {'history_lock': threading.RLock(), '_closing': True, 'running': True}
    emitted = []
    # Fork: with the requeue backoff bound below, the ten refusals would sleep ~30 s of real time.
    fake_time = SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=lambda _s: None)
    namespace = dict(time=fake_time, logger=logging.getLogger(__name__),
        _emit=lambda *args: emitted.append(args),
        _session_turn_admission=_session_turn_admission,
        _ensure_active_session_slot=lambda *args: None,
        _notif_log_failure=lambda *args: None)
    for module, names in [(prompt_turn, ('_admit_prompt_turn', '_run_prompt_submit')),
                          (session_notifications, ('_notif_submit', '_notif_claim_turn', '_notif_release_turn',
                           '_notif_release_claim',
                           '_notif_dispatch_event', '_notif_dispatch_completions', '_async_delegation_display_metadata'))]:
        for name in names:
            namespace[name] = rebind(getattr(module, name), namespace)
    # Also bind the retry helper when present; old code must fail on the receipt, not an absent test import.
    if hasattr(session_notifications, '_notif_defer_event'):
        namespace['_notif_defer_event'] = rebind(session_notifications._notif_defer_event, namespace)
    # Fork: the fork's owner-refusal backoff (_notif_owner_refused / _notif_owner_preflight) and per-session
    # hold (_notif_hold_for) sit on the same claim/submit/requeue path; bind them into the isolated namespace
    # too, else the rebound dispatcher dies on a NameError instead of exercising the refusal.
    for name in ('_notif_run_submit', '_notif_owner_refused', '_notif_owner_preflight', '_notif_note_owner_refusal',
                 '_notif_refusal_backoff', '_notif_hold_for', '_notif_retry_completions',
                 '_notification_event_requires_owner', 'start_synthesized_turn', 'admit_synthesized_turn'):
        if hasattr(session_notifications, name):
            namespace[name] = rebind(getattr(session_notifications, name), namespace)
    for name in ('_OWNER_REFUSAL_CODES', '_NOTIF_REFUSAL_BACKOFF_S', '_NOTIF_REFUSAL_BACKOFF_MAX_S',
                 '_notif_held_lock'):
        if hasattr(session_notifications, name):
            namespace[name] = getattr(session_notifications, name)
    # Fork: _run_prompt_submit stages the turn, then runs it through _run_admitted_prompt.
    if hasattr(prompt_turn, '_run_admitted_prompt'):
        namespace['_run_admitted_prompt'] = rebind(prompt_turn._run_admitted_prompt, namespace)
    # The dispatcher binds the session's DB row before admission; this session has no store.
    namespace['_ensure_session_db_row'] = lambda _session: True
    original_queue = process_registry.completion_queue
    process_registry.completion_queue = queue.Queue()
    try:
        for _ in range(10):
            session['running'] = kind == 'async_delegation'
            if kind == 'async_delegation':
                namespace['_notif_dispatch_event']('live', session, event, 'nonhuman summary')
                record = delegation.get_durable_delegation(event['delegation_id'])
                assert (record['delivery_state'], record['delivery_attempts']) == ('pending', 0), record
            else:
                namespace['_notif_dispatch_completions']('live', session, [(event, 'result')],
                    SimpleNamespace(completion_queue=process_registry.completion_queue,
                                    is_completion_consumed=lambda _: False), None)
            assert process_registry.completion_queue.get_nowait() == event
            assert session['running'] is False
        assert not any(e[0] == 'message.start' for e in emitted), emitted
        accepted = []
        def accept(*args, **kwargs):
            accepted.append((args, kwargs))
            return True
        namespace['_run_prompt_submit'] = accept
        session['running'] = kind == 'async_delegation'
        if kind == 'async_delegation':
            namespace['_notif_dispatch_event']('live', session, event, 'nonhuman summary')
            assert delegation.get_durable_delegation(event['delegation_id'])['delivery_state'] == 'delivered'
            assert accepted[0][1]['display_kind'] == 'async_delegation_complete'
        else:
            namespace['_notif_dispatch_completions']('live', session, [(event, 'result')],
                SimpleNamespace(completion_queue=process_registry.completion_queue,
                                is_completion_consumed=lambda _: False), None)
        assert len(accepted) == 1 and process_registry.completion_queue.empty()
    finally:
        process_registry.completion_queue = original_queue
