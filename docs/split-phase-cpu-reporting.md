# Split-phase registry CPU reporting

When the installation-root CPU gate is ON, the sync reporter holds one snapshot,
then finishes it on the same thread after the one-second window. The existing
wait is clamped only while a snapshot is pending; polling, the 0.5-second outbox
tick, ten-second report cadence and locked admission sample are unchanged.

## Quality diagnostic

The existing `hermes_cli.machine_metrics` INFO logger emits
`machine metrics admission quality: cpu_sample_drops=N` whenever a consumed
pending finish returns no sample (including an invalid/late window or a changed
root limit). N is cumulative for that Reporter instance and resets with it.
It is emitted before transport, including when that transport subsequently fails.
No Board wire fields or timing JSON schema fields are added.

This is **not all lost reports**: an exception before the counter increment or
a send failure after a valid sample is not included. It is an admission-quality
observation, not a latency verdict. Valid samples and OFF single-phase reports
do not emit this diagnostic. Compare only counts from the same instance/lifetime.

On non-Linux with the root gate ON, the unknown initial CPU snapshot still gets
one pending wake and finishes without a triple. This compatibility limitation
is retained: no made-up CPU value, admission fail-open, or native-platform
verification is claimed. OFF remains single phase. The current window remains
one second; wake and finish share `WINDOW_S` rather than independent literals.

## Regression evidence and limits

The initial interface migration missed two substituted Reporter test doubles
in connection and connected-feed tests. A metrics-focused test slice did not
include those callers. Both doubles now implement the no-pending method without
changing their assertions; canonical reproduction failed three cases before the
repair. The rollout's inventory-derived consumer regression gate includes those
callers as well as metrics and loop tests.

The new external-quality regression failed on the private-counter-only source;
its behavior asserts two cumulative log emissions and unchanged CPU-less wire
samples. Window-consistency coverage rejects a due pending snapshot left behind
by a separately hardcoded finish window. All actual live latency acceptance is
separate from these unit contracts and must follow the rollout's frozen review,
CI, installation and capture gates.
