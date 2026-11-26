"""Real client/parser boundaries with transport waits held before later phases."""
import importlib
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
import providers


def _event(payload):
    return ('data: ' + json.dumps(payload) + '\n\n').encode()


def _setup(monkeypatch):
    import hermes_cli.config
    monkeypatch.setattr(hermes_cli.config, 'load_config_readonly', dict)
    providers.get_provider_profile('gemini')
    return importlib.import_module('plugins.model_providers.gemini.client')


def _phases(caplog):
    return [r.getMessage() for r in caplog.records if r.name.endswith('gemini.stream_phases')]


def test_keepalive_is_raw_not_semantic_or_model_output(monkeypatch, caplog):
    mod = _setup(monkeypatch)
    caplog.set_level(logging.INFO)

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            rows = _phases(caplog)
            assert any('phase=response_headers ' in s for s in rows)
            assert not any('phase=first_raw_text ' in s for s in rows)
            yield b': private-keepalive-must-not-log\n\n'
            rows = _phases(caplog)
            assert any('phase=first_raw_text ' in s for s in rows)
            assert not any('phase=first_sse_event ' in s for s in rows)
            assert not any('phase=first_translated_chunk ' in s for s in rows)
            yield _event({'event_type': 'interaction.created', 'interaction': {'id': 'private-interaction-id'}})
            rows = _phases(caplog)
            assert any('phase=first_sse_event ' in s for s in rows)
            assert not any('phase=first_translated_chunk ' in s for s in rows)
            yield _event({'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output', 'content': [{'type': 'text', 'text': 'private-model-text'}]}})
            rows = _phases(caplog)
            assert any('phase=first_translated_chunk ' in s for s in rows)
            yield _event({'event_type': 'interaction.completed', 'interaction': {'status': 'completed', 'usage': {}}})

    with mod.GeminiClient(api_key='private-api-key', http_client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, stream=Stream())))) as client:
        chunks = list(client.chat.completions.create(model='gemini-4-carbon', messages=[{'role': 'user', 'content': 'private-prompt'}], stream=True))
    assert ''.join(c.choices[0].delta.content or '' for c in chunks) == 'private-model-text'
    rows = _phases(caplog)
    assert len(rows) == 6
    assert 'outcome=normal' in rows[-1]
    assert 'events=3 outputs=2' in rows[-1]
    assert 'private-' not in '\n'.join(rows)


def test_first_raw_before_read_failure_without_fabricating_semantics(monkeypatch, caplog):
    mod = _setup(monkeypatch)
    caplog.set_level(logging.INFO)

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b': keepalive\n\n'
            raise httpx.ReadTimeout('private-upstream-error')

    with mod.GeminiClient(api_key='private-api-key', http_client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, stream=Stream())))) as client:
        with pytest.raises(mod.GeminiAPIError):
            list(client.chat.completions.create(model='gemini-4-carbon', messages=[], stream=True))
    rows = _phases(caplog)
    assert len(rows) == 4
    assert 'events=0 outputs=0 outcome=exception' in rows[-1]
    assert not any('phase=first_sse_event ' in s or 'phase=first_translated_chunk ' in s for s in rows)
    assert 'private-' not in '\n'.join(rows)


