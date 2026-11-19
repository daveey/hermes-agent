"""``kanban.llm_pacing`` defaults: the LLM quota pacing governor (Kanban claim time only). [daveey]

Import-free so ``config_defaults`` can embed them; the governor's ``PacingConfig`` keeps the same values (a test
pins the two together). Before claiming a card the dispatcher holds it while every account of its provider's OAuth
pool (``five_hour``/``seven_day`` usage windows) is past its reserve (at reserve). It fails open when no account
reads. The pool's burn against ``target_percent`` (open/paced) is still judged and stored but holds nothing.

- ``reserve_*_percent``: past this an account's window is left to chats, cron and notices.
- ``providers``: governed providers; cards on any other provider always start.
- ``read_interval_seconds``: usage read cadence per Hermes home. ``rate_window_seconds``: burn-rate lookback.
  ``min_rate_span_seconds``: shortest sample span a burn slope is measured over.
- Capacity model (``llm_pacing.json`` ``v2``; a token throttle enforces it, core does not):
  ``margin_seconds``: each account is planned to reach its reserve this long before its weekly reset.
  ``history_days``: retention of ``llm_pacing_history.sqlite``. ``tpp_min_delta_percent`` /
  ``tpp_window_seconds`` / ``tpp_half_life_seconds``: the tokens-per-percent estimate needs this summed percent
  delta within this lookback (and at least 2 h), smoothed with this half-life. ``tokens_per_percent``: seeds,
  ``{provider: {seven_day|five_hour: tokens}}``, used until a rate is measured. ``budget_publish``: this host
  publishes the budget to the token throttle (read by the hermes-mods throughput plugin; the hub only).
- Ratio routing (``kanban_ratio_routing``): ``ratio_routing`` starts each unpinned default-tier card on the governed
  pool of ``kanban.default_model``'s routing class furthest below its ``v2`` budget share (``ratio``);
  ``ratio_route_models``: ``{pool: model}``, the route each pool runs (default: its first route in the registry).
- The per-host start limit (``pace_starts``, ``deferrable_share``) is removed: 2026-11-17 with both pools ``paced``
  every host was cut to one run per provider, 3 of 18 slots busy and 130 cards waiting, while the pools still had
  over two account-weeks left. How many cards run is the worker slots' business, how many tokens they burn a token
  throttle's.
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
    "margin_seconds": 10800,
    "history_days": 8,
    "tpp_min_delta_percent": 5,
    "tpp_window_seconds": 86400,
    "tpp_half_life_seconds": 43200,
    "tokens_per_percent": {},
    "budget_publish": False,
    "ratio_routing": True,
    "ratio_route_models": {},
}
