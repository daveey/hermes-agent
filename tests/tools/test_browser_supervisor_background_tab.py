"""The CDP supervisor must open its fallback page in the background.

A foreground Target.createTarget makes macOS hand Chrome keyboard focus, stealing
it from the user's app whenever a supervisor attached to a browser with no page.
"""
import asyncio

import tools.browser_supervisor as bs


def test_initial_page_created_in_background(monkeypatch):
    supervisor = bs.CDPSupervisor(task_id="bg-tab", cdp_url="ws://127.0.0.1:9222")
    calls = []

    async def fake_cdp(method, params=None, **_kw):
        calls.append((method, params or {}))
        if method == "Target.getTargets":
            return {"result": {"targetInfos": []}}
        if method == "Target.createTarget":
            return {"result": {"targetId": "T1"}}
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "S1"}}
        return {"result": {}}

    async def _noop(*_a, **_k):
        pass

    monkeypatch.setattr(supervisor, "_cdp", fake_cdp)
    monkeypatch.setattr(supervisor, "_enable_page_domains", _noop)
    monkeypatch.setattr(supervisor, "_install_dialog_bridge", _noop)
    asyncio.run(supervisor._attach_initial_page())

    creates = [p for m, p in calls if m == "Target.createTarget"]
    assert creates == [{"url": "about:blank", "background": True}]