@pytest.mark.parametrize('failure', ['connect', 'headers', 'eof'])
def test_early_end_has_only_observed_phases(monkeypatch, caplog, failure):
    mod = _setup(monkeypatch)
    caplog.set_level(logging.INFO)

    def handle(req):
        if failure == 'connect':
            raise httpx.ConnectTimeout('private-error')
        return httpx.Response(403 if failure == 'headers' else 200, content=b'')

    with mod.GeminiClient(api_key='private-key', http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
        if failure == 'eof':
            assert list(client.chat.completions.create(model='gemini-4-carbon', messages=[], stream=True)) == []
        else:
            with pytest.raises(mod.GeminiAPIError):
                list(client.chat.completions.create(model='gemini-4-carbon', messages=[], stream=True))
    rows = _phases(caplog)
    assert len(rows) == (2 if failure == 'connect' else 3)
    assert 'raw_chunks=0 raw_chars=0 events=0 outputs=0' in rows[-1]
    assert ('outcome=normal' if failure == 'eof' else 'outcome=exception') in rows[-1]
    assert 'private-' not in '\n'.join(rows)


def test_phase_clocks_counts_and_trace_are_per_hop_and_bounded(monkeypatch, caplog):
    _setup(monkeypatch)
    phases_mod = importlib.import_module('plugins.model_providers.gemini.stream_phases')
    clock = [50.0]
    monkeypatch.setattr(phases_mod, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    caplog.set_level(logging.INFO)
    for _ in range(2):
        with phases_mod.StreamPhases() as phase:
            clock[0] += 1
            observed = phase.response(SimpleNamespace(iter_text=lambda: iter(['', ': ping\n'] * 1000)))
            clock[0] += 2
            assert len(list(observed.iter_text())) == 2000
            clock[0] += 3
            for _ in range(1000):
                phase.event()
                phase.output([])
            clock[0] += 4
            phase.output([object()])
    rows = _phases(caplog)
    assert len(rows) == 12
    traces = [s.split('trace=')[1].split()[0] for s in rows]
    assert len(set(traces[:6])) == len(set(traces[6:])) == 1
    assert traces[0] != traces[6]
    for start in (0, 6):
        assert 'phase=request_start elapsed_ms=0' in rows[start]
        assert 'phase=response_headers elapsed_ms=1000' in rows[start + 1]
        assert 'phase=first_raw_text elapsed_ms=3000' in rows[start + 2]
        assert 'phase=first_sse_event elapsed_ms=6000' in rows[start + 3]
        assert 'phase=first_translated_chunk elapsed_ms=10000' in rows[start + 4]
        assert 'raw_chunks=1000 raw_chars=7000 events=1000 outputs=1 outcome=normal' in rows[start + 5]


def test_consumer_close_is_not_provider_failure_and_closes_response(monkeypatch, caplog):
    mod = _setup(monkeypatch)
    caplog.set_level(logging.INFO)
    closed = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield _event({'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output', 'content': [{'type': 'text', 'text': 'private-output'}]}})
            pytest.fail('consumer must not request more transport data')

        def close(self):
            closed.append(True)

    with mod.GeminiClient(api_key='private-key', http_client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, stream=Stream())))) as client:
        iterator = client.chat.completions.create(model='gemini-4-carbon', messages=[], stream=True)
        assert next(iterator).choices[0].delta.content == 'private-output'
        iterator.close()
        iterator.close()
    rows = _phases(caplog)
    assert len(rows) == 6
    assert 'outcome=consumer_closed' in rows[-1]
    assert closed == [True]
    assert 'private-' not in '\n'.join(rows)


def test_real_continuation_links_hops_without_reusing_phase_state(monkeypatch, caplog):
    mod = _setup(monkeypatch)
    caplog.set_level(logging.INFO)
    hop = [0]

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            current = hop[0]
            yield b': keepalive\n\n'
            rows = _phases(caplog)
            current_rows = [s for s in rows if f'hop={current} ' in s]
            assert len(current_rows) == 3
            yield _event({'event_type': 'interaction.created', 'interaction': {'id': f'private-{current}'}})
            yield _event({'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output', 'content': [{'type': 'text', 'text': 'x'}]}})
            if current == 0:
                yield _event({'event_type': 'interaction.status_update', 'status': 'requires_continuation', 'continuation_token': 'private-token'})
            yield _event({'event_type': 'interaction.completed', 'interaction': {'id': f'private-{current}', 'status': 'requires_continuation' if current == 0 else 'completed', 'usage': {}}})
            hop[0] += 1

    with mod.GeminiClient(api_key='private-key', http_client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, stream=Stream())))) as client:
        chunks = list(client.chat.completions.create(model='gemini-4-carbon', messages=[], stream=True,
                      extra_body={'max_output_tokens': 262_144}))
    assert ''.join(c.choices[0].delta.content or '' for c in chunks) == 'xx'
    rows = _phases(caplog)
    assert len(rows) == 12 and hop == [2]
    assert len({s.split('trace=')[1].split()[0] for s in rows}) == 1
    assert 'private-' not in '\n'.join(rows)
