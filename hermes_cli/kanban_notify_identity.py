"""Subscriber identity for ``hermes kanban notify-subscribe`` (a topical sibling of the
``hermes_cli/kanban.py`` facade; re-exported there under the same name)."""
from __future__ import annotations


def _caller_identity_for_target(platform: str, chat_id: str, thread_id, user_id, user_id_alt) -> tuple:
    """``(user_id, user_id_alt)`` for a subscription, the calling gateway session's sender filled
    in when the target IS that session's chat and no id was given. An agent running this command
    from a group chat omits ``--user-id``; under ``group_sessions_per_user`` the wake would then key
    a second, participant-less session (#147522). Another chat's (or topic's) subscription is
    never stamped with the caller's identity."""
    if user_id or user_id_alt:  # an explicit recipient is never paired with the caller's other id
        return user_id, user_id_alt
    from gateway.session_context import get_session_env
    if (str(get_session_env("HERMES_SESSION_PLATFORM", "")).lower() != str(platform or "").lower()
            or str(get_session_env("HERMES_SESSION_CHAT_ID", "")) != str(chat_id or "")
            or str(get_session_env("HERMES_SESSION_THREAD_ID", "")) != str(thread_id or "")):
        return user_id, user_id_alt
    return (get_session_env("HERMES_SESSION_USER_ID", "") or None,
            get_session_env("HERMES_SESSION_USER_ID_ALT", "") or None)
