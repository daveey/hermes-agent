"""Startup custody and memory-only profile lookup for native annotations. [daveey]"""
from types import MappingProxyType
import threading

from hermes_cli import kanban_annotation_facts as annotations

_lock = threading.RLock()
_registry = None


class AnnotationRegistry:
    """No discovery, capture or alternate view cache: only authorized handles."""
    def __init__(self, bindings, fallback_profile):
        self.bindings = MappingProxyType(dict(bindings))
        if fallback_profile not in self.bindings or any(
                name != binding.scope.profile_id for name, binding in self.bindings.items()):
            raise ValueError('invalid annotation binding profile')
        self.fallback_profile = fallback_profile
        self._release = None

    def select(self, profile=None):
        return self.bindings.get(self.fallback_profile if profile is None else profile)

    def install(self):
        global _registry
        with _lock:
            if _registry is not None:
                raise RuntimeError('annotation registry already installed')
            self._release = annotations.install_lookup(self.select)
            _registry = self
        return self

    def close(self):
        global _registry
        with _lock:
            if _registry is self:
                self._release()
                _registry = None


def binding_for(profile=None):
    """Existing authorized v1 binding; a missing binding is never discovered."""
    with _lock:
        registry = _registry
    return registry.select(profile) if registry is not None else None
