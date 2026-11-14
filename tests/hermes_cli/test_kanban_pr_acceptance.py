"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                rule = None if state.get("no_required") else {"requiredStatusChecks": [
                    {"context": "required", "app": {"databaseId": 1}}]}
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": state.get("pr_state", "MERGED"),
                    "mergedAt": "2026-11-02T13:10:04Z",
                    "baseRef": {"branchProtectionRule": rule}},
                    "defaultBranchRef": {"name": "main", "branchProtectionRule": rule}}}}
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-suites" in self.path:
                # One suite holding every served run: OPEN proves its latest view against it.
                suite_sha = self.path.split("/commits/", 1)[1].split("/", 1)[0]
                count = 100 + (0 if state.get("missing") else 1)
                value = [{"total_count": 1, "check_suites": [{
                    "id": 7, "app": {"id": 1}, "head_sha": suite_sha, "latest_check_runs_count": count}]}]
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "started_at": "2026-11-02T12:51:52Z",
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42", "check_suite": {"id": 7}}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"},
                         "state": "open" if state.get("pr_state", "MERGED") == "OPEN" else "closed",
                         "merged": state.get("pr_state", "MERGED") == "MERGED"}
            else:
                self.send_error(404)
                return
            if state.get("no_ci") and "/check-runs" in self.path:
                value = [{"total_count": 0, "check_runs": []}]
            if state.get("no_ci") and "/check-suites" in self.path:
                value = [{"total_count": 0, "check_suites": []}]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(u).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.platforms("linux")
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.platforms("linux")
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("claimed", [False, True])
def test_origin_release_requires_completion_bound_pr_receipt(github, claimed):
    from hermes_cli.kanban_origin_ledger import observe_owner_task

    with connect() as conn:
        tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
        run = kb.claim_task(conn, tid).current_run_id if claimed else None
        assert kb.complete_task(conn, tid, summary="verified", expected_run_id=run,
                                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})

        def verified():
            conn.execute("SAVEPOINT observation")
            try:
                return observe_owner_task(conn, {"id": tid, "status": "done"}, now=123)["origin_observation"]["verified"]
            finally:
                conn.execute("RELEASE observation")

        assert verified()
        event = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='completed'", (tid,)).fetchone()
        payload = json.loads(event["payload"])
        receipt_id = payload["pr_acceptance_event_id"]
        receipt = conn.execute("SELECT payload FROM task_events WHERE id=?", (receipt_id,)).fetchone()
        assert json.loads(receipt[0])["head_sha"] == "a" * 40
        conn.execute("UPDATE task_events SET run_id=NULL WHERE id=?", (receipt_id,))
        assert not verified()  # even synthesized completion runs must own their receipt
        completion_run = conn.execute("SELECT run_id FROM task_events WHERE id=?", (event["id"],)).fetchone()[0]
        conn.execute("UPDATE task_events SET run_id=? WHERE id=?", (completion_run, receipt_id))
        assert verified()
        conn.execute("UPDATE tasks SET completion_contract='https://github.com/acme/repo/pull/8' WHERE id=?", (tid,))
        assert not verified()
        conn.execute("UPDATE tasks SET completion_contract='https://github.com/acme/repo/pull/7' WHERE id=?", (tid,))
        assert verified()
        payload.pop("pr_acceptance_event_id")
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), event["id"]))
        assert not verified()  # legacy completions cannot borrow a nearby receipt


# --- #122689: acceptance must read the repo as the ASSIGNEE profile's gh login ---

@pytest.mark.platforms("posix")
def test_acceptance_runs_gh_as_the_assignee_profile(tmp_path, monkeypatch):
    """The gh child env carries the assignee's own GH credentials (its .env),
    never the ambient/launch residue, and an invisible repo is classified
    `auth` naming the repository — not a retryable `infra` failure."""
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # resolver must not probe the real ~/.local/bin/gh
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("GH_TOKEN=b-token\nBWS_ACCESS_TOKEN=b-vault\n"
                                        "OP_SERVICE_ACCOUNT_TOKEN=b-op\n", encoding="utf-8")
    # Ambient residue that must NOT decide the login.
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os\n"
                  f"json.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status != "done"
        receipts = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
        assert receipts[-1]["classification"] == "auth"
        assert "acme/repo" in receipts[-1]["detail"]
    captured = json.loads(env_dump.read_text())
    assert captured["GH_TOKEN"] == "b-token"
    # gh never needs the profile's vault bootstrap token, even though its .env holds one.
    assert "BWS_ACCESS_TOKEN" not in captured and "OP_SERVICE_ACCOUNT_TOKEN" not in captured
    assert captured.get("GH_CONFIG_DIR") != "/nonexistent/launch/gh"
    assert "credentials" in (kb.get_task(conn, tid).last_failure_error or "")


@pytest.mark.platforms("posix")
def test_assignee_without_own_gh_login_never_falls_through_to_ambient_login(tmp_path, monkeypatch):
    """An assignee profile with no GH_TOKEN/GH_CONFIG_DIR of its own must not inherit the
    launch user's ~/.config/gh (HOME/XDG_CONFIG_HOME stay the launch process's): gh is pinned
    to a profile-owned config dir, its 'not logged in' exit is classified `auth` naming the profile."""
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # resolver must not probe the real ~/.local/bin/gh
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "launch-xdg"))

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Real gh: GH_CONFIG_DIR wins; a config dir without hosts.yml means "not logged in" (exit 4).
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                  f"pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
                  "if 'GH_CONFIG_DIR' in os.environ and not os.path.exists(os.environ['GH_CONFIG_DIR']):\n"
                  "    sys.exit(4)\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'b'" in receipt["detail"] and "no login" in receipt["detail"]
    captured = json.loads(env_dump.read_text(encoding="utf-8-sig"))
    assert captured["GH_CONFIG_DIR"] == str(assignee_home / "gh")
    assert "GH_TOKEN" not in captured and "GITHUB_TOKEN" not in captured


