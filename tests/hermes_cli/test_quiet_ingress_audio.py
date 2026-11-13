"""Quiet-v3 R4-F F1: Desktop voice STT/TTS routes reserve local work BEFORE the provider call and
release only when the worker thread physically exits.

The REAL route functions run (HTTP handlers called directly, ws handlers through TestClient) against a
real ``local_work.Registry``; only the provider SDK entry points are faked with event-gated fakes.
"""
import asyncio
import base64
import json
import threading
from urllib.parse import urlencode

import pytest

from hermes_cli import local_work
from hermes_constants import hermes_home_key
from tests.waiting import async_wait_until, wait_until

GATE_TIMEOUT = 10.0


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reg = local_work.Registry(homes=[hermes_home_key(tmp_path)])
    monkeypatch.setattr(local_work, "_registry", reg)
    return reg


def _clean(reg):
    snap = reg.snapshot()
    assert snap.unknowns == (), snap.unknowns
    assert snap.active == 0, dict(snap.per_kind)


def _wav_payload():
    from hermes_cli.web_models import AudioTranscriptionRequest
    raw = base64.b64encode(b"RIFFfake").decode()
    return AudioTranscriptionRequest(data_url=f"data:audio/wav;base64,{raw}", mime_type="audio/wav")


@pytest.mark.asyncio
async def test_transcribe_reserves_before_provider_and_survives_cancelled_waiter(registry, monkeypatch):
    from hermes_cli.web_routers import audio

    entered, release = threading.Event(), threading.Event()
    seen = {}

    def transcribe_recording(path):
        seen["active_in_provider"] = registry.snapshot().active
        entered.set()
        assert release.wait(GATE_TIMEOUT)
        return {"success": True, "transcript": "hi", "provider": "fake"}

    monkeypatch.setattr("tools.voice_mode.transcribe_recording", transcribe_recording)
    epoch0 = registry.snapshot().epoch

    task = asyncio.create_task(audio.transcribe_audio_upload(_wav_payload()))
    await async_wait_until(entered.is_set, what="provider entered")
    assert seen["active_in_provider"] == 1, "reserved before/while the provider call runs"
    assert registry.snapshot().epoch > epoch0

    task.cancel()  # the waiter goes away; the provider thread is still running
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry.snapshot().active == 1, "an abandoned waiter must not release a live provider thread"

    release.set()
    await async_wait_until(lambda: registry.snapshot().active == 0, what="thread exit releases")
    _clean(registry)


@pytest.mark.asyncio
async def test_transcribe_provider_error_releases_exactly_once(registry, monkeypatch):
    from fastapi import HTTPException
    from hermes_cli.web_routers import audio

    def boom(path):
        raise RuntimeError("provider down")

    monkeypatch.setattr("tools.voice_mode.transcribe_recording", boom)
    with pytest.raises(HTTPException):
        await audio.transcribe_audio_upload(_wav_payload())
    _clean(registry)


@pytest.mark.asyncio
async def test_tts_speak_reserves_for_provider_call(registry, monkeypatch):
    from fastapi import HTTPException
    from hermes_cli.web_models import TTSSpeakRequest
    from hermes_cli.web_routers import audio

    entered, release = threading.Event(), threading.Event()
    seen = {}

    def text_to_speech_tool(text):
        seen["active"] = registry.snapshot().active
        entered.set()
        assert release.wait(GATE_TIMEOUT)
        return json.dumps({"success": False, "error": "no provider"})

    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", text_to_speech_tool)
    task = asyncio.create_task(audio.speak_text(TTSSpeakRequest(text="hello")))
    await async_wait_until(entered.is_set, what="tts provider entered")
    assert seen["active"] == 1
    release.set()
    with pytest.raises(HTTPException):
        await task
    await async_wait_until(lambda: registry.snapshot().active == 0, what="released")
    _clean(registry)


