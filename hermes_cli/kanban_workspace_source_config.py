"""``kanban.workspace_source`` defaults (workspace_source v1). Import-free so ``config_defaults`` can embed them.

- ``enabled``: portable worktree sources (default off; opt in per machine). Honoured since core PR-2
  completed the protocol (``kanban_workspace_source.SOURCE_PROTOCOL_COMPLETE``).
- ``clones``: ``{"<owner>/<name>": "<abs path>"}`` dedicated clones (never the live checkout), proven at gateway start
  and on every registry heartbeat and attested to the Board.
- ``publish_on_park``: publish the task branch when a run parks (schedule/block/continue/review), not only on complete.
- ``push_timeout_s``: bound on the publish push. ``preflight_timeout_s``: bound on an ending's receipt preflight
  (``None`` = ``push_timeout_s + 30``). ``materialize_timeout_s``: bound on resolving and materializing a source before
  spawn (``None`` = lease ttl / 3 - 30 s); past it the host refuses (``materialize_timeout``) and skips that revision.
"""

DEFAULTS = {
    "enabled": False,
    "clones": {},
    "publish_on_park": True,
    "push_timeout_s": 30,
    "preflight_timeout_s": None,
    "materialize_timeout_s": None,
}
