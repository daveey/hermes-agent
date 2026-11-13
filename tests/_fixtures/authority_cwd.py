"""Opt-in cwd isolation for gateway-authority tests with a symbolic ``profile_id``.

Registered in ``tests/conftest.py`` (see the note there on why fixtures are imported rather than
listed in ``pytest_plugins``) so gateway, ACP and CLI tests receive it by name.
"""
import pytest


@pytest.fixture
def authority_cwd_in_tmp(tmp_path, monkeypatch):
    """Run in ``tmp_path`` for tests whose authority has a symbolic ``profile_id`` ('owned',
    'fixture', ...). Production passes the profile's absolute home; a symbolic id is a RELATIVE
    path, so the authority's profile-scoped work (an ``on_session_open`` delivery loading config)
    created ``<cwd>/<id>/`` in the checkout. Opt-in (``usefixtures``), never autouse: most tests rely
    on the checkout as cwd."""
    monkeypatch.chdir(tmp_path)
