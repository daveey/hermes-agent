# Unloaded notification diagnostics

These are **observations**, not scheduling inputs. No eligibility, retry interval,
settlement, expiry, wake, cadence or memo-lifetime policy is changed. Receipts use
`hermes_cli.loop_accounting`; all label vocabularies are finite literals in
`tui_gateway/unloaded_diagnostics.py` and
`gateway/notification_retry_provenance.py`. Unknown values never become metric
names. Registry capacity overflow remains the existing `_overflow` counter.

## Units and conservation

All metric names start with `unloaded.diag.`:

| Family | Unit / meaning |
| --- | --- |
| `host.primary.<admission>` | One per actual `unloaded.host_pass`, including exceptions; lexical per-thread CPU overlay |
| `host.exit.{complete,exception,unknown}` | One per host pass |
| `host.due.{fact,retry,due,maintenance,unknown}` | Nonexclusive admission flags |
| `host.fact.<kind>` | Source-kind observations consumed by actual action admission; kinds are health, TaskFact, SourceState, SessionFact, ChatWaitingFact, reset, unknown |
| `retry.site.<reason>` | Actual local card-change incompletion site |
| `remote.call.<outcome>` | One per entered remote-change call |
| `remote.miss.<test>.<status>` | Independent evaluated miss tests; true, false, not_evaluated or unknown |
| `remote.chat.trigger.<trigger>` | One per chat actually iterated |
| `remote.chat.result.<result>` | One per chat actually iterated |
| `remote.chat.flag.health_pending` | Independent health observation on iterated chats |
| `remote.cert.<outcome>` | One certification-stage receipt per entered remote call; unreached is not_attempted |
| `remote.cert.flag.<failure>` | Independent simultaneous prints_none / after_poll_none / cursor_moved flags, or unknown |
| `remote.health.<outcome>` | One health-stage receipt per entered remote call; unreached is not_reached |
| `remote_notices.fallback.<reason>` | One per actual remote-notices accounting fallback |
| `remote_notices.pending_detail.<detail>` | Optional bounded downstream subreason for other_pending |
| `instrumentation.unknown` | Invalid family, without fabricating a remote invocation |

Absent overflow, sum primary runs = host-pass runs = sum host exits;
sum remote call outcomes = sum certification outcomes = sum health outcomes;
sum chat triggers = sum chat results = actual iterated chats;
sum remote-notices primary reasons = actual remote-notices fallbacks.
Nonpending change/skip accounting does not emit a fallback.

Host primary precedence is fact > retry deadline > subpass due_at > maintenance >
unknown. Within retry/due, choose earliest deadline then lexical bounded subpass
and reason. Simultaneous flags stay separate. Raw wake hints and catch-up role
transitions are not fact provenance. Facts track the actual delta kinds consumed
by ActionWake; missing provenance is unknown. Retry/due provenance is optional on
reports/results and does not alter `next_due` or immediate retry(0).

Primary CPU wraps the actual host function lexically, including early returns and
BaseException. It is a partition **overlay**: never add it to parent CPU or nested
subpass CPU. Parent/partition instrumentation overhead differs slightly; compare
partition CPU across admission reasons, not the overhead difference to the parent.
Count equality requires no registry overflow.

Remote refreshed means attempted refresh, not memo certification. The second
poll still occurs after fingerprint collection even if prints are None. Existing
per-board cursor comparisons, seed/ack behavior and health acknowledgement order
remain intact. Unevaluated short-circuit tests are never reported false. Health,
certification, chat and call receipts are different units. Certification entry is
observed immediately before projection acknowledgment, and ends after the final
certification decision, before health acknowledgment. Earlier projection acquisition,
iteration, load or seed failures retain `not_attempted`; failures inside that interval
emit `exception`. A later health failure keeps the finalized certification outcome
(including a successful memo assignment).

## Proof surface

Canonical tests enter real scheduler, host wrapper, gates, card fanout and remote
fanout paths using isolated fake fact stores and SQLite fixtures. Diagnostic test
fixtures export `counter_snapshot` JUnit properties for reproducible raw stage
snapshots. Use a distinct `--junitxml` path for each file; canonical per-file workers
would overwrite a shared path. Supplement composed tests with static vocabulary,
legacy fake-report signatures, deterministic deadline ties, overflow and context
isolation controls. Existing accounting/scheduler/fanout consumers remain required.
