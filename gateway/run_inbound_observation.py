"""Bookkeeping after runner admission, before intercepts or busy dispatch."""


def observe_user_input(runner, event):
    from hermes_cli.observability.shared_metrics_events import record_gateway_slash_command
    from gateway.process_consumer_health import invalidate
    invalidate(runner, runner._session_key_for_source(event.source))
    record_gateway_slash_command(event)
