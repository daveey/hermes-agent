"""Small Kanban notice text helpers independent of the session registry."""

import contextlib


def chat_wait_suffix(payload: dict) -> str:
    from hermes_cli.kanban_chat_wait import notice_text
    return ": " + notice_text(payload)


def failure_suffix(kind: str, payload: dict, title: str) -> str:
    """Cause-accurate failure wording shared by the Desktop failure formatters."""
    from hermes_cli.kanban_failure_summary import describe_failure
    return describe_failure(kind, title, payload)


def timed_out_suffix(task, payload: dict, title: str) -> str:
    from hermes_cli.kanban_failure_summary import retry_promised
    limit = 0
    with contextlib.suppress(TypeError, ValueError):
        limit = int(payload.get("limit_seconds") or 0)
    tail = "; will retry" if retry_promised(payload) else "; retry state was not recorded"
    return f" timed out (max_runtime={limit}s){tail}"


def left_open_suffix(payload: dict) -> str:
    from hermes_cli.kanban_pr_landing import left_open_notice
    notice = left_open_notice(payload)
    return f"\n{notice}" if notice else ""


def quota_episode_suffix(payload: dict) -> str:
    from hermes_cli.kanban_quota_episode import notice_text
    return notice_text(payload)


def repin_suffix(payload: dict) -> str:
    from hermes_cli.kanban_repin_notice import notice_text
    return ": " + notice_text(payload)


def parent_unclaimed_suffix(payload: dict) -> str:
    from hermes_cli.kanban_parent_unclaimed import notice_text
    return ": " + notice_text(payload)
