"""Fixtures shared across hermes_cli tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _admitting_hub_judge(monkeypatch):
    """Hub admission never reaches a live model in tests. [daveey]

    Legacy placement/move/routing suites pin the pre-admission logic, so the default fake
    admits (the old "stays on the hub" outcome). Gate behaviour is tested with the explicit
    ``hub_judge`` fixture (tests/hermes_cli/hub_judge_support.py) and tests/test_kanban_hub_admission*.
    """
    from hermes_cli import kanban_hub_admission as judge
    from tests.hermes_cli.hub_judge_support import FakeJudge
    monkeypatch.setattr(judge, "admit", FakeJudge())


@pytest.fixture(autouse=True)
def _isolate_kanban_worker_process_registries(monkeypatch):
    """Spawn doubles and exit classifications belong to one test, not the next tick's test.

    Restoring Popen alone leaves its fake handles parked in the dispatcher's registries.
    Use fresh dictionaries rather than clearing shared ones so teardown restores their owner.
    """
    from hermes_cli import kanban_db_dispatch
    from hermes_cli import kanban_queue_reason

    monkeypatch.setattr(kanban_db_dispatch, "_live_worker_procs", {})
    monkeypatch.setattr(kanban_db_dispatch, "_recent_worker_exits", {})
    monkeypatch.setattr(kanban_queue_reason, "_streaks", {})  # transient Board-failure streaks are per test
    from hermes_cli import kanban_sync_claims
    monkeypatch.setattr(kanban_sync_claims, "_STATE", kanban_sync_claims._State())  # a test's sync loop gates only it


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )


@pytest.fixture(autouse=True)
def _private_repo_checkout_lock(monkeypatch, tmp_path_factory):
    """``cmd_update`` locks the checkout it runs from: in tests, this repository, whose git
    common dir every parallel test process (and, from a linked worktree, the live install's
    ``hermes update``) shares. Give each test its own lock file for THIS repo; lock tests on
    fixture checkouts are unaffected."""
    from pathlib import Path

    from hermes_cli import update_lock

    repo = Path(update_lock.__file__).resolve().parents[1]
    real = update_lock.checkout_lock_path
    private = tmp_path_factory.mktemp("checkout-lock") / "hermes-update.lock"

    def checkout_lock_path(install_root=None):
        root = Path(install_root) if install_root else repo
        return private if root.resolve() == repo else real(install_root)

    monkeypatch.setattr(update_lock, "checkout_lock_path", checkout_lock_path)


@pytest.fixture(autouse=True)
def _source_channels_resolve_locally(request, monkeypatch):
    """Every unflagged ``hermes update`` resolves its channel through R2; tests must
    not reach the network for that. Default every channel to a ``source-branch``
    record delivering ``origin/<name>`` through the documented seam. Channel tests
    that model records themselves re-patch ``_resolve_channel`` after this runs;
    the marker opts out entirely for tests of the reader's own network path.
    """
    if request.node.get_closest_marker("real_release_channels"):
        return
    from hermes_cli import source_releases
    from hermes_cli.release_channels import ChannelResolution

    def resolve(name, repository):
        record = {"schema": 1, "name": name, "repository": repository, "policy": "source-branch",
                  "state": "active", "identity": None, "nextSequence": 1, "head": None,
                  "delivery": {"kind": "source-branch", "branch": name}}
        return ChannelResolution(record, record, None)

    monkeypatch.setattr(source_releases, "_resolve_channel", resolve)
    # stable reads GitHub releases directly; keep it off the network the same way.
    monkeypatch.setattr(source_releases, "_resolve_stable", lambda repository, *_: source_releases.SourceTarget(
        "stable", "stable", repository, branch="stable"))


@pytest.fixture(autouse=True)
def _pr_state_offline(monkeypatch):
    """Keep the respawn guard's PR-state lookup off the network: gh "fails", so ``active_pr``
    holds exactly as before (fail closed). Tests of the lookup re-patch ``_gh_pr_state``. [daveey]"""
    try:
        from hermes_cli import kanban_db_dispatch_pr_state as prs
    except Exception:
        return
    prs.clear_cache()
    monkeypatch.setattr(prs, "_gh_pr_state", lambda url: None)
    from hermes_cli import kanban_dependency_review_relink as relink
    relink.clear_cache()
    monkeypatch.setattr(relink, "_gh_pr_head", lambda url: None)
    yield
    prs.clear_cache()
    relink.clear_cache()


@pytest.fixture(autouse=True)
def _no_detached_commit_graph_writer(monkeypatch):
    """The update's maintenance starts a detached ``git commit-graph write`` on the checkout it ran
    from: in a test, this repository or a tmp copy deleted under it. In-process runs never spawn it."""
    from hermes_cli import git_commit_graph

    monkeypatch.setattr(git_commit_graph, "spawn_commit_graph_refresh", lambda root: False)


@pytest.fixture(autouse=True)
def _discharge_host_update_obligation():
    """Start and end every ``hermes_cli`` test with NO host update-restart obligation.

    The record is host-scoped on purpose (one multiplexer per host), so it lives in the
    per-OS-USER host state dir — not in the per-test ``HERMES_HOME``. The root conftest pins
    that dir per test only when the caller supplied no ``HERMES_GATEWAY_LOCK_DIR`` (#118097
    keeps the documented override working), so with one set every test in a file shares it and
    a test that arms the obligation makes the next one read a restart it never owed. Clearing
    the record — rather than re-pinning the dir — leaves that override rule untouched.
    """

    def _clear() -> None:
        try:
            from hermes_cli.update_host_obligation import clear_host_obligation

            clear_host_obligation()
        except Exception:
            # Import/env failure here must never error an unrelated test.
            pass

    _clear()
    yield
    _clear()


@pytest.fixture
def isolated_source_completion(monkeypatch):
    """Unit-test the completion tail in-process; real transport is tested separately."""
    from hermes_cli import update_cmd, update_completion

    monkeypatch.setattr("hermes_cli.source_build.build_update_products", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.venv_sync.publish_launchers", lambda *a: None)

    def complete(request):
        update_completion._complete_selected(request)
        return {"exit_code": 0, "receipt": update_completion._read_terminal_receipt(request),
                "windows_resume": request["windows_resume"]}

    monkeypatch.setattr(update_cmd, "run_completion", complete)


@pytest.fixture(autouse=True)
def _reset_prompt_toolkit_output_cache():
    """Clear prompt_toolkit's cached AppSession output around each CLI test.

    See the module docstring for the capsys/prompt_toolkit interaction this
    guards against.
    """

    def _clear() -> None:
        try:
            from prompt_toolkit.application.current import get_app_session

            get_app_session()._output = None
        except Exception:
            # prompt_toolkit not importable / internal shape changed — the
            # tests that rely on this simply keep their prior behavior.
            pass

    _clear()
    yield
    _clear()

@pytest.fixture
def probe_root(tmp_path):
    """A fixture checkout the installation launcher can boot from.

    ``runtime_command`` prepends the checkout root and runs ``import hermes_bootstrap``
    before the probe body, exactly as production does. Tests that point the import
    guard at a scratch tree need that module present, or the probe dies before its
    health marker — a developer venv whose editable ``.pth`` shadows the root hides
    the dependency, CI's clean environment does not.
    """
    (tmp_path / "hermes_bootstrap.py").write_text("", encoding="utf-8")
    return tmp_path


# Sync suites that prove WHAT a repair pass heals or credits, under the real clock, one round after a write by
# another connection. Production batches such passes (``LOCAL_WRITE_REPAIR_MIN_S``); the batching itself is
# pinned with the real constant in test_kanban_sync_repair_window.py. Listed by name so a new suite gets the
# production window by default.
_LOCAL_WRITES_REPAIR_AT_ONCE = frozenset(f"test_kanban_sync_{name}" for name in (
    "client", "copy_chat_links", "copy_links", "feed_apply_credit", "idle_round", "own_writes",
    "partition_repair", "placeholder_links", "repair_content", "round_connections"))


@pytest.fixture(autouse=True)
def _sync_repair_local_write_window(request, monkeypatch):
    if request.module.__name__.rpartition(".")[2] in _LOCAL_WRITES_REPAIR_AT_ONCE:
        from hermes_cli import kanban_sync_repair_window
        monkeypatch.setattr(kanban_sync_repair_window, "LOCAL_WRITE_REPAIR_MIN_S", 0.0)


@pytest.fixture(autouse=True)
def _reset_retired_host_loops():
    """A superseded or code-skewed watchdog under test retires this process's host loops; undo it after."""
    yield
    from tests._fixtures.host_loop_reset import reset_retired_host_loops
    reset_retired_host_loops()


def pytest_addoption(parser):
    """Explicit cross-repository input for the courier/core integration gate."""
    parser.addoption("--card-answer-courier", default=None,
                     help="Exact hermes-hub client source for test_card_answer_courier_integration.py")
