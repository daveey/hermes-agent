# Assistance cards are not prerequisites

A helper that only supplies attachments, diagnostics or administrative assistance
must not become a prerequisite of its own source. Declare the relationship in
the helper's body as a standalone line:

```text
<!-- kanban-helper-for: t_0123456789abcdef -->
```

Use the actual source ID verbatim (classic eight-hex and synced sixteen-hex IDs
are supported). This is an explicit, refusal-only declaration, not a permission,
completion receipt or inference from a title. Multiple lines can declare multiple
sources. Worker creates already inherit creator ownership; do not add a
helper → source dependency merely to associate the cards. The source may create
a helper without putting either card in the other's `parents` list.

`kanban_link` returns `helper_parent_forbidden` when that helper is proposed as a
parent of the named source. Internal graph insertion and source creation with
that parent also refuse; the creation transaction rolls back. The error explains
how to preserve ownership without adding the edge. Other source IDs are not
implicitly classified, and untyped creator children remain ordinary prerequisites.
A review, required landing, safety decision or genuinely blocking producer must
remain an ordinary dependency, not be declared an assistance-only helper.

This declaration does not make any parent non-gating. Existing edges, archived
restrictions, spend stops and containment remain unchanged. Adding the declaration
after an edge exists does not remove or waive it. Recovery of historical edges
requires the existing authorized dependency/scope workflow; neither this marker
nor archiving a card is a recovery grant.
