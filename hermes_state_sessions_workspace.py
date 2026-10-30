"""Workspace filtering SQL for the session lifecycle mixin. [daveey]"""


def _cwd_prefix_clause(cwd_prefix: str) -> tuple[str, list[str]]:
    """Match a cwd prefix without treating path characters as wildcards. [daveey]"""
    from hermes_state_sessions import _escape_like

    prefix = cwd_prefix.rstrip("/\\") or cwd_prefix
    # ``_``/``%`` are LIKE wildcards but ordinary path characters: unescaped, a
    # prefix also matches sibling directories. The ``=`` arm keeps the raw prefix.
    esc = _escape_like(prefix)
    return (
        "(s.cwd = ? OR s.cwd LIKE ? ESCAPE '\\' OR s.cwd LIKE ? ESCAPE '\\')",
        [prefix, f"{esc}/%", f"{esc}\\\\%"],
    )


def _workspace_key_clause(key: str) -> tuple[str, list[str]]:
    """Match git roots, falling back to cwd for legacy sessions. [daveey]"""
    from hermes_state_sessions import _cwd_prefix_clause

    prefix = key.rstrip("/\\") or key
    cwd_clause, cwd_params = _cwd_prefix_clause(prefix)
    return (
        f"(s.git_repo_root = ? OR (COALESCE(s.git_repo_root, '') = '' AND {cwd_clause}))",
        [prefix, *cwd_params],
    )
