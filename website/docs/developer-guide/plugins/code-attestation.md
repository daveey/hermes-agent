# Plugin code attestation for capsule owners

The `kanban-capsule-contract-v2` capability requires a supported boot route, an
unchanged clean core checkout and a process ledger with **no unknown code**.
Enabling or installing a plugin does not satisfy that requirement. Normal legacy
plugin loading is unchanged when attestation fails; the strict capability is
withheld.

## Reviewed directory plugins

`hermes_cli/data/plugin-review-records.json` is a versioned core review record:

```json
{
  "version": 1,
  "plugins": {
    "example-plugin": {
      "__init__.py": "<64 lowercase hexadecimal SHA256 characters>",
      "helpers.py": "<64 lowercase hexadecimal SHA256 characters>"
    }
  }
}
```

The example digest placeholders are not valid records. The registry enrolls only
explicitly reviewed byte sets; see [the run28 enrollment](run28-review-records.md)
for the first records and their independent-review provenance.
To enroll another, review the exact plugin sources, calculate their SHA256 digests,
and submit the record through the normal independent core review and CI path.
Include every executable source and any file whose integrity is part of that
review. A new plugin revision needs new review and new pinned digests. Installation
metadata, a plugin's own manifest, a Git remote and freshly hashed installed files
are never approval authorities.

Before constructing a directory plugin module, the loader reads the bounded
registry and checks its Git blob digest against the **boot commit's** tree entry.
It validates the schema, canonical relative paths and all registered file bytes,
then registers that physical directory with the executed-code ledger. Files that
escape the directory via symlinks are rejected. Repeated loads reuse only an
identical component/root/kind/digest registration; other overlap remains a sticky
conflict. Resolved directories inside core keep existing core custody regardless
of the plugin's manifest source label.

Execution still requires the existing source/code-object checks. Files missing
from the approved map cannot acquire review just by sharing the directory. Each
strict integrity observation freshly checks all registered review-map files,
including files not yet imported. Changed or deleted bytes withhold the capability;
restoring bytes cannot erase an already latched unknown. Registry reads are
bounded to 1 MiB and 4096 file records; validation reads at most 8 MiB per file and
32 MiB per plugin. Exceeding a bound fails closed.

Entry-point plugins currently have no reviewed-package registration contract.
Their loading explicitly latches an unknown before `ep.load()`, even if their
files have installer RECORD receipts. They continue to work on legacy paths but
cannot advertise the strict capsule capability.

## Dependencies and latency

Managed dependency activation retains its existing order: register the selected
site-packages environment, verify installer RECORD custody, then call
`site.addsitedir`. These dependencies are **installer-trusted, not source-audited**.
Plugin-controlled search-path additions are not automatically registered. An
unrooted dependency or unsupported generated code still withholds the capability.

The closed attrs 26.1.0 rule also requires the reviewed `attr/_make.py` digest,
observed producer/caller code identities and paired compile/exec output custody;
a producer receipt alone grants nothing. Installer metadata is keyed by both
site-packages root and distribution, so overlapping same-version generations
cannot borrow one another's RECORD. Multiple valid claims for one source path
latch ambiguity regardless of digest agreement or enumeration order. A missing
or changed original RECORD refuses generated output. Receipt reports retain the
usual distribution key for a unique generation; overlapping distributions use
root-qualified keys, preserving each used generation's independent evidence.

`dependency_receipts().unknown_producer_sources` attributes an unsupported
execution to its producer's previously observed installer-receipted source, when
that exact live producer code object is known. Each diagnostic contains the
producer filename, qualname, component and source SHA256. It does **not** attest
the generated code, add a generator to the closed producer table, or remove or
reclassify an unknown. A synthetic filename (including an attrs filename) is not
evidence of source custody. Unobserved, modified or filename-spoofed producers do
not acquire these receipts. Reports bound this attribution to 4096 reasons and
8 source receipts per reason; truncating diagnostics never grants trust.

Install attestation through the supported bootstrap before importing the plugin
loader. Constructing a diagnostic ledger after importing the loader correctly
latches `prehook-module` refusals; these cannot be repaired by late registration
or treated as equivalent to a supported-bootstrap observation.

Identify uses one in-flight verifier with no cached positive. Its 0.4-second
absolute deadline includes thread setup and leaves headroom within a 0.5-second
identify budget. Late positives are discarded. Slow storage withdraws v2 rather
than hiding the legacy answer. OS scheduling or synchronous thread-start stalls
cannot be hard-preempted by a Python deadline; this is not a hard-real-time
promise. Owner launch still performs full synchronous verification, without
filtering the ledger's unknowns.
