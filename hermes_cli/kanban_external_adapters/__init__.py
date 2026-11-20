"""Watch adapters: one poll of an external system -> ``{job, service, detail, url, retry_after}``. [daveey]

``github`` reads a GitHub Actions run or a PR's checks (wrapping ``kanban_ci_progress``); ``shell`` maps a legacy
wake-check probe's exit code and optional ``HERMES_WATCH {json}`` line. The state machine that turns polls into a
card state is ``hermes_cli.kanban_external_state``; the database side is ``hermes_cli.kanban_external_watch``.
"""
