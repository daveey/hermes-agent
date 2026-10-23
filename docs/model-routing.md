# Class routing contract (opt-in)

`class:deep`, `class:standard`, and `class:fast` require `provider: auto`.
Concrete model identifiers bypass class allocation unchanged. Do not enable class
selectors in production until every entry path in the rollout matrix is verified.

The host/profile-scoped `model_routing` section has this version-1 shape:

```yaml
model_routing:
  enabled: false
  policy_version: 1
  allow_metered: false
  routes:
    deep: []
    standard: []
    fast: []
```

Each entry is an exact mapping:

- `provider`, `model`: exact runtime identifiers, not aliases or class markers.
- `billing`: `subscription`, `credit-funded`, or `metered`.
- `weight`: integer 1–100; all models under a provider in one class use the same
  provider weight. Account count and number of models never increase provider share.
- `capabilities`: boolean capabilities (`tools`, `vision`, `streaming`, `json`, …),
  optional positive integer `context_tokens` and `max_latency_ms`.
- `eligible: true`: operator has verified entitlement and exact model availability.
- `funding_verified: true`: operator has verified this billing classification.

Missing attestations fail closed. Subscription labels are accepted only for
`anthropic`, `openai-codex`, and `xai-oauth`. Runtime selection further requires a
native OAuth credential and native endpoint/protocol. Gemini API credits are
`credit-funded`, not subscription/OAuth; newly expanded paid/credit traffic requires
explicit `allow_metered: true` as well as verified funding. No live route manifest
is shipped in core. Unknown model equivalence must not be guessed from a name.

## Shared API

`agent.model_routing.resolve_model_route(model, provider='auto', *, config=None,
session_id=None, parent_session_id=None, requirements=None, excluded=(),
state_path=None, eligible=None)` returns `{model, provider, metadata}`.
`metadata` is None for concrete pins. A class request needs an actual durable
session/job identity. `config` is the full profile config, not only the section;
if omitted the effective profile-scoped loader is used. `eligible(route)` is an
optional runtime availability probe, run after manifest/capability/billing filters
and before allocation. `requirements` uses capability booleans, `min_context_tokens`
and `max_latency_ms`. `excluded` contains failed `(provider, model)` pairs.

`hermes_cli.runtime_provider.resolve_runtime_provider` takes optional
`routing_session_id`, `routing_parent_session_id`, `routing_requirements`,
`routing_config`, `routing_excluded`. Class requests use the shared selector before
provider validation. Returns concrete `model` and `model_routing` metadata alongside
normal credential fields. Callers MUST use this concrete model before any request.
`resolve_runtime_with_fallback` accepts the identity/requirements arguments too,
and does NOT walk legacy fallback entries for class requests.

Routing raises `ModelRoutingError` (ValueError, deliberately not AuthError) for
empty pools, invalid policy, absent identity or billing violations. Callers must
not catch it and retry through a generic/paid provider. Credential pools for
subscription routes filter account selection/rotation to OAuth without deleting
paid credentials from the user's store. Native pools retain refresh authority;
router state never stores credentials or refresh tokens.

## Durable state

`<profile home>/model-routing.sqlite3` stores route metadata, alias roots and
allocation counters under `BEGIN IMMEDIATE` transactions. A resume or compression
child references its actual parent session. Aliases share a failover decision.
Allocation does not occur per tool call. Sticky routes are reused while permitted;
a revoked route fails rather than silently changing an active session. A failed
attempt may explicitly exclude its concrete route and select only within its
original class and authorized billing policy. New sessions prefer subscriptions;
a healthy existing credit-funded session is not silently migrated mid-turn.

Metadata: `requested_class`, `policy_version`, `selected_provider`, `selected_model`,
`billing_group`, `reason` (`allocation` or `same-class-failover`). No prompts or
credentials are stored. Per-provider account selection remains inside native pools.

## Implementation status

Core allocator, config validation, runtime credential boundary, and common agent
construction/recovery are implemented. CLI startup, readiness/lazy credentials,
agent creation and session restore now preserve selectors and subscription fences.
The CLI shell retains the selector while the agent sends only the concrete model;
session `model_config.model_routing` records the selection for restoration. Explicit
switches clear that metadata, including when selecting the same concrete model.
CLI integration tests exercise the native Anthropic SDK streaming request through
an isolated mock HTTP transport, inspect endpoint/model/OAuth headers, and restore
from a real temporary session DB. This is not a live entitlement probe.

Desktop/TUI construction defers class credential resolution until AIAgent has its
durable session key. Runtime persistence/restoration retains the class while showing
the concrete model; explicit switches clear the metadata. Config sync does not
rebalance existing conversations when a class default changes. Desktop tests cover
construction, restoration, explicit pinning, empty-pool zero sends, and the native
Anthropic streaming SDK request via mock HTTP transport (not live entitlement).

Messaging, API, delegation, cron, Kanban and auxiliary adapters and their per-entry
request/recovery tests remain required before activation. Desktop background/preview
agents and startup readiness also require audit. This document is not rollout proof.
