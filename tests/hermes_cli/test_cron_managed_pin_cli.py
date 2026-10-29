"""``hermes cron create/edit`` refuse a pin the managed routing registry rejects; doctor lists them.

Same contract as kanban's create-time check (abe20b1815): under
``model_routing.enforce_subscription_only`` the job's pin must pass ``pin_policy`` when it is set,
not only when the job wakes. [daveey]
"""
import argparse
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.jobs import create_job, get_job, list_jobs
from hermes_cli.cron import cron_command
from hermes_cli.subcommands.cron import build_cron_parser
from tests.cron.test_preflight_managed_pin import managed_yaml


@pytest.fixture()
def managed_home(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def configure(enforce=True):
        (tmp_path / "config.yaml").write_text(
            "model:\n  default: claude-opus-5-5\n  provider: anthropic\n" + managed_yaml(enforce),
            encoding="utf-8")

    configure()
    return configure


def _cli(*argv):
    parser = argparse.ArgumentParser(prog="hermes")
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=cron_command)
    return cron_command(parser.parse_args(["cron", *argv]))


def test_cron_edit_rejects_unregistered_pin(managed_home, capsys):
    job = create_job(prompt="classify", schedule="every 1h", model="claude-sonnet-5-5", provider="anthropic")
    rc = _cli("edit", job["id"], "--model", "claude-sonnet-5", "--provider", "anthropic")
    out = capsys.readouterr().out
    assert rc == 1
    assert "model_pin_not_in_routing_registry" in out
    assert "claude-sonnet-5-5" in out
    assert get_job(job["id"])["model"] == "claude-sonnet-5-5"  # unchanged


def test_cron_edit_accepts_registered_pin(managed_home, capsys):
    job = create_job(prompt="classify", schedule="every 1h", model="claude-sonnet-5")
    rc = _cli("edit", job["id"], "--model", "claude-sonnet-5-5", "--provider", "anthropic")
    assert rc in (0, None), capsys.readouterr().out
    assert get_job(job["id"])["model"] == "claude-sonnet-5-5"


def test_cron_edit_of_unrelated_field_is_not_refused(managed_home, capsys):
    """Only setting a pin is checked, so a stale-pinned job can still be renamed/paused."""
    job = create_job(prompt="classify", schedule="every 1h", model="claude-sonnet-5")
    rc = _cli("edit", job["id"], "--name", "renamed")
    assert rc in (0, None), capsys.readouterr().out
    assert get_job(job["id"])["name"] == "renamed"


def test_cron_create_rejects_unregistered_pin(managed_home, capsys):
    rc = cron_command(Namespace(
        cron_command="create", schedule="every 1h", prompt="classify", name="x", deliver=None,
        repeat=None, skill=None, skills=None, script=None, workdir=None, no_agent=False,
        model="claude-sonnet-5", provider=None))
    out = capsys.readouterr().out
    assert rc == 1
    assert "model_pin_not_in_routing_registry" in out
    assert list_jobs(include_disabled=True) == []


def test_cron_edit_policy_off_accepts_any_pin(managed_home, capsys):
    managed_home(enforce=False)
    job = create_job(prompt="classify", schedule="every 1h")
    rc = _cli("edit", job["id"], "--model", "claude-sonnet-5", "--provider", "anthropic")
    assert rc in (0, None), capsys.readouterr().out
    assert get_job(job["id"])["model"] == "claude-sonnet-5"


def test_cron_doctor_lists_rejected_pin(managed_home, capsys):
    bad = create_job(prompt="classify", schedule="every 1h", model="claude-sonnet-5")
    good = create_job(prompt="ok", schedule="every 1h", model="claude-sonnet-5-5")
    rc = cron_command(Namespace(cron_command="doctor"))
    out = capsys.readouterr().out
    assert rc == 1
    assert bad["id"] in out and "model_pin_not_in_routing_registry" in out
    assert "claude-sonnet-5-5" in out
    assert good["id"] not in out
