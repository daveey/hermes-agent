# Kanban capsule contract v2

`kanban-capsule-contract-v2` is an additive behavioral compatibility contract for
capsule-required Kanban submission across core revisions. It is not a general
permission grant, a full loaded-code digest or a sandbox against same-uid attackers.

## Admission and launch

The submitter still requires a known full commit identity and transport-v1. A
same-revision owner retains the legacy **submitter admission** path. Different
revisions additionally require v2. Every capsule-required launch, same-revision
included, must pass strict owner/child identity checks; dirty/untracked core or
any unfiltered ledger unknown (including external plugins) refuses that launch.
No caller chooses a child core or interpreter.

The ready descriptor names the implemented ABI. Every control-socket identify
filters v2 against the bootstrap-installed integrity ledger and strict disk
identity: matching resolved root, matching boot commit, supported owner launch
route, no unfiltered pending/unknown provenance, and clean unchanged Git state
before and after ledger inventory. No ledger is installed late. Dirty or unknown
boot identity is sticky; an update withdraws compatibility until a clean restart.
Ordinary gateway operations and transport-v1 are not withdrawn by this check.

Before scope resolution or single-use rendezvous consumption, placement repeats
strict identity checks. Capsule workers launch through the fixed bootstrap-first
managed-worker template with the owner's executable and core. Ordinary workers
retain the previous bare module command. The owner appends a private v2 SHA argv
expectation alongside the observation FD. Old children reject the extra argv.
The new child requires its own installed ledger and the exact managed-worker
route before hello, then repeats checks before execution, observation, after
checkpoint recovery, before construction, before ready and before turns. An
installed FD with lost expectation fails closed. Startup failures revoke binding
and close session/RPC resources; consumed rendezvous cannot be reused.

These checks are uncached. Advisory identify waits at most 0.5 seconds for a
single-flight verifier; overrun or an already-running check withdraws v2 only,
with no reuse of late positive results. Launch and child verification remain
strict, synchronous and uncached. A measured clean disk scan alone is not a
bound on ledger inventory. Unsupported bootstrap routes do not perform the
strict capsule boot scan.

## Frozen ABI and evidence limits

Golden vectors cover observer digest bytes/order, module/sentinel interfaces,
claim and registration/reply fields, observer line, capsule requirement derivation,
start identity/type/workspace validation and private consumed-marker mode. The
existing claim/run, peer credentials, observer verification and scope placement
contracts remain authoritative. Changing their meaning requires deliberate ABI
migration, not silently reusing v2.

The composed no-model fixture drives a real cross-SHA submitter WebSocket RPC
through owner placement and authenticated rendezvous into an unmodified native
managed main/execute, real claim binding and observation. Its discovery/ticket,
owner handler/adoption reply, owner installed-ledger observation and post-bind
checkpoint effect are explicitly scoped fixture seams. The verified observer is
a stand-in, not an application's private observer. Separate tests freeze a real
pre-v2 worker entrypoint, test native startup update refusals, and drive prehello
loss through dispatcher crash disposition. These do not establish a historical
full-core matrix, private app lifecycle, successful model completion, systemd
confinement or app deployment readiness. A pinned application must separately
review its ownership port, observer/scope argv, dependency environment, historical
workflow preservation and owner-controlled activation against its deployed base.
