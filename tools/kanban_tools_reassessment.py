"""Owner reassessment tool input schema and privately captured lifecycle kwargs."""


def schema():
    """Typed opt-in only; no issuer coordinates or scanner/capability promise."""
    def text(maximum):
        return {"type": "string", "minLength": 1, "maxLength": maximum, "pattern": r"\S"}

    def obj(properties):
        return {"type": "object", "additionalProperties": False,
                "required": list(properties), "properties": properties}

    return {**obj({
        "version": {"type": "integer", "enum": [1]},
        "reassess_at": {"type": "integer", "minimum": 1},
        "target": obj({"kind": {"type": "string", "enum": ["queue", "update", "external"]},
                       "identity": text(512), "revision": text(256)}),
        "evidence_refs": {"type": "array", "minItems": 1, "maxItems": 16, "uniqueItems": True,
                          "items": obj({"table": {"type": "string", "enum": ["task_events"]},
                                        "uid": {"type": "string", "minLength": 36, "maxLength": 36, "pattern":
                                                "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}})},
    }), "description": "Opt-in bounded reassessment by this admitted owner run. Requires explicit producer awaiting "
        "and existing same-run portable event evidence; reassess_at is Unix seconds, after now and no later than "
        "the admitted backstop. Delayed parks only. Records pending evidence, not producer health, scanner "
        "suppression, release or execution authority. Issuer custody is captured privately, never supplied here."}


def install(*schemas):
    """Add only the typed field; retain every existing park policy and bound."""
    for park_schema in schemas:
        park_schema["parameters"]["properties"]["owner_reassessment"] = schema()


def lifecycle_kwargs(args: dict, tid: str) -> dict:
    """Lifecycle kwargs for an opt-in: the privately captured live owner origin (None when absent, which
    the lifecycle refuses). A caller cannot name issuer coordinates; nothing here is scanner authority."""
    value = args.get("owner_reassessment")
    if value is None:
        return {}
    from agent.origin_worker_custody import capture
    return {"owner_reassessment": value, "owner_origin": capture(tid)}
