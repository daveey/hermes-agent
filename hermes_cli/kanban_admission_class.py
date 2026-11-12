"""Admission-class metadata shared by task producers and editors."""
import re


class AdmissionMetadataError(ValueError):
    """Invalid class/frees pair, distinct from storage failures."""


def edit_fields(conn, task_id: str, admission_class, admission_frees) -> dict:
    """Validate the postimage from the caller's writer transaction."""
    if admission_class is None and admission_frees is None:
        return {}
    current_class, current_frees = conn.execute(
        "SELECT admission_class, admission_frees FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    validate(admission_class if admission_class is not None else current_class,
             admission_frees if admission_frees is not None else current_frees)
    return {field: value for field, value in (
        ("admission_class", admission_class), ("admission_frees", admission_frees),
    ) if value is not None}


def validate(admission_class: str, admission_frees: str | None) -> None:
    """Reject unrecognized classes and capacity labels without a concrete claim."""
    if admission_class not in ("normal", "capacity", "urgent"):
        raise AdmissionMetadataError("admission_class must be normal, capacity or urgent")
    if admission_class == "capacity" and (
        not isinstance(admission_frees, str)
        or re.fullmatch(r"frees: [^\r\n]*\S[^\r\n]*", admission_frees) is None
    ):
        raise AdmissionMetadataError("capacity requires a one-line frees: claim")
