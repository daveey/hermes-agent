"""A pytest process census must not inspect other sessions on its host."""
from __future__ import annotations

import os
import sys

import psutil
import pytest

_PROCESS = psutil.Process
_PROCESS_ITER = psutil.process_iter


def session_process_iter(roots, attrs=None, ad_value=None):
    """Keep real current-session children, before reading any requested attributes.

    Real host enumeration matters for integration tests, but another pytest's
    deleted cwd or thousands of unrelated jobs are not inputs to this session.
    Tests installing a synthetic process_iter still replace this default normally.
    """
    root = _PROCESS(os.getpid())
    owned = {root.pid, *(child.pid for child in root.children(recursive=True))}
    # Do not ask the host iterator for attrs: cwd() may stat a foreign deleted
    # path, and environ/cmdline can inspect unrelated real Hermes processes.
    for proc in _PROCESS_ITER():
        if proc.pid not in owned and not _rooted_in_session(proc, roots):
            continue
        if attrs is not None:
            try:
                proc.info = proc.as_dict(attrs=attrs, ad_value=ad_value)
            except psutil.NoSuchProcess:
                continue
        yield proc


def _rooted_in_session(proc, roots):
    """Also retain reparented fixture daemons, without stat'ing a foreign cwd."""
    try:
        # psutil's Linux cwd() stats a path ending in ' (deleted)' to distinguish
        # a deleted inode from a literal suffix. Read only the procfs link here.
        cwd = os.readlink(f"/proc/{proc.pid}/cwd") if os.path.isdir("/proc/self") else proc.cwd()
        cwd = cwd.removesuffix(" (deleted)")
        return any(cwd == root or cwd.startswith(root + os.sep) for root in roots)
    except (psutil.Error, OSError):
        return False


@pytest.fixture(autouse=True)
def isolate_process_census(monkeypatch, tmp_path_factory):
    roots = (str(tmp_path_factory.getbasetemp()), os.environ.get("HERMES_TEST_SANDBOX_HOME", ""))
    roots = tuple(os.path.realpath(root) for root in roots if root)
    def process_iter(attrs=None, ad_value=None):
        # Only cleanup's census is isolated. Other integration tests discover
        # abandoned gateway/desktop descendants by environment, not cwd, and
        # must retain their normal psutil census. No caller gets fake processes.
        caller = sys._getframe(1).f_globals.get("__name__")
        if caller in {"hermes_constants_scratch", "hermes_cli.kanban_workspace_jobs"}:
            return session_process_iter(roots, attrs, ad_value)
        return _PROCESS_ITER(attrs=attrs, ad_value=ad_value)

    process_iter.cache_clear = _PROCESS_ITER.cache_clear
    monkeypatch.setattr(psutil, "process_iter", process_iter)
