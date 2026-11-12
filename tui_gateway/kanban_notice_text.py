"""Small Kanban notice text helpers independent of the session registry."""


def chat_wait_suffix(payload: dict) -> str:
    from hermes_cli.kanban_chat_wait import notice_text
    return ": " + notice_text(payload)


def failure_suffix(kind: str, payload: dict, title: str) -> str:
    """Cause-accurate failure wording shared by the Desktop failure formatters."""
    from hermes_cli.kanban_failure_summary import describe_failure
    return describe_failure(kind, title, payload)
