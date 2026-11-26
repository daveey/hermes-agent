"""Bounded, content-free localization of one Interactions HTTP stream.

Raw transport activity is not model output. Keep these clocks separate; in
particular an SSE keep-alive must never renew the agent's first-output clock.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Iterator

logger = logging.getLogger(__name__)


class StreamPhases:
    """At most six fixed-schema INFO records per HTTP hop; no request/response data."""

    def __init__(self, previous: StreamPhases | None = None) -> None:
        self.at = time.monotonic()
        self.trace = previous.trace if previous is not None else uuid.uuid4().hex
        self.hop = previous.hop + 1 if previous is not None else 0
        self.seen: set[str] = set()
        self.raw_chunks = self.raw_chars = self.events = self.outputs = 0

    def mark(self, phase: str) -> None:
        if phase not in self.seen:
            self.seen.add(phase)
            logger.info("Gemini stream phase trace=%s hop=%d phase=%s elapsed_ms=%d", self.trace, self.hop, phase,
                        int((time.monotonic() - self.at) * 1000))

    def __enter__(self) -> StreamPhases:
        self.mark("request_start")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # Do not log exception text: providers may include response/prompt data.
        # Lifetime includes consumer suspension across yield; it is NOT upstream latency.
        outcome = "consumer_closed" if exc_type is GeneratorExit else "exception" if exc_type is not None else "normal"
        logger.info("Gemini stream end trace=%s hop=%d lifetime_elapsed_ms=%d raw_chunks=%d raw_chars=%d events=%d outputs=%d outcome=%s",
                    self.trace, self.hop, int((time.monotonic() - self.at) * 1000), self.raw_chunks, self.raw_chars,
                    self.events, self.outputs, outcome)

    def response(self, response: Any) -> Any:
        self.mark("response_headers")
        return _TextStream(response, self)

    def event(self) -> None:
        self.events += 1
        self.mark("first_sse_event")

    def output(self, chunks: list[Any]) -> None:
        self.outputs += len(chunks)
        if chunks:
            self.mark("first_translated_chunk")


class _TextStream:
    """Only the iter_text surface consumed by the existing native SSE parser."""

    def __init__(self, response: Any, phases: StreamPhases) -> None:
        self.response, self.phases = response, phases

    def iter_text(self) -> Iterator[str]:
        for text in self.response.iter_text():
            if text:
                self.phases.raw_chunks += 1
                self.phases.raw_chars += len(text)
                self.phases.mark("first_raw_text")
            yield text
