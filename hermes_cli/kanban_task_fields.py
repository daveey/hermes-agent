"""Task row decoding groups, shared by the kanban_db compatibility facade."""

# Columns every schema version has (KeyError if the SELECT omitted them).
REQUIRED = (
    "id", "title", "body", "assignee", "status", "priority", "created_by",
    "workspace_kind", "workspace_path", "claim_lock",
)
EPOCH = ("created_at", "started_at", "completed_at", "claim_expires", "last_heartbeat_at")
# Later-added columns read as NULL when absent from the row.
OPTIONAL = (
    "branch_name", "project_id", "tenant", "result", "idempotency_key", "worker_pid",
    "max_runtime_seconds", "current_run_id", "workflow_template_id", "containment_episode",
    "current_step_key", "max_retries", "session_id", "completion_contract", "placement_policy", "model_pin_mandatory",
)
# Text columns where "" is stored/read as "not set".
EMPTY_IS_NULL = (
    "model_override", "provider_override", "reasoning_effort", "goal_max_turns", "block_kind",
)
