"""A tagless fork must validate plugins against reachable upstream releases."""

import os

import pytest

from hermes_cli.version_info import _git_version_info
from tests.ci.test_plugin_catalog_ci_confinement import (
    PINNED_JOB, _commit, _find_step, _git, _run,
)

UPSTREAM = "https://github.com/NousResearch/hermes-agent.git"


@pytest.mark.parametrize("conflict", [False, True])
def test_trusted_base_imports_release_tags_without_moving_head(tmp_path, conflict):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    (upstream / "tracked").write_text("release\n", encoding="utf-8")
    released = _commit(upstream)
    _git(upstream, "tag", "v0.23.0")
    (upstream / "tracked").write_text("future\n", encoding="utf-8")
    _commit(upstream)
    _git(upstream, "tag", "v99.0.0")
    checkout = tmp_path / "trusted"
    _git(tmp_path, "clone", "--no-tags", str(upstream), str(checkout))
    _git(checkout, "checkout", "--detach", released)
    assert _git_version_info(checkout).base_version == "unknown"
    if conflict:
        _git(checkout, "tag", "v99.0.0", released)

    step = _find_step("Fetch upstream release identity", PINNED_JOB)
    assert step["if"] == "steps.changed.outputs.files != ''"
    result = _run(step["run"], tmp_path, checkout, {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{upstream.as_uri()}.insteadOf",
        "GIT_CONFIG_VALUE_0": UPSTREAM,
        "GIT_TERMINAL_PROMPT": "0",
    })
    if conflict:
        assert result.returncode != 0
        assert _git(checkout, "rev-parse", "v99.0.0") == released
        assert _git(checkout, "rev-parse", "HEAD") == released
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == released
    assert not _git(checkout, "status", "--porcelain")
    # A newer official tag is fetched too, but cannot inflate this base's version.
    assert _git(checkout, "tag", "--list", "v99.0.0") == "v99.0.0"
    assert _git_version_info(checkout).base_version == "0.23.0"
