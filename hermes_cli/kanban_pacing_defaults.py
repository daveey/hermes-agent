"""``kanban.llm_pacing`` defaults: the LLM quota pacing governor (Kanban claim time only). [daveey]

Import-free so ``config_defaults`` can embed them; the governor's ``PacingConfig`` keeps the same values (a test
pins the two together). Before claiming a card the dispatcher holds it when its provider's OAuth pool
(``five_hour``/``seven_day`` usage windows) burns fast enough to pass ``target_percent`` before a window resets
(paced), or every account is past its reserve (held). It fails open when no account reads.

- ``reserve_*_percent``: past this an account's window is left to chats, cron and notices.
- ``providers``: governed providers; cards on any other provider always start.
- ``read_interval_seconds``: usage read cadence per Hermes home. ``rate_window_seconds``: burn-rate and
  average-concurrency lookback. ``min_rate_span_seconds``: shortest sample span a burn slope is measured over.
- ``deferrable_share``: share of a paced limit review-lane, negative-priority and woken cards may use.
"""

DEFAULTS = {
    "enabled": True,
    "target_percent": 95,
    "reserve_five_hour_percent": 80,
    "reserve_seven_day_percent": 85,
    "providers": ["anthropic", "openai-codex", "xai-oauth"],
    "read_interval_seconds": 300,
    "rate_window_seconds": 3600,
    "min_rate_span_seconds": 600,
    "deferrable_share": 0.5,
}
