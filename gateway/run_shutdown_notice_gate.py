"""Whether a restart/shutdown notice may go to a platform: the ``gateway_restart_notification`` gate.

Split out of ``gateway/run_shutdown.py``; bound onto ``GatewayRunner`` via ``GatewayShutdownMixin``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from gateway.config import Platform, restart_notification_enabled_on_disk

logger = logging.getLogger("gateway.run")


class GatewayShutdownNoticeGateMixin:
    """``gateway_restart_notification`` checks for shutdown/cron-interrupt notices."""

    def _restart_notification_allowed(self, platform: Platform) -> bool:
        """False when the platform config sets ``gateway_restart_notification=false``."""
        platform_cfg = self.config.platforms.get(platform)
        return platform_cfg is None or bool(platform_cfg.gateway_restart_notification)

    def _profile_config_home(self, profile: Optional[str]) -> Optional[Path]:
        """The HERMES_HOME whose config.yaml configures *profile* (``None`` = the launch profile)."""
        if profile is None:
            from gateway.run import _hermes_home
            return _hermes_home
        return (getattr(self, "_served_profile_homes", None) or {}).get(profile)

    def _notice_allowed(self, platform: Platform, what: str, platform_cfg=None, *, profile_home=None) -> bool:
        """``_restart_notification_allowed`` with the INFO suppression line for shutdown notices.

        ``platform_cfg`` is a SERVED profile's own platform entry; ``self.config`` is the launch profile's.
        With ``profile_home`` the flag is re-read from that profile's config.yaml: the cached config is
        the boot-time one, and an opt-out written since must already silence this shutdown. [daveey]
        """
        allowed = (self._restart_notification_allowed(platform) if platform_cfg is None
                   else bool(platform_cfg.gateway_restart_notification))
        if profile_home is not None:
            allowed = restart_notification_enabled_on_disk(profile_home, platform, allowed)
        if allowed:
            return True
        logger.info(
            "Shutdown notification suppressed for %s: %s has gateway_restart_notification=false", what, platform.value,
        )
        return False