@pytest.mark.asyncio
async def test_no_registry_is_inert(monkeypatch):
    from hermes_cli.web_routers import audio
    monkeypatch.setattr(local_work, "_registry", None)
    monkeypatch.setattr("tools.voice_mode.transcribe_recording",
                        lambda path: {"success": True, "transcript": "ok", "provider": "fake"})
    assert (await audio.transcribe_audio_upload(_wav_payload()))["transcript"] == "ok"


# ------------------------------------------------------------------ websocket routes

@pytest.fixture
def stream_client(monkeypatch, _isolate_hermes_home):
    from starlette.testclient import TestClient
    from hermes_cli import web_server

    previous = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app)
    try:
        yield client, web_server._SESSION_TOKEN
    finally:
        client.close()
        if previous is None:
            if hasattr(web_server.app.state, "auth_required"):
                delattr(web_server.app.state, "auth_required")
        else:
            web_server.app.state.auth_required = previous


@pytest.fixture
def ws_registry(monkeypatch, _isolate_hermes_home):
    from hermes_constants import get_hermes_home
    reg = local_work.Registry(homes=[hermes_home_key(get_hermes_home())])
    monkeypatch.setattr(local_work, "_registry", reg)
    return reg


def test_speak_stream_producer_thread_holds_reservation_until_it_exits(stream_client, ws_registry, monkeypatch):
    client, token = stream_client
    entered, release = threading.Event(), threading.Event()

    class Streamer:
        sample_rate, channels = 24000, 1

        def stream(self, text):
            entered.set()
            assert release.wait(GATE_TIMEOUT)
            return iter(())

    monkeypatch.setattr("tools.tts_streaming.resolve_streaming_provider", lambda cfg: Streamer())
    monkeypatch.setattr("tools.tts_tool._load_tts_config", dict)
    monkeypatch.setattr("tools.tts_tool._get_provider", lambda cfg: "fake")
    monkeypatch.setattr("tools.tts_tool._resolve_max_text_length", lambda provider, cfg: 4000)
    url = f"/api/audio/speak-stream?{urlencode({'token': token})}"
    epoch0 = ws_registry.snapshot().epoch

    with client.websocket_connect(url) as conn:
        conn.send_text(json.dumps({"text": "Hello there."}))
        conn.send_text(json.dumps({"done": True}))
        assert entered.wait(GATE_TIMEOUT)
        assert ws_registry.snapshot().active >= 1
        conn.send_text(json.dumps({"stop": True}))  # barge-in: the provider call is still blocked
    # socket gone, provider thread still inside stream(): reservation must persist
    assert ws_registry.snapshot().active >= 1, "producer thread alive => still reserved"
    assert ws_registry.snapshot().epoch > epoch0

    release.set()
    wait_until(lambda: ws_registry.snapshot().active == 0, what="producer exit releases")
    _clean(ws_registry)


def test_transcribe_stream_session_reserved_across_open_and_finalize(stream_client, ws_registry, monkeypatch):
    client, token = stream_client
    seen = {}

    class Session:
        def set_input_rate(self, rate):
            pass

        def push_audio(self, data):
            pass

        def cancel(self):
            pass

        def finalize(self):
            seen["active_finalize"] = ws_registry.snapshot().active
            return {"success": True, "transcript": "hi", "provider": "fake"}

    def open_streaming_session(**_):
        seen["active_open"] = ws_registry.snapshot().active
        return Session()

    monkeypatch.setattr("tools.transcription_streaming.open_streaming_session", open_streaming_session)
    url = f"/api/audio/transcribe-stream?{urlencode({'token': token})}"
    with client.websocket_connect(url) as conn:
        conn.send_text(json.dumps({"sample_rate": 16000}))
        conn.send_bytes(b"\0\0")
        conn.send_text(json.dumps({"eos": True}))
        assert conn.receive_json()["type"] == "final"
    assert seen["active_open"] >= 1 and seen["active_finalize"] >= 1
    wait_until(lambda: ws_registry.snapshot().active == 0, what="session released")
    _clean(ws_registry)
