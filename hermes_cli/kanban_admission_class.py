"""Admission-class metadata shared by task producers and editors."""
import re


def validate(admission_class: str, admission_frees: str | None) -> None:
    """Reject unrecognized classes and capacity labels without a concrete claim."""
    if admission_class not in ("normal", "capacity", "urgent"):
        raise ValueError("admission_class must be normal, capacity or urgent")
    if admission_class == "capacity" and (
        not isinstance(admission_frees, str)
        or re.fullmatch(r"frees: [^\r\n]*\S[^\r\n]*", admission_frees) is None
    ):
        raise ValueError("capacity requires a one-line frees: claim")
