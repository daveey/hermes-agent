# Quiet observation: extension-code reporting

Quiet CPU observations attest the core and work/activity custody, not arbitrary
extension code. The raw executed-code ledger still records all its unknowns.
Only the four emission sites for `unrooted-exec:`, `unrooted-module:`,
`generated-origin-unknown:`, and `native-load:` may report external provenance
as informational. Core-root paths (resolved through symlinks), absent/unknown
provenance, generic unknown emissions, and all other kinds remain blocking.
A failed installer-receipt verification for an indexed native dependency also
emits blocking `dependency-unreceipted:`; a native-load diagnostic cannot hide it.
Module provenance is captured when the ledger emits the unknown, not reconstructed
from the mutable module table during measurement.

Successful check/start/snapshot/end responses include `unattested_code`:

- `count`: number of unique informational ledger reasons (not audited executed units);
- `sha256`: SHA-256 of the entire sorted reason list serialized as UTF-8 JSON,
  `ensure_ascii=True`, compact separators `(',', ':')`;
- `entries`: the first 20 strings from that sorted list.

The observer carries the final capture sample's report into its verdict document.
A passing observation with a nonempty report includes the message
`extension code not attested (N units); its CPU is in the numerator`.
CPU remains the entire gateway process's cumulative CPU delta; nothing is
subtracted. Work, epoch/membership change, PID replacement, and threshold failures
retain their existing outcomes.

## Update-window policy

Idle remains admissible. Batching is admissible only at least 120 seconds beyond
the maximum supported quiet lease (480 seconds), so the shared lease-free
check/host preflight and gateway start/snapshot readers use a 600-second bound.
Near/overdue batching, draining, applying, blocked, unrecognized states, foreign
host records and any update hold refuse. Draining/holds appearing during a
capture still invalidate it. The host preflight additionally retains its existing
30-second freshness margin for spawning and handshake.

Schema-1 records do not publish their cadence. The named fallback freshness bound
is 600 seconds plus 60 seconds scheduling jitter = 660 seconds. The publisher is
`hermes-sync.publish_core_window`, which writes `observed_at` on each core pass
and drainer state change. Cadence was checked read-only against state sources:
`hosts/David-MBP/launchd/com.daveey.hermes-sync.plist` (`StartInterval=600`) and
`hosts/hermes-workers/systemd/hermes-core-update.timer` (`OnUnitActiveSec=10min`).
No updater, restart-free docs/tests update behavior, or runtime capture identity
logic is changed.

`quiet-observe-check` also reports `predicates` (active agents, workers, held turn
leases) and `predicate_diagnostics` informationally. Admission is not a quietness
claim; the existing start measurement and subsequent snapshots still require
these predicates to be zero/readable and fence work with the activity epoch.

## Regression evidence and old-test adjustments

The new gateway integration uses the real supported managed launcher, bootstrap,
code ledger, GatewayRunner, registry, and isolated temporary-home quiet lease.
It imports an actual plugin defining a dataclass and loading an absolute libc
with ctypes, admits check/start and valid snapshots, verifies the full digest and
20-entry bound, and refuses a dirty core file. No live lease or Mac suite is used.

Old assertions are retained. Policy-bound fixture inputs necessarily changed:

- `tests/gateway/test_quiet_control.py::write_window`: near batching moved from
  exactly 600 seconds to 599, preserving old refusal/collision test intent.
- `test_start_refused_on_update_markers`: stale age 500 -> 700.
- `test_start_preflight_matches_the_start_refusal_from_host_files`: stale
  parameter 500 -> 700.
- `tests/hermes_cli/test_kanban_quiet.py::_write_window`: near batching 600 -> 599.
- `test_preflight_refuses_a_doomed_start_without_taking_the_lease_or_spending_the_cooldown`:
  stale age 295 -> 700.
- `test_preflight_refuses_a_missing_window_and_a_window_about_to_go_stale`:
  margin-bound age 100 -> 650.
- `tests/hermes_cli/test_code_attestation_relaunch.py::test_check_and_start_agree_on_a_stale_update_window`:
  stale age 600 -> 700.

These fixture-only updates are required by P2's changed policy; leaving those old
inputs untouched would assert the obsolete 120-second freshness and all-batching
refusal policy. No old plugin refusal or attestation assertion was removed.
