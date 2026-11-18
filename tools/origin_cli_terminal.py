"""Local foreground bridge lifetime; no retries after offering CLI custody."""
import json
import select
import socket
import sys

from hermes_cli import kanban_origin_native as native
from tools.origin_cli_bridge import Launch
from tools.origin_cli_policy import current_policy


class NoRetry(RuntimeError):
    """The shell may already have committed a create through the parent."""


def supported():
    return (sys.platform.startswith("linux") and hasattr(socket, "SOCK_SEQPACKET")
            and hasattr(socket, "MSG_CMSG_CLOEXEC") and hasattr(select, "poll"))


def offer(env, env_type, survivable, task_id):
    from agent.delegation_context import is_delegated_child_context
    from tools.environments.local import LocalEnvironment
    if (not supported() or type(env) is not LocalEnvironment or env_type != "local"
            or survivable is not None or is_delegated_child_context()
            or current_policy() is None or native.capture(None) is None):
        return None
    return Launch(task_id)


def execute(env, command, *, env_type, task_id, attempt=None, **kwargs):
    launch = offer(env, env_type, kwargs.get("survivable"), task_id)
    if launch is None:
        return env.execute(command, **kwargs)
    if attempt is not None:
        attempt.append(True)  # outer post-processing must not retry this shell either
    # A custody-bearing shell cannot be handed to background execution. Ordinary
    # shells and update-survivable launches retain their existing behavior.
    kwargs.pop("yield_handler", None)
    kwargs["origin_cli_launch"] = launch
    reports = []
    try:
        try:
            result = env.execute(command, **kwargs)
        finally:
            reports = launch.retire()
    except Exception as exc:  # health: allow BLE001 -- convert every backend failure to a non-retryable outcome
        raise NoRetry("CLI custody launch failed; do not retry automatically. "
                      + json.dumps(reports)) from exc
    except BaseException as exc:
        exc.add_note("Native CLI admission; do not retry automatically: " + json.dumps(reports))
        raise
    finally:
        launch.close()
    if reports:
        result["output"] = result.get("output", "") + "\n[Native CLI admission: " + json.dumps(reports) + "]"
    return result
