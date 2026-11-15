"""Fail-closed contracts for self-work diff review selection."""

import difflib

import pytest

from hermes_cli.kanban_selfwork_risk import classify_diff


def change(path="tests/hermes_cli/test_render.py", before="assert 1\n", after="assert 2\n", **extra):
    item = dict(old_path=path, new_path=path, status="M", binary=False,
                patch="".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                fromfile="a/" + path, tofile="b/" + path)))
    item.update(extra)
    return item


def test_ordinary_test_edit_is_automated():
    result = classify_diff([change()])
    assert result["risk"] == "automated"
    assert result["reasons"] and all(isinstance(reason, str) for reason in result["reasons"])


@pytest.mark.parametrize("path", [
    "hermes_cli/kanban_dispatch.py", "tests/hermes_cli/test_kanban_dispatch.py",
    "tests/hermes_cli/test_kanban_selfwork_risk.py", "tests/cron/test_scheduler.py",
    "tests/test_credentials.py", "tests/test_auth.py", "tests/test_secrets.py",
    "tests/test_oauth.py", "tests/test_token_broker.py", "tests/test_api_key.py",
    "tests/test_quota.py", "tests/test_pacing.py", "tests/test_budget.py",
    "tests/test_billing.py", "tests/test_money.py", "tests/test_cost.py",
    "tests/test_rate_limit.py", "tests/test_account_usage.py",
    "tests/conftest.py", "tests/helpers/conftest.py", "tests/fixtures/data.py",
    "tests/test_fixtures.py", "tests/run_tests.py", "tests/test_runner.py",
    "tests/ci/test_docs.py", ".github/workflows/test.yml", "scripts/run_tests.sh",
    "tests/pytest.ini", "tests/package.json", "tests/pyproject.toml",
    "tests/test_build.py", "tests/test_config.py", "tests/test_setup.py",
    "tests/test_shell.sh", "tests/test_payload.json", "tests/__init__.py",
    "hermes_cli/render.py", "README.md",
])
def test_sensitive_shared_configuration_and_unknown_paths_are_full(path):
    assert classify_diff([change(path)])["risk"] == "full"


@pytest.mark.parametrize("changes", [None, {}, "tests", [], [None], [{}]])
def test_invalid_collection_or_item_is_full(changes):
    assert classify_diff(changes)["risk"] == "full"


@pytest.mark.parametrize("extra", [
    {"patch": None}, {"patch": ""}, {"patch": 1}, {"patch": "garbage"},
    {"binary": True}, {"binary": None}, {"binary": 0},
    {"status": "T"}, {"status": "?"}, {"status": "R100"},
    {"old_path": "../tests/test_a.py"}, {"new_path": "/tests/test_a.py"},
    {"new_path": "tests/./test_a.py"}, {"new_path": "tests\\test_a.py"},
    {"new_path": "tests/test_a.py\n"}, {"new_path": None},
])
def test_malformed_or_uncertain_metadata_is_full(extra):
    assert classify_diff([change(**extra)])["risk"] == "full"


@pytest.mark.parametrize("field", ["old_path", "new_path", "status", "patch", "binary"])
def test_missing_required_field_is_full(field):
    item = change()
    del item[field]
    assert classify_diff([item])["risk"] == "full"


@pytest.mark.parametrize("patch", [
    "@@ -1 +1 @@\n-assert 1\n+assert 2\n",  # headerless Git patch is accepted
])
def test_valid_headerless_patch_is_supported(patch):
    assert classify_diff([change(patch=patch)])["risk"] == "automated"


@pytest.mark.parametrize("suffix", [
    "\nGIT binary patch\nliteral 2\n", "\nnew mode 100755\n",
    "\nnew file mode 120000\n", "\n... truncated ...\n", "\n+extra\n",
])
def test_unsafe_or_truncated_patch_is_full(suffix):
    assert classify_diff([change(patch=change()["patch"] + suffix)])["risk"] == "full"


def test_mismatched_hunk_counts_or_header_paths_are_full():
    patch = change()["patch"]
    assert classify_diff([change(patch=patch.replace("@@ -1 +1 @@", "@@ -1,9 +1 @@"))])["risk"] == "full"
    assert classify_diff([change(patch=patch.replace("a/tests/", "a/other/"))])["risk"] == "full"


def test_all_paths_must_qualify_without_mutating_inputs():
    items = [change(), change("hermes_cli/render.py")]
    original = [dict(item) for item in items]
    assert classify_diff(items)["risk"] == "full"
    assert items == original
    assert classify_diff(tuple([change(), change("tests/test_output.py")]))["risk"] == "automated"


