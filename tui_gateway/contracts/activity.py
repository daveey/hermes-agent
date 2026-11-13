"""Quiet-activity classification of JSON-RPC methods.

Only methods named here are PASSIVE (read-only list/status/capability polling that
never produces agent, model, tool or write work; Desktop polls them continuously, so
reserving for them would make every quiet episode invalid). They are never suppressed
or gated, only left unreserved and counted as CPU. Everything else, including every
method not named here and any name unknown to the catalog, is WORK: the dispatcher
reserves before invoking it. Add a method here only after reading its handler.
"""

from __future__ import annotations

PASSIVE_METHODS = frozenset({
    "agents.list", "approval.pending", "billing.state", "client.capabilities",
    "commands.catalog", "complete.path", "complete.slash", "config.get", "config.show",
    "connectors.accounts", "connectors.catalog", "connectors.list",
    "connectors.operation.status", "connectors.policy.get", "connectors.tools",
    "delegation.status", "diagnostics.account_usage_cache", "display.status",
    "free_tier.status", "gateway.capabilities", "groups.capabilities", "groups.context",
    "groups.list", "groups.log", "groups.replica_state", "groups.state", "handoff.state",
    "i18n.catalog", "i18n.languages", "insights.get", "machine.facts", "mcp.catalog",
    "mcp.servers.list", "mcp.servers.oauth.poll", "mcp.servers.status", "model.options",
    "onboarding.state", "pet.cells", "pet.gallery", "pet.generate.status", "pet.info",
    "pet.info.meta", "pet.thumb", "ping", "plugins.list", "process.list", "profiles.describe",
    "profiles.get_asset", "profiles.list", "project.facts", "projects.get", "projects.list",
    "projects.project_sessions", "projects.tree", "prompt.admission.get", "rollback.list",
    "session.active_list", "session.context_breakdown", "session.control.read",
    "session.events.since", "session.events.stats", "session.history", "session.list",
    "session.most_recent", "session.status", "session.usage", "setup.runtime_check",
    "setup.status", "shared_metrics.status", "spawn_tree.list", "spawn_tree.load",
    "subagent.list", "subagent.roster", "subagent.tail", "subscription.state",
    "system.battery", "tools.list", "tools.show", "toolsets.list", "usage.bars",
    "vault.list", "vault.sources", "verification.status", "wake.status",
})


def is_passive(method) -> bool:
    return isinstance(method, str) and method in PASSIVE_METHODS


def is_work(method) -> bool:
    """Unclassified or unknown => work."""
    return not is_passive(method)
