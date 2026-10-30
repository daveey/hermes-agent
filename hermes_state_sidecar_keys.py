"""The ``sessions.model_config`` keys a managed worker may write on its own session row.

One registry for both sides of the worker boundary: the owner validates ``session.sidecars`` and a
compaction ``model_config_patch`` against it (``hermes_state_worker_context``,
``hermes_state_worker_compression``), and the worker's ``RuntimeSessionStore`` checks a patch before
journaling it, so an unregistered key fails that one call instead of poisoning the receipt journal.

Every key the agent writes through ``patch_session_model_config`` / ``update_conversation_model_config_value``
must be listed here; ``tests/hermes_state/test_sidecar_keys_registry.py`` scans the tree and fails on drift.
Identity and lineage markers stay owner-reserved and must never be added.
"""

SIDECAR_KEYS = frozenset({
    '_usage_anchor',                  # agent/usage_anchor.py
    '_proactive_prune_rearm_tokens',  # agent/context_compressor.py (+ compaction model_config_patch)
    '_anthropic_rejected_thinking',   # agent/anthropic_thinking_replay.py
    '_fallback_probe_at',             # agent/context_compressor_prellm.py
    'model_routing',                  # agent/model_routing_agent.py (class failover / clear)
    'prompt_refresh_epoch',           # agent/prompt_refresh.py
    'reasoning_rejected_carriers',    # agent/message_sanitization.py
    'onboarding_run',                 # agent/onboarding_record.py
    'codex_thread_id',                # agent/codex_runtime.py
})

# Lineage/identity markers only the owner writes (session creation, branch, delegate reservation).
OWNER_RESERVED_KEYS = frozenset({'_delegate_from', '_branched_from'})
