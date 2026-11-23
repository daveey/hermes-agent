---
title: "Replay Current-Row Recovery"
description: "Inspect and explicitly retry a paused, journal-bound Kanban current-image read"
---

# Replay current-row recovery

This applies only to an already-bound replay journal. It does not enroll a Board,
activate replay, or replace normal sync. Check `hermes kanban --board BOARD sync status
--json`: `replay_fault` names the retained operation, sequence, journal, task,
reason, attempt count, retry deadline and recovery command.

A known final replay verdict and a separately authenticated **current** row are
different evidence. Unknown, deleted, malformed or locally tombstoned current rows
never authorize fabricated absence, a historical image or silent child release.
Transport stays frozen. A final deferred status reference is retained independently
of whether the current-row read or projection succeeds; it is not a status-applied
receipt. Projection failures still roll back transport settlement and parent retirement.

Network read failures back off exponentially, capped at 60 seconds, and stop after
eight failed reads. Offline elapsed time does not add attempts. Unusable current
images stop immediately. The persistent fault is logged once when paused and is
returned by sync status; automatic pushes do not retry a paused stream. Other
Boards/streams continue normally. This is not durable lag/observer-callback delivery.

The owner should inspect the serving Board and reconcile the prerequisite using
the existing recovery lane. If a deleted row is intentional, do not recreate it
just to make the read succeed: resolve the retained intent under its existing
authority. After the prerequisite is verified, explicitly permit another exact read:

```sh
hermes kanban --board BOARD sync replay-resume --expect OP_ID --reason "verified prerequisite"
```

This checks the saved journal/generation/sequence/operation identity under the writer
reservation. It clears only that read hold. It does not resend immediately, invent
a new operation, delete custody, cancel work or acknowledge the Board. The next
normal push inspects and recovers the exact verdict without resending known work.
A mismatching journal/operation requires recovery of the saved journal, not manual
fault-table deletion or a guessed `--expect` value.
