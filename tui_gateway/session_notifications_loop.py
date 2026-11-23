"""Loaded-session executor scheduling; callbacks only wake this existing loop.

Each poller is a generator of passes (``_notification_poller_*_steps``) that yields at its one queue
wait. f OFF drives it on the session's own thread (``_notification_poller_drive``); f ON advances every
session's generator on the shared cooperative executor (``notification_loaded_executor``)."""
from .method_ctx import bind_module


def _notification_poller_drive(steps, queue):
    """Blocking driver: performs each yielded wait with ``take_ready`` on the calling thread."""
    from tools import process_registry_awaiting
    try:
        request = next(steps)
    except StopIteration:
        return
    while True:
        timeout, wake_generation = request
        try:
            try:
                ready = process_registry_awaiting.take_ready(queue, timeout, wake_generation=wake_generation)
            except BaseException as exc:  # health: allow BLE001 -- rethrown into the pass
                request = steps.throw(exc)
            else:
                request = steps.send(ready)
        except StopIteration:
            return


def _notification_poller_run(stop_event, sid, session, process_registry, handle):
    _notification_poller_drive(_notification_poller_run_steps(stop_event, sid, session, process_registry, handle),
                               process_registry.completion_queue)


def _notification_poller_run_steps(stop_event, sid, session, process_registry, handle):
    from tui_gateway.notification_facts import for_home
    from tui_gateway.notification_wait import wake
    from tui_gateway import notification_loaded_f
    owner = for_home(session.get("profile_home"))
    # An explicitly bound lifecycle retains its own activation/rollback authority.
    action = (owner.add_action(lambda: wake(process_registry.completion_queue)) if owner else
              notification_loaded_f.register(session, lambda: wake(process_registry.completion_queue)))
    try:
        yield from _notification_poller_execute_steps(stop_event, sid, session, process_registry, handle, action)
    finally:
        if action is not None:
            action.close()


def _notification_poller_execute(stop_event, sid, session, process_registry, handle, action):
    _notification_poller_drive(
        _notification_poller_execute_steps(stop_event, sid, session, process_registry, handle, action),
        process_registry.completion_queue)


