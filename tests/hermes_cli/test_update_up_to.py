"""``hermes update --up-to <sha>`` stops a main update at a first-parent commit before the tip.

hermes-sync applies a verified Desktop prebuild of an older origin/main commit C with this flag.
Real Git fixtures: origin/main is A - B - C - M(C + side S) - T, the install sits on main at A.
"""
import argparse
from copy import deepcopy
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import main, update_cmd, update_up_to
from hermes_cli.subcommands.update import build_update_parser


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout.strip()


def _commit(root, name, text):
    (root / name).write_text(text)
    git(root, "add", ".")
    git(root, "commit", "-qm", name)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def fork(tmp_path, monkeypatch):
    from hermes_cli import update_cmd_posix_pause as pause
    monkeypatch.setattr(pause, "_discover_systemd", list)
    monkeypatch.setattr(pause, "_discover_launchd", list)
    monkeypatch.setattr(pause, "_discover_bare", lambda pids: ([], []))
    home, origin, checkout = (tmp_path / name for name in ("home", "origin", "checkout"))
    home.mkdir()
    origin.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_INSTALL_ROOT", str(checkout))
    monkeypatch.delenv("HERMES_MANAGED", raising=False)
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.name", "Fixture")
    git(origin, "config", "user.email", "fixture@example.invalid")
    git(origin, "config", "commit.gpgsign", "false")
    a = _commit(origin, "a.txt", "a\n")
    b = _commit(origin, "b.txt", "b\n")
    git(origin, "checkout", "-q", "-b", "side")
    s = _commit(origin, "s.txt", "side\n")
    git(origin, "checkout", "-q", "main")
    c = _commit(origin, "c.txt", "c\n")
    git(origin, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    m = git(origin, "rev-parse", "HEAD")
    t = _commit(origin, "t.txt", "tip\n")
    git(tmp_path, "clone", "-q", str(origin), str(checkout))
    git(checkout, "config", "user.name", "Fixture")
    git(checkout, "config", "user.email", "fixture@example.invalid")
    git(checkout, "config", "commit.gpgsign", "false")
    git(checkout, "reset", "-q", "--hard", a)
    monkeypatch.setattr(main, "PROJECT_ROOT", checkout)
    monkeypatch.setattr("hermes_cli.config.get_project_root", lambda: checkout)
    monkeypatch.setattr("hermes_cli.backup._sibling_profile_homes", lambda h: [])
    seen = SimpleNamespace(preflight=[], armed=[], upstream_prepared=[], upstream_synced=[], pushed=[],
                           stops=[], zip=[], resumed=[])
    monkeypatch.setattr(update_cmd._commit, "preflight_refusal",
                        lambda git_cmd, root, target_ref, files: seen.preflight.append(target_ref))
    real_arm = update_cmd._commit.arm_commit_point

    def arm(git_cmd, root, expected_sha, **move):
        seen.armed.append((expected_sha, move.get("target")))
        return real_arm(git_cmd, root, expected_sha, **move)

    monkeypatch.setattr(update_cmd._commit, "arm_commit_point", arm)
    monkeypatch.setattr(update_cmd, "_prepare_upstream_sync",
                        lambda *a, enabled, **k: seen.upstream_prepared.append(enabled))
    monkeypatch.setattr(main, "_sync_with_upstream_if_needed",
                        lambda *a, **k: seen.upstream_synced.append(git(checkout, "rev-parse", "HEAD")) or True)
    monkeypatch.setattr(update_cmd, "_push_synced_fork", lambda *a, **k: seen.pushed.append(a))
    monkeypatch.setattr(update_up_to, "_record_stop", lambda reason, **k: seen.stops.append(reason))
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=main.cmd_update)
    opts = update_cmd._UpdateOptions(pre_update_version=None, gw_input_fn=None,
        assume_yes=True, keep_stash=False, switch_branch=False, discard_local_changes=False)
    monkeypatch.setattr(update_cmd, "_resolve_update_options", lambda *_: opts)
    monkeypatch.setattr(update_cmd, "_begin_update_receipt_and_plan", lambda *_: None)
    monkeypatch.setattr(main, "_run_pre_update_backup", lambda *_: None)
    monkeypatch.setattr(main, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(main, "_resume_windows_gateways_after_update", lambda token: seen.resumed.append(token))
    # A fork: a plain update would merge upstream and push; --up-to must do neither.
    monkeypatch.setattr(update_cmd, "_prepare_git_command", lambda **_: (False, ["git"], True))
    monkeypatch.setattr(update_cmd, "_update_via_zip", lambda *a, **k: seen.zip.append(a))
    completed = []
    monkeypatch.setattr(update_cmd, "_complete_source_update",
                        lambda request: completed.append(deepcopy(request)))
    return SimpleNamespace(root=checkout, a=a, b=b, c=c, s=s, m=m, t=t, parser=parser,
                           completed=completed, seen=seen)


def run(fork, *argv):
    update_cmd._cmd_update_impl(fork.parser.parse_args(["update", "--yes", *argv]), False)


def head(fork):
    return git(fork.root, "rev-parse", "HEAD"), git(fork.root, "rev-parse", "--abbrev-ref", "HEAD")


def test_stops_at_the_first_parent_commit_on_main_without_upstream_sync_or_push(fork, capsys):
    run(fork, "--up-to", fork.c)

    out = capsys.readouterr().out
    assert head(fork) == (fork.c, "main")
    assert f"→ Updating up to {fork.c[:10]} (origin/main is {fork.t[:10]}; 2 commit(s))" in out
    assert "→ Found 2 new commit(s)" in out
    assert fork.seen.upstream_prepared == [False]
    assert fork.seen.upstream_synced == [] and fork.seen.pushed == []
    assert len(fork.completed) == 1 and fork.completed[0]["expected_sha"] == fork.c
    assert fork.seen.stops == []


@pytest.mark.parametrize("advance", [False, True])
def test_pin_at_head_finishes_without_moving_or_syncing(fork, advance, capsys):
    git(fork.root, "reset", "-q", "--hard", fork.c)
    origin = fork.root.parent / "origin"
    git(origin, "reset", "-q", "--hard", fork.c)
    git(fork.root, "fetch", "-q", "origin")
    tip = _commit(origin, "later.txt", "later\n") if advance else fork.c

    run(fork, "--up-to", fork.c)

    assert head(fork) == (fork.c, "main")
    assert git(fork.root, "rev-parse", "origin/main") == tip
    assert fork.seen.upstream_prepared == [False]
    assert fork.seen.upstream_synced == [] and fork.seen.pushed == []
    assert fork.seen.armed == []
    assert len(fork.completed) == 1
    assert fork.completed[0]["expected_sha"] == fork.c
    assert fork.completed[0]["completion_message"] == "✓ Already up to date!"
    assert fork.seen.stops == []
    assert "0 commit(s)" in capsys.readouterr().out


def test_pin_at_head_off_first_parent_line_is_refused(fork, capsys):
    git(fork.root, "reset", "-q", "--hard", fork.s)
    out = _refused(fork, capsys, fork.s, head(fork))
    assert "not on origin/main's first-parent line" in out


def test_preflight_commit_point_and_marker_name_the_selected_commit_not_the_tip(fork, monkeypatch):
    pinned = []
    real_pull = update_cmd._pull_updates

    def pull(*a, **k):
        pinned.append(k.get("expected_sha"))  # the receipt's commit is fixed BEFORE the tree moves
        return real_pull(*a, **k)

    monkeypatch.setattr(update_cmd, "_pull_updates", pull)
    run(fork, "--up-to", fork.c)

    assert fork.seen.preflight == [fork.c]
    assert fork.seen.armed == [(fork.c, fork.c)]
    assert pinned == [fork.c]


def test_a_later_plain_update_moves_on_to_the_tip(fork):
    run(fork, "--up-to", fork.b)
    assert head(fork) == (fork.b, "main")

    run(fork)

    assert head(fork) == (fork.t, "main")
    assert fork.completed[-1]["expected_sha"] == fork.t
    assert fork.seen.upstream_prepared == [False, True]


def _refused(fork, capsys, sha, before):
    with pytest.raises(SystemExit) as stopped:
        run(fork, "--up-to", sha)
    out = capsys.readouterr().out
    assert stopped.value.code == 1
    assert f"✗ --up-to {sha[:10]}: " in out and "No update was applied." in out
    assert head(fork) == before
    assert fork.seen.stops == ["up_to_refused"]
    assert fork.seen.armed == [] and fork.completed == [] and fork.seen.pushed == []
    assert fork.seen.resumed  # paused (Windows) gateways are handed back before the exit
    return out


@pytest.mark.parametrize("case", [
    "side-branch", "not-on-origin-main", "behind-head", "head-has-local-commit",
    "unknown-commit",
])
def test_refuses_a_commit_off_the_first_parent_line_or_not_past_head(fork, capsys, case):
    sha = {"side-branch": fork.s, "behind-head": fork.a,
           "head-has-local-commit": fork.c, "unknown-commit": "0" * 40}.get(case)
    if case == "behind-head":
        git(fork.root, "reset", "-q", "--hard", fork.b)
    elif case == "head-has-local-commit":
        _commit(fork.root, "local.txt", "local\n")  # HEAD is not an ancestor of C
    elif case == "not-on-origin-main":
        git(fork.root, "checkout", "-q", "-b", "other")
        sha = _commit(fork.root, "x.txt", "x\n")  # a descendant of HEAD that origin/main lacks
        git(fork.root, "checkout", "-q", "main")
    out = _refused(fork, capsys, sha, head(fork))
    expected = {"side-branch": "not on origin/main's first-parent line",
                "not-on-origin-main": "not on origin/main's first-parent line",
                "behind-head": "is not an ancestor of it",
                "head-has-local-commit": "is not an ancestor of it", "unknown-commit": "not a commit"}[case]
    assert expected in out


@pytest.mark.parametrize("parked", ["detached", "branch"])
def test_refuses_a_checkout_that_is_not_on_branch_main(fork, capsys, parked):
    if parked == "detached":
        git(fork.root, "checkout", "-q", "--detach", fork.a)
    else:
        git(fork.root, "checkout", "-q", "-b", "parked")
    out = _refused(fork, capsys, fork.c, head(fork))
    assert "not branch main" in out


@pytest.mark.parametrize("install", ["zip", "release", "branch"])
def test_refuses_installs_that_do_not_update_main_from_git(fork, capsys, monkeypatch, install):
    if install == "zip":
        monkeypatch.setattr(update_cmd, "_prepare_git_command", lambda **_: (True, ["git"], False))
    elif install == "release":
        monkeypatch.setattr(update_cmd._check, "select_apply_target",
                            lambda args, branch, request, **k: (fork.c, fork.c, False, None))
    else:
        def stable(args, branch, request, **k):
            request["branch"] = "stable"
            return "origin/stable", None, False, None
        monkeypatch.setattr(update_cmd._check, "select_apply_target", stable)
    _refused(fork, capsys, fork.c, head(fork))
    assert fork.seen.zip == [] and fork.seen.preflight == []


@pytest.mark.parametrize("extra", [
    ["--branch", "main"], ["--channel", "main"], ["--set-channel", "main"], ["--check"], ["--plan"],
])
def test_refuses_another_target_or_a_read_only_mode(fork, capsys, extra):
    args = fork.parser.parse_args(["update", "--up-to", fork.c, *extra])
    with pytest.raises(SystemExit) as stopped:
        main._update_preflight_handled(args)
    assert stopped.value.code == 1
    assert f"✗ --up-to {fork.c[:10]}: cannot be combined with {extra[0]}" in capsys.readouterr().out
    assert fork.seen.stops == ["up_to_refused"]


@pytest.mark.parametrize("sha", ["abc123", "A" * 40, "g" * 40])
def test_refuses_anything_but_a_full_lowercase_sha(fork, capsys, sha):
    with pytest.raises(SystemExit) as stopped:
        main._update_preflight_handled(fork.parser.parse_args(["update", "--up-to", sha]))
    assert stopped.value.code == 1 and "expected a full 40-hex commit sha" in capsys.readouterr().out


def test_package_managed_and_image_installs_refuse_with_exit_1(fork, capsys, monkeypatch):
    args = fork.parser.parse_args(["update", "--up-to", fork.c])
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config.managed_error", lambda *_: None)
    with pytest.raises(SystemExit) as stopped:
        main._update_preflight_handled(args)
    assert stopped.value.code == 1 and "package-managed" in capsys.readouterr().out

    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.update_contract.evaluate_update_admission",
                        lambda root: SimpleNamespace(message="docker image: pull a new image"))
    monkeypatch.setattr("hermes_cli.update_contract.record_refusal_receipt", lambda refusal: None)
    with pytest.raises(SystemExit) as stopped:
        main._update_preflight_handled(args)
    assert stopped.value.code == 1 and "not updated from a git checkout" in capsys.readouterr().out
    assert fork.seen.stops == ["up_to_refused", "up_to_refused"]


