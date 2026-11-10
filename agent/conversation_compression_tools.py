"""Tool-schema refresh at the admitted compaction boundary."""
import logging

logger = logging.getLogger("agent.conversation_compression")


def refresh_agent_tool_definitions(agent) -> bool:
    """Rebuild frozen tool schemas only when compaction already invalidated the prompt cache."""
    from tools.mcp_tool_agent import refresh_agent_mcp_tools
    added = refresh_agent_mcp_tools(agent, content_aware=True)
    if added:
        logger.info("Compaction tool refresh added tools: %s", sorted(added))
    return bool(added)
