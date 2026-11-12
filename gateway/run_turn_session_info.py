"""Session-info block (model/provider/context/endpoint) for auto-reset notices; inherited by GatewayTurnMixin."""
from __future__ import annotations

import dataclasses
from typing import Optional

from agent.i18n import t
from gateway.session import SessionSource
from utils import base_url_hostname


class GatewayTurnSessionInfoMixin:
    def _reset_notice_session_info(self, source: SessionSource) -> str:
        """Session-info block for the auto-reset notice, resolved inside the profile serving ``source``.

        Call via ``asyncio.to_thread``: resolution can block (credential refresh, context-length
        probes), and the scope is entered here so contextvars behave in the worker thread."""
        with self._profile_scope_for_source(source):
            return self._format_session_info(source)

    def _format_session_info(self, source: Optional[SessionSource] = None) -> str:
        """Model / provider / context-length / endpoint block so users can spot bad context detection.
        With ``source``, the chat's ``channel_overrides`` route is reported (what its turn runs)."""
        from gateway.run import _resolve_gateway_model_context
        from gateway.session_info_route import channel_override_route
        model, route, force_provider = channel_override_route(getattr(self, "config", None), source)
        resolved = _resolve_gateway_model_context(model=model, route=route)
        if force_provider:
            resolved = dataclasses.replace(resolved, provider=force_provider)
        context_length = resolved.context_length
        ctx_source = {
            "config": "config",
            "default": "fallback, not a detected limit — set model.context_length in config to override",
        }.get(resolved.context_source, "detected")
        ctx_display = (
            f"{context_length / 1_000_000:.1f}M" if context_length >= 1_000_000
            else f"{context_length // 1_000}K" if context_length >= 1_000 else str(context_length)
        )
        lines = [
            t("gateway.session.info_model", model=resolved.model),
            t("gateway.session.info_provider", provider=resolved.provider or "openrouter"),
            t("gateway.session.info_context", tokens=ctx_display, source=ctx_source),
        ]
        if (resolved.provider or "") == "moa":
            # The preset name hides who pays: the aggregator runs every tool-loop step (#112359).
            from hermes_cli.config import load_config
            from hermes_cli.moa_config import normalize_moa_config
            agg = normalize_moa_config(load_config().get("moa"))["presets"].get(resolved.model, {}).get("aggregator") or {}
            if agg:
                lines.append(t("gateway.session.info_acting_model", provider=agg.get("provider"), model=agg.get("model")))
        base_url = resolved.base_url
        if base_url and base_url_hostname(base_url) in ("localhost", "127.0.0.1", "0.0.0.0"):
            lines.append(t("gateway.session.info_endpoint", url=base_url))
        return "\n".join(lines)
