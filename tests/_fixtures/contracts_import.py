"""Cold contract import interleaving for the browser attachment regression.

Run in a fresh interpreter: another thread has created registry.py, but has not
registered sessions yet. Observe actual imports and lookups to release that
thread, never manufacture a missing contract or replace wire validation.
"""
import asyncio
import builtins
import importlib
import importlib.abc
import importlib.machinery
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


def main():
    assert "tui_gateway.contracts" not in sys.modules, "requires a cold catalog"
    blocked, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []
    real_import = builtins.__import__

    class ObservedMethods(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            if key == "session.resume":
                # Old dispatch snapshots the missing entry before package completion.
                release.set()
                assert finished.wait(15), "catalog import did not finish"
            return value

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "tui_gateway.contracts.sessions":
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            assert spec is not None and spec.loader is not None
            original = spec.loader

            class Loader(importlib.abc.Loader):
                def create_module(self, spec):
                    return original.create_module(spec)

                def exec_module(self, module):
                    registry = sys.modules["tui_gateway.contracts.registry"]
                    registry.METHODS = ObservedMethods(registry.METHODS)
                    assert "session.resume" not in registry.METHODS
                    assert "session.resume" not in registry.CANONICAL_METHODS
                    blocked.set()
                    assert release.wait(15), "dispatch never reached the import boundary"
                    original.exec_module(module)

            spec.loader = Loader()
            return spec

    def import_catalog():
        try:
            importlib.import_module("tui_gateway.contracts")
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    def observed_import(name, globals=None, locals=None, fromlist=(), level=0):
        if (name == "tui_gateway.contracts" and globals
                and globals.get("__name__") == "gateway.session_controls"):
            # The fixed caller enters the real package import lock before lookup.
            release.set()
        return real_import(name, globals, locals, fromlist, level)

    finder = Finder()
    sys.meta_path.insert(0, finder)
    builtins.__import__ = observed_import
    thread = threading.Thread(target=import_catalog)
    thread.start()
    try:
        assert blocked.wait(15), "import never reached the real sessions loader"
        with tempfile.TemporaryDirectory() as temp, SessionDB(db_path=Path(temp) / "state.db") as db:
            async def scenario():
                db.create_session("s", source="test")
                authority = SessionAuthority(SimpleNamespace(), profile_id=temp, instance_id="test", db=db,
                                             epoch=begin_runtime_epoch(db, instance_id="test"))
                authority.sessions["s"] = LiveSession(None, "route")
                viewer = AuthorityConnection(authority, object(), {"user_id": "human"})
                try:
                    result = await viewer.dispatch({"id": 1, "method": "session.resume",
                                                    "params": {"session_id": "s"}})
                    assert "result" in result, result
                    from tui_gateway.contracts import CANONICAL_METHODS
                    snapshot = CANONICAL_METHODS["session.resume"].result.model_validate(result["result"])
                    assert snapshot.info.desktop_protocol == "hermes-gateway-v1"
                    refused = await viewer.dispatch({"id": 2, "method": "session.resume",
                                                     "params": {"session_id": "s", "bogus": True}})
                    assert refused["error"]["data"]["fields"] == ["bogus"], refused
                finally:
                    await viewer.close()
            asyncio.run(scenario())
    finally:
        release.set()
        thread.join(15)
        builtins.__import__ = real_import
        sys.meta_path.remove(finder)
        assert not thread.is_alive()
        assert not errors, errors
    print("COLD_CATALOG_REAL_RESUME_AND_VALIDATION_OK")


if __name__ == "__main__":
    main()
