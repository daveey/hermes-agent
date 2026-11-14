"""Holder narrow gate: a liveness-only move is not refreshed; header-only confirmation keeps display adoption
current; a substantive move refreshes after the floor; renewal and own mutation still force reads. [daveey]"""
import pytest

from hermes_cli import kanban_annotation_disk as disk
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_host as helpers

world = helpers.world
SCOPE = helpers.SCOPE


@pytest.fixture
def gate(world, monkeypatch):
    state = {"key": "K1", "calls": 0}

    def narrow_now(scope, full, fp):
        state["calls"] += 1
        return state["key"] if fp is not None else None

    monkeypatch.setattr(host, "_narrow_now", narrow_now)
    helpers._poll()
    host.demand(SCOPE)
    host.kick(SCOPE)  # first holder read publishes a narrow key (a poll's copy carries none)
    assert host._holder_refresh_scope(SCOPE)
    return state


def _header():
    out: dict = {}
    assert disk.load_host(SCOPE, shared=True, header_out=out) is not None
    return out


def _move(world):
    world["conn"].execute("INSERT INTO tasks VALUES ('t9', 'ready', 's1')")


def test_holder_publishes_narrow_key(gate):
    header = _header()
    assert header["narrow_key"] == "K1"
    assert header["verified_at"] == header["read_at"]


def test_poll_publication_carries_no_narrow_key(world, monkeypatch):
    monkeypatch.setattr(host, "_narrow_now", lambda *a: pytest.fail("poll must not pay for a narrow key"))
    helpers._poll()
    assert _header().get("narrow_key") is None


def test_moved_inputs_unchanged_narrow_key_no_refresh_header_confirmed(world, gate):
    read_at = _header()["read_at"]
    _move(world)
    helpers._advance(world, 10)
    assert not host._holder_refresh_scope(SCOPE)
    header = _header()
    assert header["read_at"] == read_at  # strict read time never moves without a read
    assert header["verified_at"] == read_at + 10
    helpers._advance(world, 5)  # 15 s after the read: past DISPLAY_MAX_AGE_S, 5 s after confirmation
    helpers._other_process()
    assert host.adopt_display(SCOPE) is not None


def test_strict_adopt_stays_exact_after_confirmation(world, gate):
    _move(world)
    helpers._advance(world, 10)
    assert not host._holder_refresh_scope(SCOPE)
    helpers._other_process()
    assert host.adopt(SCOPE) is None  # fingerprint moved: the exact caller reads for itself


def test_unconfirmed_copy_ages_out_for_display(world, gate):
    _move(world)
    helpers._advance(world, 13)
    helpers._other_process()
    assert host.adopt_display(SCOPE) is None


def test_substantive_change_refreshes_after_floor(world, gate):
    _move(world)
    gate["key"] = "K2"
    helpers._advance(world, 2)
    assert not host._holder_refresh_scope(SCOPE)  # floor
    helpers._advance(world, 8)
    assert host._holder_refresh_scope(SCOPE)
    assert _header()["narrow_key"] == "K2"
    assert ksa._last_annotation[SCOPE][0]["s1"] == 3


def test_unknown_narrow_key_is_moved(world, gate):
    _move(world)
    gate["key"] = None
    helpers._advance(world, 10)
    assert host._holder_refresh_scope(SCOPE)


def test_renewal_still_forces_read(world, gate):
    _move(world)
    helpers._advance(world, 10)
    assert not host._holder_refresh_scope(SCOPE)
    helpers._advance(world, 10)
    assert host._holder_refresh_scope(SCOPE)


def test_own_mutation_bypasses_narrow_gate(world, gate):
    _move(world)
    helpers._advance(world, 2)
    host.kick(SCOPE)
    assert host._holder_refresh_scope(SCOPE)


def test_unmoved_inputs_skip_narrow_key(world, gate):
    calls = gate["calls"]
    helpers._advance(world, 12)
    assert not host._holder_refresh_scope(SCOPE)
    assert gate["calls"] == calls  # exact fingerprint equal: no narrow read at all


def test_floor_skips_narrow_key(world, gate):
    calls = gate["calls"]
    _move(world)
    helpers._advance(world, 4)
    assert not host._holder_refresh_scope(SCOPE)
    assert gate["calls"] == calls


def test_confirm_refuses_other_key(world, gate):
    assert not disk.confirm_host(SCOPE, "K-other", world["wall"])
    assert disk.confirm_host(SCOPE, "K1", world["wall"])
    assert not disk.confirm_host(SCOPE, "K1", world["wall"] + 60)  # never ahead of now