@pytest.mark.parametrize("old,new", [
    ("tests/test_render.py", "tests/test_print.py"),
    ("tests/test_auth.py", "tests/test_print.py"),
    ("tests/test_render.py", "hermes_cli/render.py"),
])
def test_renames_are_conservatively_full(old, new):
    assert classify_diff([change(old, new_path=new, status="R")])["risk"] == "full"


def test_diagnostics_do_not_echo_patch_contents():
    result = classify_diff([change(patch="secret-value-that-must-not-leak")])
    assert result["risk"] == "full"
    assert "secret-value" not in str(result)


def test_proven_guard_comment_wording_is_automated():
    item = change("hermes_cli/context_switch_guard.py", "# Refuse stale context.\nvalue = 1\n",
                  "# Refuse outdated context.\nvalue = 1\n")
    assert classify_diff([item])["risk"] == "automated"


@pytest.mark.parametrize("before,after", [
    ("value = 1\n", "value = 2\n"),
    ('"""Old wording."""\n', '"""New wording."""\n'),
    ('message = """\n# Old wording.\n"""\n', 'message = """\n# New wording.\n"""\n'),
    ("# coding: utf-8\n", "# coding: latin-1\n"),
    ("# ordinary text\n", "#!/usr/bin/python\n"),
    ("# ordinary text\n", "# type: ignore\n"),
    ("# ordinary text\n", "# noqa\n"),
    ("# ordinary text\n", "# fmt off\n"),
    ("# ordinary text\n", "# health allow HX001\n"),
    ("# ordinary text\n", "# pragma no cover\n"),
    ("# ordinary text\n", "# doctest SKIP\n"),
    ("# ordinary text\n", "# ruff ignore\n"),
    ("value = 1 # old wording\n", "value = 1 # new wording\n"),
    ("# Old wording.\nvalue = (\n", "# New wording.\nvalue = (\n"),
])
def test_unproven_or_executable_guard_wording_is_full(before, after):
    assert classify_diff([change("hermes_cli/context_switch_guard.py", before, after)])["risk"] == "full"


def test_guard_comment_patch_without_known_lexical_prefix_is_full():
    patch = "@@ -20 +20 @@\n-# Old wording.\n+# New wording.\n"
    assert classify_diff([change("hermes_cli/context_switch_guard.py", patch=patch)])["risk"] == "full"


@pytest.mark.parametrize("path", ["hermes_cli/kanban_dispatch_guard.py", "hermes_cli/auth_guard.py",
                                  "hermes_cli/quota_guard.py", "hermes_cli/money_guard.py",
                                  "other/guard.py", "hermes_cli/render.py"])
def test_comment_proof_never_overrides_sensitive_or_unknown_area(path):
    assert classify_diff([change(path, "# Old wording.\n", "# New wording.\n")])["risk"] == "full"


def test_comment_looking_lines_in_tests_do_not_need_production_proof():
    assert classify_diff([change(before='x = """\n# Old\n"""\n',
                                 after='x = """\n# New\n"""\n')])["risk"] == "automated"


@pytest.mark.parametrize("patch", [
    "@@ -0 +0 @@\n-assert 1\n+assert 2\n",
    "@@ -1 +1 @@\n-assert 1\n+assert 2",  # unmarked incomplete last line
    "@@ -1 +1 @@\n-assert 1\n+assert 2\n@@ -1 +1 @@\n-assert 1\n+assert 2\n",
])
def test_invalid_patch_coordinates_or_unmarked_truncation_are_full(patch):
    assert classify_diff([change(patch=patch)])["risk"] == "full"


@pytest.mark.parametrize("path", ["tests/helpers/test_render.py", "tests/support/test_render.py",
                                  "tests/test_harness.py", "tests/test_kanban.py"])
def test_shared_helpers_and_ambiguous_dispatcher_tests_are_full(path):
    assert classify_diff([change(path)])["risk"] == "full"


@pytest.mark.parametrize("before,after,status,absent", [
    ("", "assert True\n", "A", "old_path"),
    ("assert True\n", "", "D", "new_path"),
])
def test_regular_test_additions_and_deletions(before, after, status, absent):
    item = change(before=before, after=after, status=status, **{absent: None})
    assert classify_diff([item])["risk"] == "automated"


def test_git_regular_file_metadata_is_accepted_but_symlink_and_executable_are_full():
    item = change()
    preamble = "diff --git a/tests/hermes_cli/test_render.py b/tests/hermes_cli/test_render.py\n"
    assert classify_diff([change(patch=preamble + "index abcd..ef01 100644\n" + item["patch"])])["risk"] == "automated"
    for mode in ("120000", "100755", "160000"):
        assert classify_diff([change(patch=preamble + f"index abcd..ef01 {mode}\n" + item["patch"])])["risk"] == "full"


def test_title_metadata_cannot_reduce_risk():
    assert classify_diff([change("tests/test_auth.py", title="test-only guard wording")])["risk"] == "full"
