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

### Admission identity

`serve_producer_identity.ProcessKey` is the only admission identity: boot ID,
PID-namespace inode (`pid:[...]`), PID, kernel start ticks and launcher-incarnation
digest. The last component hashes the twice-read executable/argv/environment/cwd
observation, including its namespace. Source-boundary generation and installation
are separate validity checks, not substitutes for process identity. A role label
does not create a different process. Receipt replacement, duplicate rejection,
classification, broker liveness, explicit legacy retirement and final census
revalidation use this key. The raw `Incarnation` triple is only a kernel birth
probe: it cannot authorize classification or be supplied as a retirement ticket.

Shared-home foreign-namespace rows are retained verbatim without probing their
PIDs, even on numeric identity collisions. Same-boot foreign evidence keeps the
strict census INCOMPLETE. Uncertain own-namespace retirement (including zombies
and unreadable exec details) retains evidence without breaking ordinary launch;
it does not certify retirement. PID reuse may positively retire an old key after
kernel birth recheck. A changed exec observation alone never does. Missing exec
details require a separate birth recheck. Boot/namespace changes or incoherent
observations refuse retirement. A foreign `/proc` numbering view is refused.

Adversarial review of the key found and covered two additional liveness cases:
an unreadable different-UID successor on a reused PID and a zombie/incomplete
receipt blocking unrelated startup. Both now retain uncertain evidence; readable
kernel reuse remains positive retirement. Tests also cover every key component,
same-namespace duplicates versus two-namespace shared homes, repeated foreign
retention, raw-birth rejection, missing exec versus actual exit and mismatched
observations. The three original independent review probe files are preserved
verbatim alongside the matrix.

This is an observed exec identity, not a kernel exec counter: an unobserved
identical self-exec cannot be distinguished by procfs. Enrollment remains
unguarded; only a live process-local broker object plus kernel flock can promote
it, and that object cannot survive exec. This limitation is not production
activation evidence. Old-boot foreign rows remain retained within the existing
hard ledger budgets; no cross-namespace retirement authority is inferred.

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
