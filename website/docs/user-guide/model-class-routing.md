---
title: Model class routing
---

# Model class routing

Class routing is an opt-in, profile-local allocator. Set a workload's model to
`class:deep`, `class:standard`, `class:fast`, or explicitly `class:carbon`, with provider `auto`. Hermes
resolves the marker before constructing a provider request. Classes are operator
assignments, not model-name guesses; failure never downgrades to another class.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `model_routing.enabled` | `false` | Enable class selectors. Disabled routing preserves legacy concrete-model behavior; class requests fail clearly. |
| `model_routing.policy_version` | `1` | Registry/state policy schema version. Only version 1 is supported. |
| `model_routing.allow_metered` | `false` | Permit verified credit-funded/metered registry routes when subscriptions are unavailable. This is spending authorization, not a budget or credit-exhaustion monitor. |
| `model_routing.enforce_subscription_only` | `false` | When enabled with routing, also fence concrete pins, saved overrides, auxiliary calls and fallback paths to registered eligible native subscription routes. Overrides `allow_metered`. |
| `model_routing.routes.deep` | `[]` | Exact routes eligible for deep reasoning workloads. |
| `model_routing.routes.standard` | `[]` | Exact routes eligible for standard workloads. |
| `model_routing.routes.fast` | `[]` | Exact routes eligible for fast workloads. |
| `model_routing.routes.carbon` | `[]` | Explicit-only routes, including Gemini `gemini-4-carbon`; never automatic capacity. |

Each route requires `provider`, `model`, `billing`, `weight`, and `capabilities`.
Identifiers are exact. `weight` is an integer from 1 to 100 and must be identical
for all routes belonging to one provider within a class. Equal weights give
providers equal allocation shares, regardless of account count.

`billing` is `subscription`, `credit-funded`, or `metered`. Supported native
subscription providers are `anthropic`, `openai-codex`, and `xai-oauth`.
An API key or a consumer subscription alone does not prove subscription funding.
In particular, Gemini API credits must be classified as credit-funded, not OAuth
or unlimited. Both `eligible: true` and `funding_verified: true` are required
before a route can receive work. These are host-specific operator attestations;
the runtime separately verifies usable credentials before sending requests.

Capabilities include boolean `tools`, `vision`, `json`, `streaming`, positive
integer `context_tokens`, and optionally positive integer `max_latency_ms`.
Use capabilities verified for the exact native transport, not a different API's
advertised limits. Missing required capabilities exclude the route.

A safe inactive starting configuration is:

```yaml
model_routing:
  enabled: false
  policy_version: 1
  allow_metered: false
  enforce_subscription_only: false
  routes:
    deep: []
    standard: []
    fast: []
```

Populate and verify host-specific routes before enabling selectors. Use the
supported `hermes config set` interface for live configuration; never store
credentials in this registry or copy rotating refresh tokens between hosts.
An empty eligible class fails closed rather than using a paid fallback.

## Sessions, pins, and recovery

`class:carbon` is explicit spending authorization for its verified metered or
credit-funded routes, even with `allow_metered: false` and
`enforce_subscription_only: true`. It still requires funding, eligibility,
capabilities and a usable credential. Carbon tuples (and `gemini-4-carbon`)
are excluded from every other class, even if duplicated in its registry.
Kanban never uses carbon as a default, strong/retry/review tier, ratio allocation,
or reroute target. An explicit card or review request may name `class:carbon`;
delegation inherits it only from a parent that requested carbon. No automatic
fallback crosses into carbon. Concrete paid pins remain rejected under managed
subscription enforcement; request the class instead.

Allocation is transactionally stored in `model-routing.sqlite3` under the active
profile home. A healthy session/job remains sticky across turns, tools, restarts,
resume and compression lineage. Auth, quota or transport recovery may select an
eligible route in the same class; it cannot bypass billing or capability checks.
Subscription capacity precedes metered capacity for new allocations.

Without managed subscription enforcement, explicit concrete pins retain legacy
behavior. With `enforce_subscription_only`, a concrete pin must match one
unambiguous eligible native subscription tuple in the registry. A stale paid
model in a saved session is not authorization to spend. Unknown or ambiguous pins
are rejected, not silently assigned a class. Temporary `/model --once` switches
do not replace the standing session allocation.

Routing covers CLI, Desktop/TUI, messaging, API completions and runs, delegation,
cron, Kanban and auxiliary streaming/nonstreaming calls. Deployment and live
provider eligibility are separate gates: a valid registry does not prove that
all providers are entitled, funded, or available on every host.
