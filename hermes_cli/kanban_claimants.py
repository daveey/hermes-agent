"""Which enrolled machines could ever claim a card, structurally. [daveey]

Incident t_01a1f867bac2344e (2026-11-20): a hub card with ``runs_on=["macos"]`` sat ``ready`` for 24 h. Both macos
machines (macbook, mini) are ``pinned_only`` and advertise no pickup, so they never copy another gateway's card
(``kanban_worker_node.eligible``: ``pinned_only``), and no pool worker carries ``macos``. The create-time check and
``kanban_creator_repin.stranded_refusal`` only asked whether SOME machine carries the labels, so the card was
neither refused nor re-routed; the queue text even said "waiting for macbook, mini".

This is the claimant contract those checks share. Temporary states (hold, drain, quota, offline) are waits, not
structure, and never appear here.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

Machines = Mapping[str, Mapping[str, Any]]


def refusals(labels: Optional[list[str]], machines: Optional[Machines], home: str, *,
             pins: bool = True) -> dict[str, str]:
    """``{machine: reason}`` for each machine carrying every label that could never claim a card homed on ``home``.

    A ``pinned_only`` machine keeps other gateways' work off itself unless the card pins it (``machine:<name>``),
    and a pin only works when the machine also advertises pickup (``pins=False`` leaves that case to
    ``kanban_worker_node.check_machine_pins``, whose ``placement_unavailable`` refusal callers already surface).
    The home gateway's own entry is never refused."""
    from hermes_cli import kanban_worker_node as wn
    wanted, out = set(labels or ()), {}
    for name, entry in (machines or {}).items():
        if name == home or not (entry or {}).get("pinned_only") or not wanted <= wn.effective_labels(entry, name):
            continue
        if f"machine:{name}" not in wanted:
            out[name] = "pinned_only"
        elif pins and not wn.advertises_pickup(entry):
            out[name] = "pinned_only, no pickup"
    return out


def claimants(labels: Optional[list[str]], machines: Optional[Machines], home: str) -> list[str]:
    """Machines that carry every label and could, structurally, claim a card homed on ``home``."""
    from hermes_cli import kanban_worker_node as wn
    wanted, refused = set(labels or ()), refusals(labels, machines, home)
    return sorted(name for name, entry in (machines or {}).items()
                  if name not in refused and wanted <= wn.effective_labels(entry, name))


def message(labels: list[str], machines: Machines, home: str) -> str:
    """Why ``labels`` can never be claimed from ``home``: each carrier's reason, then the cross-gateway remedy."""
    why = refusals(labels, machines, home)
    if not why:
        return (f"runs_on={labels}: no enrolled machine carries all these labels. Nothing was written. "
                "Use placement='<gateway>' to name a gateway, or runs_on=['machine:<registered-name>'] "
                "to pin a Board machine; bare strings are capability labels, not gateway aliases.")
    named = "; ".join(f"{n}: {r}" for n, r in sorted(why.items()))
    return (f"runs_on={labels}: no enrolled machine can claim this card ({named}). Those machines run only cards "
            f"created on their own gateway and never pick up {home}'s. Nothing was written. Remedy: create the "
            f"card on that machine's own gateway (cross-gateway create: placement='{sorted(why)[0]}'), or drop "
            "the label. A machine:<name> pin from here does not help unless that machine advertises pickup.")


def stranded_text(task_id: str, labels: list[str], why: Mapping[str, str]) -> str:
    """Chat/alarm sentence for a READY card nobody can claim: plain, with each candidate's reason and the remedy.
    ``why`` is :func:`refusals` (empty: no machine carries the labels at all)."""
    named = ", ".join(f"{n}: {r}" for n, r in sorted(why.items())) or "no machine carries these labels"
    return (f"NO MACHINE CAN CLAIM {task_id} (runs_on {labels}): {named}. Waiting will not start it. Remedy: "
            + (f"re-create it on {sorted(why)[0]}'s own gateway (cross-gateway create, placement='{sorted(why)[0]}'), "
               if why else "re-place it onto labels a machine carries (`kanban_move`), ")
            + "or drop the label.")
