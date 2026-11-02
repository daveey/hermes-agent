"""Frozen legacy plugin used by the behavior compatibility suite.

Keep this callback in its old, narrow form: it intentionally predates additive
hook payload fields and does not accept ``**kwargs``.
"""

received_sessions = []


def _on_session_start(*, session_id):
    received_sessions.append(session_id)
    return {"legacy_session_id": session_id}


def register(ctx):
    ctx.register_hook("on_session_start", _on_session_start)


def load_tests(loader, tests, pattern):
    """Refuse unittest discovery: it imports test modules without conftest isolation."""
    raise RuntimeError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")
