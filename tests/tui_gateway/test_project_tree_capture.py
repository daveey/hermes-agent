"""Canonical tree inputs cross an immutable boundary with all dynamic probe evidence."""
import builtins
from copy import deepcopy
import os
import socket
import subprocess

import pytest

from hermes_cli.fact_contract import freeze
from tests.tui_gateway import test_project_tree as helpers
from tui_gateway import project_tree


def test_tree_capture_replays_dynamic_probes_without_source_access(monkeypatch):
    from tui_gateway import project_tree_capture

    projects = [helpers._project('p_fixture', 'Explicit', ['/explicit'])]
    sessions = [helpers._session('/explicit', id='explicit', branch='main'),
                helpers._session('/work/repo-task-gone/sub', id='deleted', branch='feature'),
                helpers._session('', id='homeless', title=None, last_active=None)]
    discovered = [{'root': '/discovered', 'label': None, 'sessions': 0, 'last_active': 0}]
    resolve = helpers._resolver({'/work/repo': ('/work/repo', '/work/repo'),
                                 '/explicit': ('/explicit', '/explicit')})
    kwargs = dict(preview_limit=3, hydrate=True, is_junk_root=lambda _: False,
                  is_junk_cwd=lambda _: False, exists=lambda p: p != '/work/repo-task-gone/sub')
    expected = deepcopy(project_tree.build_tree(projects, sessions, discovered, resolve, **kwargs))
    captured = freeze(project_tree_capture.capture(projects, sessions, discovered, resolve, **kwargs))
    assert '/work/repo' in captured['probes']['resolve']

    def forbidden(*args, **kwargs):
        pytest.fail('canonical tree render revisited external evidence')

    with monkeypatch.context() as guard:
        guard.setattr(builtins, 'open', forbidden)
        guard.setattr(os.path, 'isdir', forbidden)
        guard.setattr(socket, 'socket', forbidden)
        guard.setattr(subprocess, 'Popen', forbidden)
        actual = project_tree_capture.project(captured)
        assert actual == expected
        actual['projects'][0]['name'] = 'mutated output'
        assert project_tree_capture.project(captured) == expected
    sessions[0]['title'] = 'changed after capture'
    assert project_tree_capture.project(captured) == expected


@pytest.mark.parametrize('hydrate', [False, True])
def test_tree_capture_preserves_absent_resolver_defaults(hydrate):
    from tui_gateway import project_tree_capture

    sessions = [helpers._session('/repo-wt-feature', id='s', branch='feature')]
    captured = freeze(project_tree_capture.capture([], sessions, [], hydrate=hydrate, preview_limit=0))
    expected = project_tree.build_tree([], sessions, [], hydrate=hydrate, preview_limit=0)
    assert project_tree_capture.project(captured) == expected
    assert captured['probes'] == dict.fromkeys(('resolve', 'is_junk_root', 'is_junk_cwd', 'exists'))


def test_missing_probe_is_not_negative_evidence():
    from tui_gateway import project_tree_capture

    sessions = [helpers._session('/repo', id='s')]
    captured = project_tree_capture.capture([], sessions, [], lambda p: None)
    del captured['probes']['resolve']['/repo']
    with pytest.raises(KeyError, match='/repo'):
        project_tree_capture.project(freeze(captured))


def test_probe_results_are_captured_once_and_not_aliased():
    from tui_gateway import project_tree_capture

    info = {'repo_root': '/repo', 'worktree_root': '/repo'}
    seen = []

    def resolve(path):
        seen.append(path)
        return info

    sessions = [helpers._session('/repo', id='a'), helpers._session('/repo', id='b')]
    captured = project_tree_capture.capture([], sessions, [], resolve)
    expected = project_tree_capture.project(freeze(captured))
    assert seen == ['/repo']
    info['repo_root'] = '/changed'
    assert project_tree_capture.project(freeze(captured)) == expected
