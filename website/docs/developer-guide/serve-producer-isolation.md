# Dormant serve producer isolation

This preparatory implementation is not a production activation path. It exercises
one session-panel Feed/CoreReaders projection in an exec child, with an immutable
prepared-wire relay serving POST `/changes`. Paired plugin changes live in the
`serve-producer-panel` hermes-mods topic branch. Neither half should land alone.

## Custody and admission

Supported launch entrypoints enroll before producer imports. Enrollment is only a
ticket: persisted `guarded=True` is rejected. The Linux fixture broker enrolls as
`producer_broker`, then acquires the stable panel family flock under LaunchGate
before importing a provider. Its child inherits the same open file description;
closing the broker descriptor never unlocks a surviving child.

`BrokerEnrollment` can classify only its own exact enrolled process as a
participant. It rechecks boot/birth/exec observation, installation and source
boundary generation, canonical runtime and live kernel flock on its descriptor.
A closed/reused descriptor, a different PID, a boolean readiness claim, or an
ordinary serve cannot promote a launch ticket. It does not certify remote serves
or producer children. Those still need an explicit custody bootstrap before a
future production admission cut can include them as participants.

`inspect_host(..., broker=...)` composes this evidence with strict collectors.
Legacy or unknown processes, inaccessible observations, corrupt or missing
ledgers, and unresolved supervisors still keep the cut INCOMPLETE. A COMPLETE
fixture cut is not a host activation receipt. Cuts are scoped to a held launch
acquisition; future production authorization must revalidate custody through
launch, not reuse a cached COMPLETE after custody closes.

## Current boundary

Only `ProjectionRelay.start_for_fixture` starts this skeleton, and it refuses
non-pytest callers. No production startup method or release flag is introduced.
Non-Linux fixtures retain POSIX family custody without claiming Linux enrollment.
No rollout, serve restart, panel freshness bound, or CPU/latency acceptance is
claimed here. Board, decision, service and websocket parity, public provider
manifest, guarded serve/child admission, drain inventory and production launch
remain subsequent gates. Links and annotation remain in-process.

## Tests

Run `scripts/run_tests.sh tests/hermes_cli/test_serve_producer_*.py` using explicit
file expansion, plus the paired session-panel `test_exec_panel_projection.py`.
The suites cover real exec production and endpoint bytes, request-side producer
tripwires, crash restart, blocked-child control responsiveness, inherited family
custody, rollback custody, bounded queues and browser 75/900-second durations
anchored at HTTP acceptance. Enrollment tests combine actual kernel locks with a
controlled hub-shaped census; the real broker fixture independently reads its
published ticket and kernel descriptor. No test establishes COMPLETE on a live
production host.
