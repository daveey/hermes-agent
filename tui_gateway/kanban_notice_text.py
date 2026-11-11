"""Small Kanban notice text helpers independent of the session registry."""


def chat_wait_suffix(payload: dict) -> str:
    from hermes_cli.kanban_chat_wait import notice_text
    return ": " + notice_text(payload)