def _notification_poller_execute_steps(stop_event, sid, session, process_registry, handle, action):
    """One poller's passes. Yields ``(timeout, wake_generation)`` at its queue wait and is sent the events
    its driver then took (``take_ready``), already in in-flight custody; released in this pass's finally."""
    from hermes_cli import loop_accounting as _la
    from tools import async_delegation
    from tools.process_registry_awaiting import release as release_in_flight
    from tui_gateway import notification_park
    from tui_gateway.notification_remote_retry import deadline
    from tui_gateway.notification_wait import due_timeout, generation
    from tui_gateway.producer_admission import deliver_pending
    queue = process_registry.completion_queue
    last_kanban_poll = last_loop_poll = last_parked_poll = last_producer_poll = 0.0
    error_retry_at = None
    while not stop_event.is_set() and not session.get("_finalized"):
        # Sample BEFORE inspecting sources: a publication during the pass must
        # invalidate the ensuing wait, not disappear into a later sample.
        wake_generation = generation(queue)
        now = time.monotonic()
        _la.timed("notifications.sweep_orphaned_completions", async_delegation.maybe_sweep_orphaned_completions, queue)
        if now - last_loop_poll >= _LOOP_POLL_SECONDS:
            last_loop_poll = now
            for what, fire in (("loop wakeup", _maybe_fire_tui_loop_tick), ("heartbeat", _maybe_fire_tui_heartbeat_tick)):
                try:
                    _la.timed(f"notifications.{what.replace(' ', '_')}", fire, sid, session)
                except Exception as tick_exc:
                    logger.debug("notification tick failed", exc_info=True)
                    _notif_log_failure(f"{what} poll failed", tick_exc)
        last_kanban_poll, error_retry_at = _notification_kanban_tick(
            sid, session, action, now, last_kanban_poll, error_retry_at)
        # Trusted producer admission is independent of the notice-source gate.
        if now - last_producer_poll >= _KANBAN_POLL_SECONDS:
            last_producer_poll = now
            _la.timed("notifications.producer_deliver_pending", deliver_pending, sid, session)
        if now - last_parked_poll >= _PARKED_POLL_SECONDS:
            last_parked_poll = now
            _la.timed("notifications.take_parked", _notif_take_parked, sid, session, process_registry, handle)
        if session.get("_notif_held") and not session.get("running") and not session.get("_turn_cancel_requested"):
            taken = _notif_take_held(session)
            try:
                handle(taken, None)
            except Exception as exc:
                logger.debug("held notification failed", exc_info=True)
                _notif_log_failure("held notification dispatch failed", exc)
            finally:
                release_in_flight(taken)
                _notif_unhold(taken)
        parked = (notification_park.claim(lambda evt: _session_owns_notification_event(sid, session, evt))
                  if notification_park.has_parked() else [])
        ready = []
        try:
            deadlines = [last_loop_poll + _LOOP_POLL_SECONDS, last_producer_poll + _KANBAN_POLL_SECONDS]
            if error_retry_at is not None:
                deadlines.append(error_retry_at)
            if action is None or not action.facts:
                deadlines.append(last_kanban_poll + _KANBAN_POLL_SECONDS)
            from tui_gateway.notification_loaded_f import Action
            if isinstance(action, Action):
                if (action_at := action.deadline(time.monotonic(), last_kanban_poll)) is not None:
                    deadlines.append(action_at)
            if (retry_at := deadline(session, time.monotonic())) is not None:
                deadlines.append(retry_at)
            has_registry_park = getattr(process_registry, "has_parked_events", lambda: True)
            if has_registry_park() or notification_park.has_parked():
                deadlines.append(last_parked_poll + _PARKED_POLL_SECONDS)
            timeout = due_timeout(time.monotonic(), deadlines, session, stop_event)
            ready = yield (0 if parked or stop_event.is_set() else timeout, wake_generation)
            if not parked and not ready:
                continue
            handle(parked + ready, None)
        except Exception as exc:
            logger.debug("notification dispatch failed", exc_info=True)
            _notif_log_failure("notification dispatch failed", exc)
        finally:
            release_in_flight(ready)
            release_in_flight(parked)
            notification_park.unhold(parked)


def _notification_kanban_tick(sid, session, action, now, last_poll, error_retry_at):
    from hermes_cli import loop_accounting as _la
    from tui_gateway.notification_remote_retry import cancel, deadline
    from tui_gateway.notification_loaded_f import Action
    if isinstance(action, Action):
        return action.tick(sid, now, last_poll, error_retry_at, _notif_poll_kanban)
    legacy_due = now - last_poll >= _KANBAN_POLL_SECONDS
    due = action.due(legacy_due=legacy_due) if action is not None else legacy_due
    retry_at = deadline(session, now)
    remote_only = False
    if due or (error_retry_at is not None and now >= error_retry_at):
        last_poll = now
    elif retry_at is not None and now >= retry_at:
        cancel(session)
        remote_only = True
    else:
        return last_poll, error_retry_at
    try:
        if remote_only:
            _la.timed("notifications.poll_kanban_remote_retry", _notif_poll_kanban, sid, session, remote_only=True)
        else:
            _la.timed("notifications.poll_kanban", _notif_poll_kanban, sid, session)
            error_retry_at = None
    except Exception as exc:
        # A failed source/action pass cannot kill the sole notification executor.
        # This obligation is independent of the remote index-defer cancellation.
        logger.debug("notification kanban pass failed", exc_info=True)
        _notif_log_failure("kanban notification pass failed", exc)
        error_retry_at = now + 1.0
    return last_poll, error_retry_at


def register(server):
    bind_module(globals(), server)
