"""Durable start RPC and its owning-runtime service factory.

Helpers are rebound onto server.py by register, like other methods_* modules.
Plugins import session_start_requests.get_service, not this bound implementation.
"""
from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_session_start_services = {}


def _start_request_owner(profile: str):
    from .session_start_requests import StartOwner
    from gateway.hosted_rooms import local_authority_gateway_id
    principal = _transport_auth_user_id(current_transport())
    if not principal:
        raise PermissionError("session.start_request requires an authenticated principal")
    _profile_home(profile)
    return StartOwner(local_authority_gateway_id(), _response_profile_name(profile), principal)


def get_session_start_service(profile: str):
    """Bound implementation; use session_start_requests.get_service from plugins.

    Owns a store handle for the gateway lifetime, including outstanding terminal
    callbacks. Agent tools must use the owning gateway runtime and principal.
    """
    from .session_start_requests import GatewayStartRuntime, StartService, StartStore
    from hermes_state_registry import acquire
    from tui_gateway import server
    import atexit
    home = _profile_home(profile)
    path = (home or Path(_hermes_home)) / "state.db"
    key = str(path.resolve())
    with _sessions_lock:
        if key not in _session_start_services:
            db = acquire(path)
            try:
                service = StartService(StartStore(db), GatewayStartRuntime(server, home))
            except Exception:
                _release_db(db)
                raise
            _session_start_services[key] = service
            atexit.register(_release_db, db)
        return _session_start_services[key]


@method("session.start_request")
def _(rid, params: dict) -> dict:
    # Reject identity/prompt injection even when contract validation is disabled.
    allowed = {"profile", "namespace", "start_request_id"}
    if (not isinstance(params, dict) or set(params) != allowed
            or any(not isinstance(params[k], str) or not params[k].strip() or len(params[k]) > 256 for k in allowed)):
        return _err(rid, -32602, "profile, namespace and start_request_id are required; no other fields allowed")
    try:
        owner = _start_request_owner(params["profile"])
        service = get_session_start_service(params["profile"])
        result = service.start(owner, params["namespace"], params["start_request_id"])
        return _ok(rid, result)
    except PermissionError:
        return _err(rid, 4030, "start request access denied")
    except NotImplementedError:
        return _err(rid, 4065, "session.start_request is unsupported in this runtime")
    except (ValueError, TypeError):
        return _err(rid, -32602, "invalid or conflicting start request")


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
