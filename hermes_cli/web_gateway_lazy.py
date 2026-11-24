"""Publish delivery hooks without eagerly importing the agent/provider tree."""
import importlib

_OWNER = object()


def load_gateway():
    return importlib.import_module('tui_gateway.server')


class GatewayOnDemand:
    """Hosted-room RPC resolves the server only when it calls a session handler."""
    def __getattr__(self, name):
        return getattr(load_gateway(), name)


def _inject(**kwargs):
    return load_gateway().inject_tui_session_message(**kwargs)


def install_message_host():
    from hermes_cli.plugins import get_plugin_manager, publish_tui_message_host
    publish_tui_message_host(_OWNER, _inject)
    get_plugin_manager().set_tui_message_injector(_OWNER, _inject)


def clear_message_host():
    from hermes_cli.plugins import get_plugin_manager, clear_published_tui_message_host
    clear_published_tui_message_host(_OWNER)
    get_plugin_manager().clear_tui_message_injector(_OWNER)


def install_readopt_waker(registry=None):
    if registry is None:
        from tools.process_registry import process_registry as registry
    registry.on_unknown_exit = lambda evt: load_gateway()._readopted_exit_deliver(evt, registry=registry)
