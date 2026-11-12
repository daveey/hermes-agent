"""A block on David must say, in its ask line, what unblock and archive mean (coach pattern
blocked-card-missing-user-ask: "the task shows blocked but doesnt tell me what i am supposed to do")."""
import json

import pytest

VAGUE = {"action": "Pick the second-builder design for bb-pod-iroh: A, B or C (recommended)",
         "why_human": "security architecture choice", "interaction": "reply",
         "scope": "release sign jobs", "evidence": "PR #1809 builds differ on the sign host"}
CLEAR = {**VAGUE, "action": ("Reply A, B or C in this chat for the bb-pod-iroh second builder. Unblock = build "
                             "your pick; archive = skip the second builder and record the accepted risk.")}


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def _task(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return kb.get_task(conn, tid)
    finally:
        conn.close()


@pytest.mark.parametrize("action,scope,interaction,choices,warns", [
    ("Choose fixture timing", "code fixtures", "reply", True, True),
    ("Approve a spend cap of $50", "code fixtures", "reply", True, False),
    ("Grant permission", "code fixtures", "approval", True, False),
    ("Give consent", "code fixtures", "reply", True, False),
    ("Choose fixture timing", "code fixtures", "physical", True, False),
    ("Choose fixture timing", "code fixtures", "reply", False, False),
    ("Choose fixture timing", "privacy consent", "reply", True, False),
    ("Ask the reviewer", "code fixtures", "reply", True, False),
    ("Delete the data", "code fixtures", "reply", True, False),
])
def test_recommended_decision_warning_is_nonfatal(worker_env, action, scope, interaction, choices, warns):
    from tools import kanban_tools as kt
    request = {**CLEAR, "action": action + ". Unblock = proceed; archive = skip.",
               "scope": scope, "interaction": interaction,
               "why_human": "the worker asked instead of applying its pick", "evidence": "fixture timing notes",
               "choices": [{"label": "Postmerge (recommended)" if choices else "Postmerge", "value": "post"},
                           {"label": "Premerge", "value": "pre"}]}
    result = json.loads(kt._handle_block({"reason": "Fixture timing", "kind": "needs_input",
                                       "human_request": request}))
    assert result.get("ok") is True, result
    assert _task(worker_env).status == "blocked"
    assert ("apply the recommended option" in result.get("warning", "")) is warns


@pytest.mark.parametrize("hr", [None, [], {}, {"interaction": "reply"},
    {"interaction": "reply", "choices": "recommended"},
    {"interaction": "reply", "choices": ({"label": "A (recommended)"},)},
    {"interaction": "reply", "choices": [None, {"label": "ordinary"}]}])
def test_recommendation_warning_ignores_missing_or_malformed_choices(hr):
    from hermes_cli.kanban_block_policy import recommended_choice_warning
    assert recommended_choice_warning(hr) == {}


@pytest.mark.parametrize("field,text", [
    ("why_human", "This exceeds the approved budget"),
    ("evidence", "The option requires permission"),
    ("label", "Grant admin access (recommended)"),
    ("value", "privacy-consent"),
])
def test_recommendation_boundary_in_explanations_and_choices(field, text):
    from hermes_cli.kanban_block_policy import recommended_choice_warning
    request = {"interaction": "reply", "action": "Choose A or B", "scope": "code fixtures",
               "choices": [{"label": "A (recommended)", "value": "a"}]}
    if field in {"label", "value"}:
        request["choices"][0][field] = text
    else:
        request[field] = text
    assert recommended_choice_warning(request) == {}


@pytest.mark.parametrize("label,warns", [
    ("A (not recommended)", False), ("A [Recommended]", True),
    ("A (RECOMMENDED)", True), ("A is not recommended", False),
])
def test_recommendation_requires_explicit_positive_marker(label, warns):
    from hermes_cli.kanban_block_policy import recommended_choice_warning
    result = recommended_choice_warning({"interaction": "reply", "action": "Choose the poller retry backoff",
                                         "choices": [{"label": label}]})
    assert bool(result) is warns
    if warns:
        assert "reversible" in result["warning"]
        assert "existing review and landing gates" in result["warning"]
        assert "this run still ends" in result["warning"]
        # Post-block reality: David is already asked, so the worker comments its pick; it must not apply it now.
        assert "do not apply the option now" in result["warning"]
        assert "comment your recommended pick and how to reverse it" in result["warning"]


# Asks that really need David which a denylist nudged anyway: PR1220 reviews t_01a1ce72aa3e4cb0 (round 1)
# and t_01a1ce964f2521ad (round 2, the 35 recorded misses). Each must stay quiet both on the fail-safe path
# (neutral scope) and on the veto path (an in-gate scope, which never qualifies an ask on its own). [daveey]
_DAVID_ONLY_ASKS = [
    "Choose the billing plan for the new GPU pod", "Rent an H100 for 48 hours",
    "Upgrade the subscription to Pro tier", "Charge the card on file",
    "Pick which API key to rotate", "Log in to the App Store Connect account", "Revoke the GitHub app",
    "Send the email to the landlord", "Post the draft publicly on X", "Post the draft on X",
    "Publish the App Store release",
    "Tell David's wife about the schedule", "Ask Alice to review",
    "Remove the old backups from S3", "Wipe the hub database and reseed", "Drop the users table",
    "Force-push main to drop the bad commit", "Merge the PR now without CI",
    "Buy the softmax.ai domain", "Register the new domain name", "Order a replacement Hue bridge",
    "Book the flight to SF", "Upgrade to the paid plan", "Cancel the AWS reservation",
    "Terminate the old EC2 instances", "Tear down the staging cluster for good", "Decommission the hub server",
    "Accept the new terms of service", "Agree to the Apple developer agreement", "Share the doc with Bob",
    "Reply to the recruiter", "Reply to Bob's PR comment", "Submit the build for App Store review",
    "Make the repo visible to everyone", "Unlock the front door", "Open the garage door",
    "Turn off the nursery camera", "Disable the baby monitor", "Disable branch protection on main",
    "Power-cycle the mini", "Spin up 8 A100s on Lambda", "Renew the domain for 5 years",
    "Donate to the campaign", "Merge without waiting for checks", "Rewrite main history", "Archive the repo",
    "Downgrade the database plan", "Restore the database from yesterday's snapshot", "Clear the prod cache",
    "Turn the thermostat down to 60", "Upgrade the Claude Max plan", "Kill the 3-day training run",
    "Approve the Discord bot install",
    # plan-review extras: mixed in-gate + boundary, lowercase names, handles
    "Disable the feature flag in prod", "Rename the helper and buy the paid plan",
    "Fix the test, then deploy to production", "Ask david about the port", "Open a PR and then force-push main",
    "Ping @sam about the retry backoff",
    # held-out probe (written after the round-3 design; 1 of 27 nudged before this term landed)
    "Choose whether to merge the PR with failing required checks",
]


@pytest.mark.parametrize("scope", ["this card", "code in this card's PR"])
@pytest.mark.parametrize("action", _DAVID_ONLY_ASKS)
def test_recommendation_nudge_stays_quiet_for_david_only_asks(action, scope):
    from hermes_cli.kanban_block_policy import recommended_choice_warning
    request = {"interaction": "reply", "action": action, "scope": scope,
               "choices": [{"label": "A (recommended)", "value": "a"}, {"label": "B", "value": "b"}]}
    assert recommended_choice_warning(request) == {}


@pytest.mark.parametrize("request_patch", [
    # neither list: no in-gate signal and no boundary term
    {"action": "Choose a number"}, {"action": "Pick option A or B"},
    # in-gate scope alone never qualifies
    {"action": "Choose a number", "scope": "code in this card's PR"},
    # substring traps: report/support/export/Portland are not PR/port signals
    {"action": "Report the support export to Portland"},
    # a boundary term in a non-recommended choice or in the explanation still vetoes
    {"action": "Pick the retry backoff", "extra_choice": "Email the vendor instead"},
    {"action": "Increase the timeout", "why_human": "David owns the AWS bill"},
])
def test_recommendation_nudge_is_fail_safe(request_patch):
    from hermes_cli.kanban_block_policy import recommended_choice_warning
    patch = dict(request_patch)
    choices = [{"label": "A (recommended)", "value": "a"}, {"label": "B", "value": "b"}]
    if "extra_choice" in patch:
        choices.append({"label": patch.pop("extra_choice"), "value": "c"})
    request = {"interaction": "reply", "scope": "this card", "choices": choices, **patch}
    assert recommended_choice_warning(request) == {}


# In-gate code decisions from the red-sweep evidence (and similar) still get the nudge.
@pytest.mark.parametrize("action", [
    "Choose fixture timing: premerge or postmerge",
    "Pick the coworld-terraform module layout: D1 c and D2 a",
    "Use worker Chrome :9333 instead of toggling remote debugging",
    "Pick the retry backoff for the poller",
    "Rename the helper module and keep a shim",
    "Pick the lint rule severity: error or warn",
    "Choose the regex for the parser: strict or lenient",
    "Use a worktree per task instead of a shared checkout",
])
def test_recommendation_nudge_still_fires_for_in_gate_code_decisions(action):
    from hermes_cli.kanban_block_policy import recommended_choice_warning
    request = {"interaction": "reply", "action": action, "scope": "code in this card's PR",
               "choices": [{"label": "A (recommended)", "value": "a"}, {"label": "B", "value": "b"}]}
    assert "apply the recommended option" in recommended_choice_warning(request).get("warning", "")


# The kanban_block schema's own ask template must not veto an in-gate ask (review t_01a1cea8ae43e248:
# "reply" and "drop" vetoed 17/17), while a boundary outside the template still does. [daveey]
@pytest.mark.parametrize("action,warns", [
    ("Pick the retry backoff for the poller. Reply A or B in this chat. Unblock = build the pick; "
     "archive = drop it.", True),
    ("Choose fixture timing. Reply with A in this chat. Unblock = proceed; archive = delete the branch.", True),
    ("Pick the retry backoff. Reply A or B in this chat. Unblock = drop the users table; archive = skip.", False),
    ("Reply to the recruiter about the PR in this chat. Unblock = proceed; archive = skip.", False),
])
def test_recommendation_nudge_ignores_the_canonical_ask_template(action, warns):
    from hermes_cli.kanban_block_policy import recommended_choice_warning
    request = {"interaction": "reply", "action": action, "scope": "this card",
               "choices": [{"label": "A (recommended)", "value": "a"}, {"label": "B", "value": "b"}]}
    assert bool(recommended_choice_warning(request)) is warns


def test_recommendation_warning_is_conditional_not_a_verdict():
    """No regex can classify David-only asks, so a misfire must be harmless: the note never claims the
    ask was in-gate and names every boundary as a reason to ignore it."""
    from hermes_cli.kanban_block_policy import RECOMMENDATION_WARNING as text
    assert "looks like" not in text and "could have applied" not in text
    assert "If any of these applies, ignore this note" in text and "does not judge this ask" in text
    for boundary in ("spend", "permissions or credentials", "other people or outward communication",
                     "safety or privacy consent", "production systems or devices", "hard-to-reverse",
                     "toss-up", "grants no permission"):
        assert boundary in text


def test_worker_guidance_applies_recommendation_with_boundaries():
    from agent.prompt_builder import KANBAN_GUIDANCE
    for phrase in ("Comment the pick and how to reverse it", "permissions or credentials",
                   "safety or privacy consent", "hard-to-reverse", "toss-up"):
        assert phrase in KANBAN_GUIDANCE


def test_block_with_vague_ask_is_refused(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_block({"reason": "Waiting on the design pick", "kind": "needs_input",
                                     "human_request": VAGUE}))
    assert "error" in d and d.get("rejection") == "user_ask_incomplete"
    assert "unblocking" in d["error"] and "archiving" in d["error"]
    assert _task(worker_env).status == "running"


def test_block_with_unblock_and_archive_meaning_lands(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_block({"reason": "Waiting on the design pick", "kind": "needs_input",
                                     "human_request": CLEAR}))
    assert d.get("ok") is True, d
    t = _task(worker_env)
    assert t.status == "blocked" and CLEAR["action"] in json.dumps(t.human_request)


def test_block_without_human_request_is_not_this_lint(worker_env):
    """Non-human waits keep their own policy; this lint only reads a supplied ask."""
    from hermes_cli.kanban_block_policy import user_ask_lint_error
    assert user_ask_lint_error(None) is None
    assert user_ask_lint_error(CLEAR) is None
    assert "archiving" in user_ask_lint_error({**CLEAR, "action": "Unblock to proceed"})


@pytest.mark.parametrize("premise, probe", [
    ("Merge https://github.com/example/project/pull/42", "gh pr view 42 -R example/project"),
    ("Remove /var/cache/example-build", "test -e /var/cache/example-build"),
    ("Install build 649; version read: `device-info --version`", "device-info --version"),
])
def test_machine_premise_warns_without_refusing(worker_env, premise, probe):
    from tools import kanban_tools as kt
    ask = {**CLEAR, "action": premise + ". Unblock = proceed; archive = drop it."}
    result = json.loads(kt._handle_block({"reason": "Human step", "kind": "needs_input",
                                         "human_request": ask}))
    assert result.get("ok") is True, result
    assert _task(worker_env).status == "blocked"
    assert probe in result["resolved_check_warning"]
    assert "resolved_check" in result["resolved_check_warning"]


@pytest.mark.parametrize("extra", [
    {"action": "Press the door-release button. Unblock = proceed; archive = drop it.", "evidence": "Door is closed"},
    {"evidence": "https://github.com/example/project/pull/42", "resolved_check": "false"},
])
def test_no_premise_warning_for_physical_or_probed_asks(worker_env, extra):
    from tools import kanban_tools as kt
    result = json.loads(kt._handle_block({"reason": "Human step", "kind": "needs_input",
                                         "human_request": {**CLEAR, **extra}}))
    assert result.get("ok") is True, result
    assert "resolved_check_warning" not in result


@pytest.mark.parametrize("evidence, expected", [
    ("See https://github.com/example/project/pull/42", "MERGED|CLOSED"),
    ('Remove `/var/cache/build files`', "test -e '/var/cache/build files'"),
    ('Remove "/var/cache/build files"', "test -e '/var/cache/build files'"),
    ("Remove '/var/cache/build files'", "test -e '/var/cache/build files'"),
    ("Install version v649", "existing version read"),
    ("Device needs ios-app1655", "existing version read"),
])
def test_evidence_premises_preserve_probe_targets(worker_env, evidence, expected):
    from tools import kanban_tools as kt
    result = json.loads(kt._handle_block({"reason": "Human step", "kind": "needs_input",
                                         "human_request": {**CLEAR, "evidence": evidence}}))
    assert result.get("ok") is True, result
    assert expected in result["resolved_check_warning"]


@pytest.mark.parametrize("ask", [None, {}, {"action": "See https://example.com/docs"},
    {"action": "https://github.com/example/project/pull/42oops"}])
def test_no_machine_premise_has_no_advice(ask):
    from hermes_cli.kanban_ask_premise import warning_fields
    assert warning_fields(ask) == {}


def test_create_blocked_with_vague_ask_is_refused(worker_env, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({"title": "Decide: second builder", "initial_status": "blocked",
                                      "human_request": VAGUE, "requires": [], "placement": "local"}))
    assert "error" in d and "user_ask_incomplete" in d["error"], d
