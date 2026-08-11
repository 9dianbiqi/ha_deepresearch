"""Tests for provider and transport telemetry without making network calls."""

from __future__ import annotations

from types import SimpleNamespace

from research.session import RunSession
from research.telemetry import TelemetryHelloAgentsLLM, llm_telemetry_scope


class RecordingSession:
    """Capture telemetry records emitted by the instrumented client."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def record_llm_telemetry(self, record: dict[str, object]) -> None:
        self.records.append(record)


def _client(response: object) -> object:
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: response)
        )
    )


def _llm(response: object, *, mode: str = "custom") -> TelemetryHelloAgentsLLM:
    llm = object.__new__(TelemetryHelloAgentsLLM)
    llm._client = _client(response)  # type: ignore[attr-defined]
    llm.model = "telemetry-model"
    llm.provider = mode
    llm.temperature = 0.2
    llm.max_tokens = 128
    llm.include_stream_usage = True
    return llm


def test_invoke_records_usage_finish_reason_and_request_id() -> None:
    response = SimpleNamespace(
        id="req-invoke-1",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="complete response"),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )
    llm = _llm(response)
    session = RecordingSession()

    with llm_telemetry_scope(session, role="reporter", retry_count=1):
        assert llm.invoke([{"role": "user", "content": "question"}]) == (
            "complete response"
        )

    assert len(session.records) == 1
    record = session.records[0]
    assert record["role"] == "reporter"
    assert record["provider"] == "custom"
    assert record["model"] == "telemetry-model"
    assert record["mode"] == "invoke"
    assert record["request_id"] == "req-invoke-1"
    assert record["input_tokens"] == 11
    assert record["output_tokens"] == 7
    assert record["total_tokens"] == 18
    assert record["finish_reason"] == "stop"
    assert record["retry_count"] == 1
    assert record["output_chars"] == len("complete response")
    assert record["exception_type"] is None


def test_stream_records_usage_finish_reason_and_completion() -> None:
    chunks = [
        SimpleNamespace(
            id="req-stream-1",
            choices=[SimpleNamespace(delta=SimpleNamespace(content="part "), finish_reason=None)],
            usage=None,
        ),
        SimpleNamespace(
            id="req-stream-1",
            choices=[SimpleNamespace(delta=SimpleNamespace(content="one"), finish_reason="length")],
            usage=None,
        ),
        SimpleNamespace(
            id="req-stream-1",
            choices=[],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        ),
    ]

    class FakeStream:
        def __iter__(self):
            return iter(chunks)

    llm = _llm(FakeStream())
    session = RecordingSession()

    with llm_telemetry_scope(session, role="reporter"):
        output = "".join(llm.stream_invoke([{"role": "user", "content": "q"}]))

    assert output == "part one"
    record = session.records[0]
    assert record["mode"] == "stream"
    assert record["request_id"] == "req-stream-1"
    assert record["chunk_count"] == 3
    assert record["first_chunk_latency_ms"] is not None
    assert record["stream_completed"] is True
    assert record["finish_reason"] == "length"
    assert record["input_tokens"] == 5
    assert record["output_tokens"] == 3
    assert record["total_tokens"] == 8
    assert record["usage_available"] is True
    assert record["retry_count"] == 0


def test_stream_exception_is_recorded_as_incomplete() -> None:
    class ExplodingStream:
        def __iter__(self):
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="partial"),
                        finish_reason=None,
                    )
                ]
            )
            raise RuntimeError("provider disconnected")

    llm = _llm(ExplodingStream())
    session = RecordingSession()

    try:
        with llm_telemetry_scope(session, role="summarizer"):
            list(llm.stream_invoke([{"role": "user", "content": "q"}]))
    except RuntimeError:
        pass
    else:
        raise AssertionError("the fake provider should fail")

    record = session.records[0]
    assert record["stream_completed"] is False
    assert record["exception_type"] == "RuntimeError"
    assert record["output_chars"] == len("partial")


def test_run_session_persists_bounded_llm_summary() -> None:
    from test_run_session import make_session

    session: RunSession = make_session()
    for index in range(66):
        session.record_llm_telemetry(
            {
                "role": "reporter",
                "mode": "stream",
                "finish_reason": "stop" if index % 2 else "length",
                "stream_completed": index % 3 != 0,
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
                "output_chars": 10,
                "duration_ms": 1.5,
            }
        )

    telemetry = session.metrics["llm"]
    assert len(telemetry["calls"]) == 64
    assert telemetry["dropped_calls"] == 2
    assert telemetry["summary"]["call_count"] == 64
    assert telemetry["summary"]["stream_incomplete_count"] > 0
    assert session.latest_llm_finish_reason(role="reporter") in {"stop", "length"}