def test_assigned_card_with_unresolvable_profile_is_auth_not_ambient(tmp_path, monkeypatch):
    """A card assigned to a profile that no longer exists must not run gh as the completing
    process's ambient login: classification `auth` naming the profile, gh never invoked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # any gh spawn would fail as infra
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-ghost", completion_contract="acme/repo", assignee="ghost")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'ghost'" in receipt["detail"] and "cannot be resolved" in receipt["detail"]


# --- contract waiver: an OWNER/REPO contract on a repo with NO required checks (agent-650ad5206a) ---
# Such a contract could never pass acceptance and nothing could change it after creation, so workers
# stalled and then rewrote completion_contract in kanban.db by hand. The supported way out is
# metadata.contract_waiver, honoured only when a fresh read shows no required or reported CI.

_PR7 = "https://github.com/acme/repo/pull/7"
_WAIVER = {"reason": "repo has no CI; tests ran locally"}


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


@pytest.mark.platforms("linux")
def test_no_required_checks_names_the_waiver_and_waiver_completes(github):
    from hermes_cli.kanban_origin_ledger import observe_owner_task

    github.update(no_required=True, no_ci=True)
    with connect() as conn:
        tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": _PR7})
        receipt = _events(conn, tid, "pr_acceptance")[-1]
        assert receipt["classification"] == "no_required_checks"
        assert "contract_waiver" in receipt["detail"]
        assert "contract_waiver" in kb.get_task(conn, tid).last_failure_error
        assert not _events(conn, tid, "contract_waived")
        # An empty reason is not a waiver.
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": _PR7, "contract_waiver": {"reason": " "}})
        assert kb.complete_task(conn, tid, result="done",
                                metadata={"published_pr": _PR7, "contract_waiver": _WAIVER})
        assert kb.get_task(conn, tid).status == "done"
        waived = _events(conn, tid, "contract_waived")
        assert len(waived) == 1 and waived[0]["reason"] == _WAIVER["reason"]
        assert waived[0]["receipt"]["classification"] == "no_required_checks"
        assert waived[0]["receipt"]["pr_url"] == _PR7
        conn.execute("SAVEPOINT observation")
        try:
            obs = observe_owner_task(conn, {"id": tid, "status": "done"}, now=123)["origin_observation"]
        finally:
            conn.execute("RELEASE observation")
        assert obs["verified"]


@pytest.mark.platforms("linux")
def test_waiver_never_bypasses_configured_required_checks(github):
    with connect() as conn:
        for fault in ("failure", "missing", "pending"):
            github.update(conclusion="failure" if fault == "failure" else
                          "pending" if fault == "pending" else "success", head="a" * 40)
            if fault == "missing":
                github["missing"] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done",
                                        metadata={"published_pr": _PR7, "contract_waiver": _WAIVER})
            assert kb.get_task(conn, tid).status != "done"
            assert not _events(conn, tid, "contract_waived")
            github.pop("missing", None)


@pytest.mark.platforms("linux")
def test_waiver_via_kanban_complete_tool(github, monkeypatch):
    from tools import kanban_tools as kt

    github.update(no_required=True, no_ci=True)
    with connect() as conn:
        tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
        run_id = kb.claim_task(conn, tid).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    refused = json.loads(kt._handle_complete({"summary": "shipped", "metadata": {"published_pr": _PR7}}))
    assert refused.get("ok") is not True and "contract_waiver" in json.dumps(refused)
    out = json.loads(kt._handle_complete({"summary": "shipped",
                                          "metadata": {"published_pr": _PR7, "contract_waiver": _WAIVER}}))
    assert out["ok"] is True
    with connect() as conn:
        assert kb.get_task(conn, tid).status == "done"
        assert _events(conn, tid, "contract_waived")


@pytest.mark.platforms("linux")
def test_waiver_via_cli_complete(github):
    from hermes_cli import kanban as kc

    github.update(no_required=True, no_ci=True)
    with connect() as conn:
        tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
    meta = json.dumps({"published_pr": _PR7})
    refused = kc.run_slash(f"complete {tid} --summary shipped --metadata '{meta}'")
    assert "contract_waiver" in refused, refused
    meta = json.dumps({"published_pr": _PR7, "contract_waiver": _WAIVER})
    kc.run_slash(f"complete {tid} --summary shipped --metadata '{meta}'")
    with connect() as conn:
        assert kb.get_task(conn, tid).status == "done"
        assert _events(conn, tid, "contract_waived")


@pytest.mark.platforms("linux")
def test_create_warns_when_contract_repo_has_no_required_checks(github, monkeypatch):
    from hermes_cli import kanban as kc
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    github.update(no_required=True, no_ci=True)
    out = json.loads(kt._handle_create({"title": "pr work", "assignee": "default", "requires": [],
                                        "completion_contract": "acme/repo"}))
    assert out["ok"] is True and "contract_waiver" in out.get("contract_warning", "")
    cli = kc.run_slash("create 'pr work 2' --completion-contract acme/repo")
    assert "contract_waiver" in cli, cli
    github["no_required"] = False
    out = json.loads(kt._handle_create({"title": "pr work 3", "assignee": "default", "requires": [],
                                        "completion_contract": "acme/repo"}))
    assert out["ok"] is True and "contract_warning" not in out
