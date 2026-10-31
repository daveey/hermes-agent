"""Defaults for per-request retry and local DNS-outage recovery."""

AGENT_RETRY_DEFAULTS = {
    # Hermes attempts wrapping each model call (SDK retries are additional). 1 = fast failover.
    "api_max_retries": 3,
    # After attempts AND fallback are spent on transient failures, allow this many recovery cycles.
    # Uses jittered 15/30/60/60/60s waits; provider Retry-After wins up to 120s. 0 disables.
    "auto_recovery_cycles": 5,
    # DNS fails for provider AND control host: wait this many awake seconds per model request,
    # without spending attempts or credential recovery. Shows "offline — waiting for network".
    # Interrupt stops the wait. 0 disables. Working DNS leaves ordinary provider retries intact.
    "offline_wait_seconds": 900,
}
