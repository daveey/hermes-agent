"""Shared adapter status for startup and reconnect recovery."""
import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

from gateway.config import Platform

logger = logging.getLogger("gateway.run_adapters")


def adapter_unavailable_message(platform: Platform, *, retrying: bool = True) -> str:
    """Keep the plugin/deps/credentials hint after the first reconnect retry."""
    message = (
        f"No adapter available for enabled {platform.value}; check the plugin, dependencies, and credentials."
    )
    return f"{message} Retrying in the background." if retrying else message


class GatewayAdapterStatusMixin:
    """Publish reconnect attention and fatal adapter status."""

    def _flag_reconnect_needs_attention(
        self, platform, info: dict, now: float, *, status_key: Optional[str] = None
    ) -> None:
        """Flag NEEDS_ATTENTION (once) past the threshold — a signal, NOT a circuit breaker. The threshold
        is the bound profile's ``agent.reconnect_attention_after``: secondaries call this inside their
        ``_profile_runtime_scope`` with their ``<profile>:<platform>`` status key."""
        from gateway.run import _reconnect_needs_attention
        if info.get("attention_flagged") or not _reconnect_needs_attention(info, now):
            return
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

    def _mark_platform_fatal(self, status_key: str, adapter) -> None:
        """Record an adapter's fatal error code/message as ``fatal`` runtime status."""
        self._update_platform_runtime_status(
            status_key, platform_state="fatal", error_code=adapter.fatal_error_code,
            error_message=adapter.fatal_error_message,
        )
