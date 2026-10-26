"""Native gateway process watcher lifecycle and completion delivery."""
import asyncio
import logging

logger = logging.getLogger("gateway.run")


class GatewayProcessWatcherMixin:
    async def _run_process_watcher(self, watcher):
        from tools.process_registry import process_registry
        from gateway.process_consumer_health import watching
        with watching(self, watcher, process_registry):
            await self._run_process_watcher_body(watcher)

    async def _run_process_watcher_body(self, watcher: dict) -> None:
        """Poll a background process and push updates until it exits. Mode
        (``display.background_process_notifications``): concise (default one-liner; failures append
        the output tail) / all (running updates + final raw) / result (final raw) / error (final raw
        if exit != 0) / off."""
        from tools.process_registry import process_registry
        from tools.process_registry_notifications import format_process_notification, should_surface_notification
        session_id = watcher["session_id"]
        interval = watcher["check_interval"]
        platform_name = watcher.get("platform", "")
        chat_id = watcher.get("chat_id", "")
        thread_id = watcher.get("thread_id", "")
        agent_notify = watcher.get("notify_on_complete", False)
        # The mode belongs to the profile that started the process; recovered watchers run in the
        # root context, so resolve it under the owning profile's scope (no-op for the default).
        async with self._completion_event_scope(watcher):
            notify_mode = self._load_background_notifications_mode()
        logger.debug("Process watcher started: %s (every %ss, notify=%s, agent_notify=%s)",
                      session_id, interval, notify_mode, agent_notify)
        silent = notify_mode == "off" and not agent_notify
        last_output_len = 0
        while True:
            await asyncio.sleep(interval)
            session = process_registry.get(session_id)
            if session is None:
                break
            async with self._completion_event_scope(watcher):
                surface = should_surface_notification({
                    "owner_task_id": getattr(session, "owner_task_id", ""),
                    "task_id": getattr(session, "task_id", ""),
                })
            if not surface:
                # Recheck live ownership next tick: handoff retains notification intent.
                if session.exited:
                    break
                continue
            if silent:
                # Still wait for the process to exit so we can log it, but don't push any messages.
                if session.exited:
                    break
                continue
            current_output_len = len(session.output_buffer)
            has_new_output = current_output_len > last_output_len
            last_output_len = current_output_len
            if session.exited:
                # Agent-notify: inject a synthetic message unless the agent already consumed the result via
                # wait/log (poll() is read-only and deliberately does NOT mark consumed).
                if agent_notify and not process_registry.is_completion_consumed(session_id):
                    completion_evt = self._build_process_completion_event(watcher, session, session_id)
                    synth_text = format_process_notification(completion_evt)
                    if not synth_text:
                        break
                    # Captured before injection: afterwards the key is busy either way (the injected
                    # turn itself installs the guard).
                    turn_busy = await self._launching_turn_active(platform_name, watcher)
                    delivered = await self._enqueue_process_completion_notification(synth_text, completion_evt)
                    if delivered is False:
                        # The process remains terminal; retry after failed adapter injection instead
                        # of suppressing the result.
                        continue
                    # The agent normally reports the result itself, so the chat gets no separate receipt.
                    # While the launching turn is still running the injection only queues a follow-up, and
                    # the chat would stay mute for as long as that turn lasts (#112033): send the concise
                    # receipt now.
                    if turn_busy and (notify_mode in {"concise", "all", "result"} or (
                        notify_mode == "error" and session.exit_code not in {0, None}
                    )):
                        message_text = self._format_process_final_message(session_id, session, "concise")
                        await self._send_watcher_message(platform_name, chat_id, thread_id, message_text, watcher)
                    break
                # Text-only notification; skip when already consumed via wait/log (the agent_notify branch
                # FALLS THROUGH here, hence the re-check).
                if process_registry.is_completion_consumed(session_id):
                    logger.debug(
                        "Process watcher: completion for %s already consumed "
                        "via wait/log — skipping raw notification (#65379)", session_id,
                    )
                    break
                if notify_mode in {"concise", "all", "result"} or (
                    notify_mode == "error" and session.exit_code not in {0, None}
                ):
                    message_text = self._format_process_final_message(session_id, session, notify_mode)
                    from gateway.warning_notifications import present_notification
                    async with self._completion_event_scope(watcher):
                        # Non-zero exit is the automatic diagnostic; a clean completion is the requested result.
                        await present_notification(
                            lambda: self._send_watcher_message(platform_name, chat_id, thread_id, message_text, watcher),
                            platform=platform_name, diagnostic=session.exit_code not in {0, None})
                break
            elif has_new_output and notify_mode == "all" and not agent_notify:
                # New output — deliver a status update (only in "all" mode; agent_notify watchers
                # only care about completion).
                await self._send_watcher_message(
                    platform_name, chat_id, thread_id, self._format_process_running_message(session), watcher,
                )
        logger.debug("Process watcher ended%s: %s", " (silent)" if silent else "", session_id)
