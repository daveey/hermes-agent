"""Placeholder client returned while availability-probe mode is active."""
from typing import Any


class _AuxProbeClientStub:
    """Non-functional placeholder returned while `aux_probe_mode` is active."""
    __slots__ = ("_hermes_aux_backend_provider", "_hermes_aux_effective_provider", "api_key", "base_url")

    def __init__(self, api_key: str = "", base_url: str = "") -> None:
        self.api_key = api_key
        self.base_url = base_url
        self._hermes_aux_backend_provider = self._hermes_aux_effective_provider = ""

    def __getattr__(self, name: str) -> Any:
        # Loud failure if a probe stub ever leaks into a runtime call path.
        raise RuntimeError(
            f"_AuxProbeClientStub used as a real client (attribute {name!r}); "
            "aux_probe_mode is for availability checks only")

    def __repr__(self) -> str:
        return "<aux availability-probe client stub>"
