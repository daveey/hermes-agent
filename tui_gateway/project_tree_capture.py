"""Detached canonical project-tree inputs and dynamic external evidence. [daveey]

Capture runs on the source worker with already-normalized, authorized inputs.
The canonical builder discovers its own bounded probe closure (including deleted
worktree sibling paths); rendering replays only that evidence and never guesses
that an unobserved path does not exist. This module performs no discovery itself.
"""
from collections.abc import Mapping

from tui_gateway import project_tree


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def capture(projects, sessions, discovered_repos, resolve=None, *, preview_limit=3, hydrate=False,
            is_junk_root=None, is_junk_cwd=None, exists=None):
    """Record the exact canonical builder's probe closure, not a guessed path list."""
    value = _plain({'projects': projects, 'sessions': sessions, 'discovered': discovered_repos,
                    'preview_limit': preview_limit, 'hydrate': hydrate})
    probes = {}

    def record(name, function):
        if function is None:
            probes[name] = None
            return None
        rows = probes[name] = {}

        def read(path):
            if path not in rows:
                rows[path] = _plain(function(path))
            return _plain(rows[path])
        return read

    project_tree.build_tree(value['projects'], value['sessions'], value['discovered'],
                            record('resolve', resolve), preview_limit=preview_limit, hydrate=hydrate,
                            is_junk_root=record('is_junk_root', is_junk_root),
                            is_junk_cwd=record('is_junk_cwd', is_junk_cwd), exists=record('exists', exists))
    value['probes'] = probes
    return value


def project(captured):
    """Reuse the canonical serializer with memory-only evidence lookups."""
    value = _plain(captured)

    def lookup(name):
        rows = value['probes'][name]
        return None if rows is None else rows.__getitem__

    return project_tree.build_tree(value['projects'], value['sessions'], value['discovered'], lookup('resolve'),
                                   preview_limit=value['preview_limit'], hydrate=value['hydrate'],
                                   is_junk_root=lookup('is_junk_root'), is_junk_cwd=lookup('is_junk_cwd'),
                                   exists=lookup('exists'))
