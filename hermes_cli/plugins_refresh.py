"""Session-boundary plugin refresh for long-lived processes. [daveey]

A ``PluginManager`` discovers once: ``discover_and_load`` returns early once ``_discovered`` is set.
``hermes plugins enable`` nudges only the host-level serve backend (its rendezvous record) and the
gateway's control socket, so a per-profile ``hermes serve --isolated`` backend (a Desktop chat over
SSH) that booted before the enable never loaded the plugin until it restarted: nine every-host
guards enabled on the hub on 10-30 stayed out of all ten per-profile backends.

:func:`refresh_if_inputs_changed` closes that gap where it is safe to: when a long-lived process
builds the agent for a NEW session (``tui_gateway`` ``_make_agent``, the gateway's
``_resolve_turn_agent`` before a fresh build), never mid-turn. It compares the discovery inputs against those the last
sweep read and, only when they differ, runs the same forced pass ``load_and_go_live`` runs, under
the same ``_GO_LIVE_LOCK`` + ``_discovery_lock``. Cost when nothing changed: one
``plugins_snapshot.precheck`` (stat of config.yaml, .env and each top-level plugin dir); the full
``fingerprint`` (~15-20 ms on the hub's plugin tree) runs only when the precheck moved.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def refresh_if_inputs_changed(manager: Any) -> bool:
    """Force-rediscover ``manager``'s plugins when their inputs changed since its last sweep. True when
    a forced pass ran; False when the manager never discovered (its first lazy discovery reads the
    current inputs anyway), the inputs are unchanged, or they could not be read."""
    from hermes_cli import plugins_snapshot
    from hermes_cli.plugins_loader import in_plugin_load_worker

    if not manager._discovered or manager._swept_fingerprint is None or in_plugin_load_worker():
        return False
    home = manager.home_path
    pre = plugins_snapshot.precheck(home)
    if pre is not None and pre == manager._swept_precheck:
        return False
    stamp = plugins_snapshot.fingerprint(home)
    if stamp is None:
        return False
    if stamp == manager._swept_fingerprint:
        manager._swept_precheck = pre  # e.g. a __pycache__ dir appeared: nothing discovery reads moved
        return False
    from hermes_cli.plugins import _join_background_discovery
    from hermes_cli.plugins_activation import _GO_LIVE_LOCK

    _join_background_discovery()
    with _GO_LIVE_LOCK, manager._discovery_lock:
        if manager._swept_fingerprint == stamp:
            return False  # another session's build swept these inputs while we waited
        logger.info("plugin inputs for %s changed since the last discovery; reloading plugins for a new "
                    "session", home)
        manager.discover_and_load(force=True)
    return True


def refresh_plugins_for_new_session() -> bool:
    """:func:`refresh_if_inputs_changed` for the active home's manager; never raises (an agent build
    must not fail on it). Call it before building a NEW session's agent, never mid-turn."""
    try:
        from agent.safe_worker_policy import safe_worker_enabled
        from hermes_cli.plugins import get_plugin_manager
        if safe_worker_enabled():
            return False
        return refresh_if_inputs_changed(get_plugin_manager())
    except Exception:
        logger.warning("plugin refresh before a new-session agent build failed", exc_info=True)
        return False