def test_an_unresolvable_origin_main_refuses(fork, capsys):
    git(fork.root, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(SystemExit) as stopped:
        update_up_to.select_target(["git"], fork.root, fork.c, "main", "origin/main", {}, stop=lambda: None)
    assert stopped.value.code == 1 and "origin/main or HEAD does not resolve" in capsys.readouterr().out


def test_capability_marker_names_the_flag():
    """hermes-sync reads this file's text in the live checkout to decide whether to pass the flag.

    Deliberate exception to tests/AGENTS.md's ban on asserting source text: the literal
    ``"--up-to"`` in hermes_cli/subcommands/update.py IS the capability marker hermes-sync greps
    for, so the source text is the contract. Do not rename or remove the marker.
    """
    from pathlib import Path

    from hermes_cli.subcommands import update as parser_module
    assert '"--up-to"' in Path(parser_module.__file__).read_text(encoding="utf-8")
    # This text marker likewise advertises the equal-HEAD extension to old sync runners.
    assert "UP_TO_ACCEPTS_HEAD = True" in Path(update_up_to.__file__).read_text(encoding="utf-8")


def test_a_failed_fast_forward_never_resets_or_detaches(fork, monkeypatch):
    calls = []

    def fake_git(git_cmd, args, *a, **k):
        calls.append(args[:2])
        return subprocess.CompletedProcess(args, 1, "", "not possible to fast-forward")

    monkeypatch.setattr(update_cmd, "_git_run", fake_git)
    monkeypatch.setattr(update_cmd, "_reconcile_diverged_checkout", lambda *a, **k: calls.append("reset"))
    monkeypatch.setattr(update_cmd, "_record_stop", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        update_cmd._move_checkout_to(["git"], "main", fork.c, fork.c, fork.a, up_to=True)
    assert calls == [["merge", "--ff-only"], ["merge-base", "--is-ancestor"]]


def test_a_zero_count_never_runs_the_fork_upstream_sync(fork, monkeypatch):
    plan = update_cmd._prepare_checkout_for_update(
        ["git"], "main", "main", is_fork=True, assume_yes=True, gateway_mode=False, gw_input_fn=None,
        switch_branch=False, target_ref=fork.a, _windows_gateway_resume=None, up_to=True)
    assert plan.commit_count == 0 and fork.seen.upstream_synced == []
