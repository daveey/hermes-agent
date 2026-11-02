"""Shared adapter status text for startup and reconnect recovery."""
from gateway.config import Platform


def adapter_unavailable_message(platform: Platform, *, retrying: bool = True) -> str:
    """Keep the plugin/deps/credentials hint after the first reconnect retry."""
    message = (
        f"No adapter available for enabled {platform.value}; check the plugin, dependencies, and credentials."
    )
    return f"{message} Retrying in the background." if retrying else message
