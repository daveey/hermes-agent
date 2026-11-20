"""Checkout-deletion exceptions cannot certify local-branch publication."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_move_pool as kmp
from tests.hermes_cli import test_kanban_checkout_deletions as checkout
from tests.hermes_cli import test_kanban_pool_queue as pq


@pytest.mark.parametrize("empty_index", [False, True])
def test_local_upstream_does_not_publish_private_commits(tmp_path, empty_index):
    _ws, repo = checkout._checkout(tmp_path)
    pq._commit(repo, "private.txt")
    pq._git(repo, "branch", "local-upstream")
    pq._git(repo, "branch", "--set-upstream-to=local-upstream")
    if empty_index:
        pq._git(repo, "rm", "-q", "-f", "a.txt", "b.txt", "hidden.txt", "private.txt")
    assert kmp.tree_state([str(repo)], refresh=False) is not None
