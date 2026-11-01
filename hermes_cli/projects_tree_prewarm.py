"""Boot-time build of the all-profiles project tree, so the Desktop's reconnect burst after a backend restart does not
pay this process's cold tree costs under GIL contention.

``GET /api/profiles/projects/tree`` builds one tree per profile. The first build in a new process pays costs that every
later build reuses: the lazy ``tui_gateway.server`` import (~0.5 s), one ``git`` root probe per session cwd
(``git_probe`` caches positives for the process), the per-profile kanban annotation scopes and the cold SQLite pages
of eleven ``state.db`` files. Uncontended on hermes-hub that is ~3 s cold against ~0.15-0.4 s warm; inside the
reconnect burst after the 22:25Z 2026-11-01 restart it took 4.02 s, past the Desktop's 4-5 s request budget
(t_01a19864fc42f5b2).

Runs after the annotation prewarm on the same daemon thread (one extra boot thread, not two), through the route's own
single-flight cache, so a Desktop poll that arrives mid-build waits for it and then reads warm caches instead of
starting a second cold scan. Every backend that serves the sidebar does this build on its first tree poll anyway; the
prewarm only moves it ahead of the burst. [daveey]
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def prewarm_project_tree() -> bool:
    """Build the all-profiles project tree once through the route. Never raises."""
    try:
        from hermes_cli.web_routers import profiles

        profiles.get_profiles_projects_tree()
        return True
    except Exception:  # health: allow BLE001 -- best-effort warm-up; the first real read reports its own error
        log.debug("project tree prewarm failed", exc_info=True)
        return False
