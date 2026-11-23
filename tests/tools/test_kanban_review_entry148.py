"""Entry148/150 bounded T3/N2/N5 controls; no production or courier changes.

T3 has two separate groups:
- Routing-disabled baseline (``test_stored_malformed_review_class_refuses_tier_admission``). The isolated
  worker profile has no enabled ``model_routing``, so ``class_unroutable_reason`` returns a policy reason
  (routing disabled) before it checks class membership. A valid class would refuse the same way, so this
  group does NOT tell malformed from valid. It only pins that the unchanged baseline refuses.
- Routing-enabled discrimination (``test_enabled_routing_stored_review_class_*``). One configured fixture
  context, identical apart from the stored ``review_model`` value. A valid exact class gets its review
  route through tier admission and dispatch. A malformed class refuses (``unknown_class``) before any
  receipt, claim, run or spawn.

MCP uses the real stdio server/client. It needs the optional ``[mcp]`` extra, so it is gated by the
shared ``require_mcp_2_sdk`` fixture and skips (rather than errors) without it. It proves only refusal
ordering; a successful MCP handoff is not exercised. acp_adapter/tools.py (event rendering) and
hermes_cli/plugins_dispatch.py (plugin hooks) are not request-review dispatchers. No ACP session, LLM
loop or plugin-hook tool loop is started here, so those transports remain unproven.

Fixture coupling is confined to the block below. Fixtures and public helpers are imported from their
owning modules. Snapshot, dispatch and two-gateway setup helpers are local to this module, so it does
not depend on private helpers of other consumer test modules.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

# --- external fixture/helper coupling (owning modules only) -----------------------------------
from tests import test_kanban_extraction141_review as review  # owns the ``effects`` spy fixture
from tests.hermes_cli import test_kanban_review_model as routing  # enabled-routing config helpers
from tests.hermes_cli import test_kanban_strong_tier as strong_tier  # owns the ``board`` fixture
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, WORKER, Gateway
from tests.tools import test_kanban_tools as worker_fixtures  # owns the ``worker_env`` fixture

worker_env = worker_fixtures.worker_env
effects = review.effects
board = strong_tier.board
# -----------------------------------------------------------------------------------------------

_TABLES = ("tasks", "task_events", "task_comments", "task_links", "task_attachments", "task_runs")


def _snapshot() -> dict:
    """Kanban rows of the current HERMES_HOME board."""
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        return _tables(conn, _TABLES)


def _tables(conn, names=None) -> dict:
    """Every row of *names* (default: every table) on *conn*."""
    if names is None:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: [tuple(r) for r in conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid')] for t in names}


def _dispatch(args: dict):
    """The real registered handler via ``registry.dispatch``; no transport is exercised."""
    import tools.kanban_tools  # registers the kanban toolset
    from tools.registry import registry
    out = registry.dispatch("kanban_request_review", args)
    return json.loads(out) if isinstance(out, str) else out


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def gws(tmp_path, monkeypatch, svc):
    """Two gateways on one fake Board service: the hub creates cards, worker-1 runs them under its lease."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    yield hub, worker
    for g in (hub, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _running_on_worker(hub, worker, svc):
    """A hub card worker-1 claims and runs; the hub reads it running under worker-1's live lease."""
    from hermes_cli import kanban_db as kb
    hub.enter()
    tid = kb.create_task(hub.conn, title="remote work", assignee="default", runs_on=["linux"])
    hub.round()
    worker.round()
    worker.tick()
    assert svc.board(BOARD)["leases"][tid]["holder"] == WORKER
    worker.round()
    hub.round()
    row = hub.conn.execute("SELECT status, lease_holder, claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (row["status"], row["lease_holder"], row["claim_lock"]) == ("running", WORKER, None)
    return tid


@pytest.mark.parametrize("model", ["class:bogus", "class:", "class: deep"])
def test_stored_malformed_review_class_refuses_tier_admission(worker_env, model):
    """Routing-DISABLED baseline only: the refusal is a policy reason checked before class membership.

    A valid class refuses identically here, so this does not discriminate. See the enabled-routing group.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_model_tier as tier
    from hermes_cli.kanban_review_model import ReviewModelUnavailable

    assert _dispatch({"summary": "handoff"}).get("ok") is True
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=? AND outcome='review_requested'",
                     (json.dumps({"review_model": model}), worker_env))
        conn.commit()
    before = _snapshot()
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, worker_env)
        with pytest.raises(ReviewModelUnavailable, match="Review model.*unroutable"):
            tier.apply_model_tier(conn, task, lane="review", cfg={})
    assert _snapshot() == before


# Identical enabled-routing context for every case; only the stored review_model differs.
STORED = ["class:deep", "class:bogus", "class:", "class: deep"]
STORED_IDS = ["valid-deep", "bogus", "empty-suffix", "padded"]


def _enabled_review_card(board, conn, stored):
    """A real claimed handoff on the configured board; its latest handoff run then stores *stored* raw.

    The raw write deliberately bypasses the new ingress guard: this models legacy/stored metadata.
    """
    from hermes_cli import kanban_db as kb
    cfg = routing.config(board)
    on_disk = json.loads(board.joinpath("config.yaml").read_text(encoding="utf-8"))
    assert on_disk["model_routing"]["enabled"] is True
    t = routing.task(conn)
    assert t is not None
    kb.claim_task(conn, t.id)
    assert kb.request_review(conn, t.id, summary="fixture ready")
    conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=? AND outcome='review_requested'",
                 (json.dumps({"review_model": stored}), t.id))
    conn.commit()
    stored_task = kb.get_task(conn, t.id)
    assert stored_task is not None
    return cfg, stored_task


def _events(conn, tid):
    from hermes_cli import kanban_db as kb
    return list(kb.list_events(conn, tid))


@pytest.mark.parametrize("stored", STORED, ids=STORED_IDS)
def test_enabled_routing_stored_review_class_tier_admission(board, stored):
    """Valid exact class gets its review_override route; malformed refuses on class membership, no receipt."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_model_tier as tier
    from hermes_cli.kanban_review_model import ReviewModelUnavailable

    with kbc.connect() as conn:
        cfg, t = _enabled_review_card(board, conn, stored)
        before = _tables(conn, _TABLES)
        count = len(_events(conn, t.id))
        if stored == "class:deep":
            assert tier.apply_model_tier(conn, t, lane="review", cfg=cfg) == "review_override"
            assert (t.model_override, t.provider_override) == (routing.OPUS, "anthropic")
            receipts = [e.payload or {} for e in _events(conn, t.id)[count:] if e.kind == "model_tier"]
            assert len(receipts) == 1, receipts
            assert (receipts[0]["reason"], receipts[0]["model"]) == ("review_override", routing.OPUS)
            assert getattr(kb.get_task(conn, t.id), "model_override", None) == routing.SONNET  # row unchanged
        else:
            with pytest.raises(ReviewModelUnavailable, match=r"unroutable: unknown_class"):
                tier.apply_model_tier(conn, t, lane="review", cfg=cfg)
            assert _tables(conn, _TABLES) == before  # no model_tier/run_route receipt, no row change


@pytest.mark.parametrize("stored", STORED, ids=STORED_IDS)
def test_enabled_routing_stored_review_class_dispatch_admission(board, stored):
    """Through dispatch_once: valid class spawns on its route; malformed refuses preclaim, no claim/run/spawn."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as dispatch

    seen = []
    with kbc.connect() as conn:
        _, t = _enabled_review_card(board, conn, stored)
        runs_before = kb.list_runs(conn, t.id)
        count = len(_events(conn, t.id))
        dispatch.dispatch_once(conn, dry_run=False, spawn_fn=lambda item, ws: seen.append(
            (item.id, item.model_override, item.provider_override)))
        after = kb.get_task(conn, t.id)
        assert after is not None
        new = _events(conn, t.id)[count:]
        kinds = [e.kind for e in new]
        if stored == "class:deep":
            assert seen == [(t.id, routing.OPUS, "anthropic")]
            assert "claimed" in kinds and "review_admission_refused" not in kinds, kinds
        else:
            assert seen == []
            assert kb.list_runs(conn, t.id) == runs_before
            assert not {"claimed", "model_tier", "run_route", "spawned"} & set(kinds), kinds
            refused = [e.payload or {} for e in new if e.kind == "review_admission_refused"]
            assert len(refused) == 1, kinds
            assert refused[0]["preclaim"] is True and "unknown_class" in refused[0]["error"], refused
            assert (after.status, after.claim_lock, after.consecutive_failures) == ("review", None, 0)
            assert after.current_run_id == t.current_run_id


@pytest.mark.parametrize("invalid", [True, False], ids=["invalid-class", "valid-class"])
def test_misscoped_worker_ordering(worker_env, effects, invalid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing() as conn:
        other = kb.create_task(conn, title="other worker's card", assignee="test-worker")
    before = _snapshot()
    result = _dispatch({"task_id": other, "summary": "handoff",
                        "metadata": {"review_model": "class:bogus" if invalid else "class:fast"}})
    assert ("model-neutral" if invalid else "worker is scoped to task") in result.get("error", ""), result
    assert effects == ([] if invalid else ["_worker_guard"])
    assert _snapshot() == before


@pytest.mark.parametrize("invalid", [True, False], ids=["invalid-class", "valid-class"])
def test_foreign_live_lease_precedes_review_metadata(gws, svc, effects, invalid):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    before = _tables(hub.conn)
    result = _dispatch({"task_id": tid, "summary": "handoff",
                        "metadata": {"review_model": "class:bogus" if invalid else "class:fast"}})
    assert result.get("code") == "remote_live_lease", result
    assert result.get("rejection") == "live_claim"
    assert result.get("holder") == WORKER
    assert effects == []
    assert _tables(hub.conn) == before


async def _mcp_review(args, env):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "agent.transports.hermes_tools_mcp_server"],
                                   cwd=Path(__file__).resolve().parents[2], env=env)
    async with asyncio.timeout(90):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=60) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert "kanban_request_review" in {tool.name for tool in tools.tools}
                result = await session.call_tool("kanban_request_review", args)
                assert not result.is_error, result
                return json.loads(next(item.text for item in result.content if item.type == "text"))


@pytest.mark.usefixtures("require_mcp_2_sdk")  # optional [mcp] extra: explicit skip when absent/stale
@pytest.mark.parametrize("invalid", [True, False], ids=["invalid-class", "valid-class-base-refusal"])
def test_mcp_stdio_review_entry_ordering(worker_env, tmp_path, invalid):
    """Bad class wins over missing summary; valid class reaches unchanged summary check (no MCP success)."""
    env = {**os.environ, "HOME": str(tmp_path), "HERMES_HOME": os.environ["HERMES_HOME"],
           "HERMES_QUIET": "1", "HERMES_REDACT_SECRETS": "true"}
    before = _snapshot()
    result = asyncio.run(_mcp_review({"task_id": worker_env, "summary": "",
                                     "metadata": {"review_model": "class:bogus" if invalid else "class:fast"}}, env))
    assert ("model-neutral" if invalid else "summary is required") in result.get("error", ""), result
    assert _snapshot() == before
