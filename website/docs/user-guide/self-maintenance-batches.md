---
sidebar_position: 41
---

# Daily self-maintenance batches

Hermes maintenance producers can declare `self_work` when creating a Kanban card.
Product cards must omit it. Titles and body text are never used to classify work.

```json
{"version": 1, "repository": "hermes-core", "area": "kanban/intake", "kind": "sweep"}
```

The declaration has exactly four fields:

- `version`: `1`.
- `repository`: `hermes-core`, `hermes-state`, or `hermes-mods`.
- `area`: one canonical subsystem key, source-path prefix, or plugin name. Each
  producer configures this value explicitly; there is no inferred list of areas.
- `kind`: `sweep`, `test-gap`, `ci-flake`, `guard`, or `post-merge`.

Use the same declaration with the `kanban_create` tool's `self_work` argument or
`hermes kanban create ... --self-work '<JSON>'`. The feature does not classify or
batch ordinary Baby Bear, Ask Eve, fabric, or other product work.

## Admission home

Configure `kanban.self_work.home` consistently on the sending gateways and the
receiving gateway, with `gateway`, `profile`, and `board` fields. For example:

```yaml
kanban:
  self_work:
    home:
      gateway: hub
      profile: default
      board: default
```

Set settings through `hermes config set`, not by editing a running config file.
An absent or inconsistent home refuses declared self-work; it never falls back to
creating a local card. The courier must support the `selfwork-create` and
`selfwork-forward` routes before enabling producers. Execution placement
(`runs_on`) remains independent of this admission home.

At the home, one transaction either creates a card or appends the new title and
body as a comment to an open card with the same repository, tenant, area and UTC
day. A different day or area creates a new owner card. The requester needs no
chat of its own: each card the intake creates is owned by its repository's
standing owner chat, `Kanban owner: selfwork:<repository>` (at most one per
repository, shared by every area and day), so a stop on it routes to that chat
and its worker can file child cards such as a review. The owner-chat backfill
links older ownerless intake cards to the same chat. Cards it had already linked
to a `gw:<sender>` owner chat before this keyed by repository stay with that
chat until they close. Done and archived cards are not
reused. Batching does not modify existing dependencies, placement, or acceptance
text, and does not bypass review, required CI, or landing checks.

The intake journal saves the result before transport acknowledgement. Retrying
the same request does not append twice. Without an explicit `idempotency_key`,
identical submissions share an identity within their submission UTC day; the
next day's submission gets a new identity. Producers retrying an occurrence
across midnight should supply a stable occurrence-specific `idempotency_key`.
Do not reuse that key for the next daily occurrence. Queued transport is not a
successful task handoff: wait for the authoritative receipt and origin binding.
