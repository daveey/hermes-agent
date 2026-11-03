"""Capsule-aware pieces of agent construction (:mod:`agent.agent_init`). [daveey]"""


def select_kanban_guidance(valid_tool_names) -> str:
    """The Kanban protocol for this agent's system prompt ("" when it owns no task).

    [daveey] A capsule-bound worker (bound before construction by agent.observation_bind) gets the capsule
    protocol: its only workspace is /workspace and the host publishes, so the generic host-workspace and
    completion lines would be false there."""
    from agent.delegation_context import owned_kanban_task
    from agent.prompt_builder import KANBAN_GUIDANCE
    if not (owned_kanban_task() and "kanban_show" in valid_tool_names):
        return ""
    from tools import capsule_workspace
    if capsule_workspace.bound():
        from agent.prompt_builder import kanban_capsule_guidance
        return kanban_capsule_guidance()
    return KANBAN_GUIDANCE
