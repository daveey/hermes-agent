"""Shared adapter status for startup and reconnect recovery."""
import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

logger = logging.getLogger("gateway.run_adapters")


class GatewayAdapterStatusMixin:
    """Publish reconnect attention and fatal adapter status."""

    def _flag_reconnect_needs_attention(
        self, platform, info: dict, now: float, *, status_key: Optional[str] = None
    ) -> bool:
        """Flag NEEDS_ATTENTION (once) past the threshold — a signal, NOT a circuit breaker. The threshold
        is the bound profile's ``agent.reconnect_attention_after``: secondaries call this inside their
        ``_profile_runtime_scope`` with their ``<profile>:<platform>`` status key. True = flagged now."""
        from gateway.run import _reconnect_needs_attention
        if info.get("attention_flagged") or not _reconnect_needs_attention(info, now):
            return False
        info["attention_flagged"] = True
        queued_for = now - info.get("queued_at", now)
        logger.warning(
            "%s has been failing/reconnecting continuously for %.1f hours (%d attempts) — flagging "
            "NEEDS_ATTENTION. Retries continue, but this usually means a permanent problem (revoked "
            "credentials, missing intents, broken sidecar). Check `hermes status` / `/platform list`.",
            status_key or platform.value, queued_for / 3600.0, info.get("attempts", 0),
        )
        self._update_platform_runtime_status(
            status_key or platform.value, platform_state="retrying", needs_attention=True,
            retrying_since=(datetime.now(UTC) - timedelta(seconds=queued_for)).isoformat(),
        )
        return True

    def _mark_platform_fatal(self, status_key: str, adapter) -> None:
        """Record an adapter's fatal error code/message as ``fatal`` runtime status."""
        self._update_platform_runtime_status(
            status_key, platform_state="fatal", error_code=adapter.fatal_error_code,
            error_message=adapter.fatal_error_message,
        )
