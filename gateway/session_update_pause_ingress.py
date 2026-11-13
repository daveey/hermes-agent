"""Production binding of the update-pause scope for canonical in-process admissions."""
from contextlib import contextmanager, nullcontext
from dataclasses import replace


def supported(authority, row, event, live, *, is_api):
    """Only a claimed native messaging or local chat turn can be checkpointed, including a local
    route's producer wake (below). API and noted-completion admissions have other result contracts
    and keep counting as active work.
    Native notices retain their presentation traits on resume, but must pass current ingress
    authorization again; a formerly internal wake is never a license to bypass Stop or policy.

    [daveey] A finite local turn (``chat -q`` / ``-z``: the hub's ``oneshot`` reviewer chats) parks too:
    its admission keeps the finite flags, so the resumed run is finite again, and its viewer redials
    the restarted owner for the same admission's settlement (``gateway_chat_view``). Unparkable, it
    refused every instant checkpoint for as long as it ran (hermes-hub 2026-11-12 15:09-15:40Z).
    """
    payload = row['payload']
    local = getattr(getattr(live.source, 'platform', None), 'value', None) == 'local'
    if is_api or (payload.get('finite') is True and not local) or 'api_turn_v1' in payload:
        return False
    if 'local_automation_v1' in payload:
        # [daveey] A producer wake on a local route (Kanban notice, /loop tick, plugin injection,
        # heartbeat) runs as a turn and resumes like a local chat turn: the claim re-checks its route
        # (``check_local_automation``) and ``resume_event`` keeps its mute/heartbeat traits only.
        # Unparkable, a notice turn held a restart-safe drain for its whole claim lease (David-MBP
        # 2026-11-12 06:03/07:16 PST). Not a noted completion: at an unviewed resume
        # ``note_if_unviewed`` would write it as a note instead of continuing. Not a detached
        # route's wake: its resume is not covered here.
        descriptor = payload['local_automation_v1']
        if not (isinstance(descriptor, dict) and isinstance(descriptor.get('identity'), str)):
            return False  # malformed: the claim refuses it anyway, never park it
        from gateway.session_completion_notes import noted_completion
        return local and not noted_completion(row)
    return 'native_text_v1' in payload or local


@contextmanager
def pause_scope(authority, row, event, live, *, is_api):
    from gateway.session_tui_ops import _profile_home
    from gateway.session_update_pause_hook import bind_scope
    from gateway.session_display import display_turn_scope
    # Presentation is trusted independently of ingress policy. Never mark a resumed
    # event internal merely to render a notice: that would bypass authorization.
    display = (display_turn_scope(event.message_id, {'kind': 'auto_continue'})
               if row.get('update_resume') else nullcontext())
    from gateway.session_cli_turn import cli_turn_scope
    # Resume the admitted options, not the original model-facing user message.
    committed = {key: value for key, value in row['payload'].get('cli_turn_v1', {}).items()
                 if key != 'api_text'}
    cli = cli_turn_scope(event.message_id, committed) if row.get('update_resume') else nullcontext()
    with display, cli, bind_scope(_profile_home(authority), eligible=supported(authority, row, event, live, is_api=is_api),
                             resuming=bool(row.get('update_resume')), message_id=event.message_id,
                             claim_held=claim_hold_gate(row)):
        yield


def claim_hold_gate(row):
    """[daveey] t_01a1d49d50e4c656: the claim's own hold test for this row's class, re-asked at turn start (None:
    the claim never holds the row). A notifier wake waits queued under a core-update claim hold
    (``session_notifier_drain.eligible``); one claimed before the hold may start long after it (a session turn
    lease wait), so its turn re-asks and, held, the admission goes back to its FIFO place (``requeue_unstarted``).

    [daveey] t_01a1d4d4c30c7e43: installed whether or not the scope is ``supported`` (``installed`` then sets only
    this gate). An unsupported notifier wake (a detached route's ``local_automation_v1`` wake) is not exempt from
    the drain: ``active_work`` lists its turn as a chat unit with ``update_pausable`` False, which the reclaim
    preflight never waits out (hermes-sync ``_parkable_gateway_chat``) and refuses as ``unmatched gateway work``.
    The re-queue writes no checkpoint, so it needs no checkpoint-capable scope. ``noted_completion`` rows are no
    notifier wakes (their producers are never ``kanban-wake``). A managed-worker session never binds a scope
    (``admit_managed`` runs before ``pause_scope``): out of scope here."""
    from gateway import session_notifier_drain
    if row.get('update_resume') or not session_notifier_drain.notifier_wake(row):
        return None
    return lambda: session_notifier_drain.held_noted()


def retain_pause(authority, row, result, captured, *, native):
    """Keep nonterminal state until the fully unwound handler can be checkpointed."""
    from gateway.session_update_pause_hook import is_paused
    if not is_paused(result):
        return False
    if native:
        authority.native_waiters.add(row['admission_id'])
    authority.pending_results[row['admission_id']] = {
        'result': result, 'usage': captured.get('usage', {})}
    return True


def resume_event(row, event):
    """The next model action after a checkpoint: trusted shared pause note, same route/provenance,
    and none of the original message's attachments, platform id or control authority."""
    if not row.get('update_resume'):
        return event
    from tui_gateway.update_pause import pause_note
    from gateway.warning_notifications import diagnostic_metadata, diagnostic_wake_muted
    heartbeat = getattr(event, '_heartbeat_session_id', None)
    # Trusted traits are read from the verified original before it is demoted: dataclasses.replace
    # drops private non-field state, and internal=False alone would unmute a diagnostic wake.
    muted = diagnostic_wake_muted(event) if (event.internal or heartbeat) else None
    # Trusted automation = gateway-stamped internal wake or heartbeat poll, never human content.
    automation = bool(event.internal or heartbeat)
    # Read before demotion: run_turn derives the turn mute from this persisted category, and only an
    # internal event may carry it, so the verified fact is stamped privately instead of keeping internal.
    diagnostic = bool(diagnostic_metadata(event))
    resumed = replace(event, text=pause_note(event.text if isinstance(event.text, str) else ''),
                   # Admission-bound, never the platform id or None: display scope matches on this id,
                   # so a None-id follow-up on the same task must not inherit auto_continue.
                   message_id=f"update-resume:{row['admission_id']}",
                   # '' (not None) wins _reply_anchor_for_event: the synthetic id is no platform message.
                   ledger_message_id=None, reply_anchor_override='',
                   platform_update_id=None, media_urls=[], media_types=[], media_text_inlined=[],
                   media_filenames=[], reply_to_message_id=None, reply_to_text=None,
                   reply_to_author_id=None, reply_to_author_name=None, reply_to_is_own_message=False,
                   prompt_response=None, auto_skill=None, channel_context=None, internal=False,
                   allow_gateway_control=False, metadata={}, edit_revision=None,
                   # Unaddressed machinery keeps its bare-silence contract under the auto_continue
                   # display kind; a human event keeps whatever reply_expected the adapter knew.
                   **({'reply_expected': False} if automation else {}))
    if diagnostic:
        resumed._trusted_diagnostic_wake = True
    if heartbeat:
        resumed._heartbeat_session_id = heartbeat  # the owner/lineage check still applies
    if muted is not None:
        resumed._notification_reply_muted = muted
    return resumed
