"""Pure, whole-candidate-set lane ordering shared by live admission and replay.

This is not an eligibility evaluator or a trace-completeness certificate.
Candidate lane values must be canonical policy lane names, not raw tenants.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Partition:
    """Immutable queue structure; row/breakdown objects remain caller-owned."""

    front: tuple
    queues: tuple
    paused: tuple


def prepare(scored, pol, candidate_lanes, alarms) -> Partition:
    """Partition in score order, retaining the live missing-card default."""
    front, queues, paused = [], {}, []
    for index, item in enumerate(scored):
        row, parts = item
        lane = pol.lanes[candidate_lanes.get(row["id"], pol.default)]
        if parts.turn_owner or parts.class_rank > 0 or row["id"] in alarms:
            front.append(item)
        elif lane.paused:
            paused.append(item)
        else:
            queues.setdefault(lane.name, []).append((index, item))
    return Partition(tuple(front), tuple((name, tuple(queue)) for name, queue in queues.items()), tuple(paused))


def order_prepared(partition: Partition, pol, running) -> list:
    """Order a pending set without mutating its partition or observed loads.

    Pass the same unchanged policy used by prepare; this synchronous helper
    does not attest policy identity or freeze caller-owned rows for storage.
    Every ordinary listed candidate consumes a lane pick, even if a later claim
    gate refuses it. Multiple queues require explicit running counts; missing
    counts raise KeyError rather than inventing zero occupancy. Zero/one queue
    needs no running observation, preserving the live lazy-read boundary.
    """
    queues = {name: list(queue) for name, queue in partition.queues}
    if len(queues) < 2:
        return list(partition.front) + [item for queue in queues.values() for _i, item in queue] + list(partition.paused)
    load = dict(running)
    out = []
    while queues:
        def cost(name):
            share = pol.lanes[name].share
            return (load[name] / share if share > 0 else float("inf"), queues[name][0][0])
        name = min(queues, key=cost)
        out.append(queues[name].pop(0)[1])
        load[name] += 1
        if not queues[name]:
            del queues[name]
    return list(partition.front) + out + list(partition.paused)
