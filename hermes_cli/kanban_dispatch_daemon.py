"""Standalone dispatch loop, outside startup/signal handling. [daveey]"""
import contextlib
import traceback


def run_loop(stop_event, interval, max_spawn, failure_limit, on_tick):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_db_connect as connect

    while not stop_event.is_set():
        try:
            # Config is mtime-cached but refreshed each tick, as before extraction.
            maximum = dispatch.resolve_max_in_progress(dispatch.configured_max_in_progress())
            with contextlib.closing(connect.connect()) as conn:
                result = dispatch.dispatch_once(conn, max_spawn=max_spawn,
                                                max_in_progress=maximum, failure_limit=failure_limit)
            if on_tick is not None:
                with contextlib.suppress(Exception):
                    on_tick(result)
        except Exception:  # health: allow BLE001 -- daemon tick boundary preserves traceback and continues service
            traceback.print_exc()
        stop_event.wait(timeout=interval)
