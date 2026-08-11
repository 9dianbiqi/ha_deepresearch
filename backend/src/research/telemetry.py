"""Best-effort LLM response telemetry for one research run."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Iterator, Mapping, cast

from hello_agents import HelloAgentsLLM  # type: ignore[import-untyped]

_CURRENT_SESSION: ContextVar[tuple[object, str | None, int] | None] = ContextVar(
    "research_llm_telemetry_session",
    default=None,
)
_MAX_TEXT = 128


@contextmanager
def llm_telemetry_scope(
    session: object,
    *,
    role: str | None = None,
    retry_count: int | None = None,
) -> Iterator[None]:
    """Bind one run session to LLM calls made in the current execution context."""
    current = _CURRENT_SESSION.get()
    inherited_retry_count = current[2] if current is not None else 0
    effective_retry_count = (
        retry_count
        if isinstance(retry_count, int) and not isinstance(retry_count, bool)
        else inherited_retry_count
    )
    token = _CURRENT_SESSION.set((session, role, max(0, effective_retry_count)))
    try:
        yield
    finally:
        _CURRENT_SESSION.reset(token)


def current_llm_telemetry_session() -> object | None:
    """Return the session currently receiving provider telemetry, if any."""
    current = _CURRENT_SESSION.get()
    return current[0] if current is not None else None


def current_llm_telemetry_role() -> str | None:
    """Return the logical role of the current provider call."""
    current = _CURRENT_SESSION.get()
    return current[1] if current is not None else None


def current_llm_telemetry_retry_count() -> int:
    """Return the report retry ordinal bound to the current provider call."""
    current = _CURRENT_SESSION.get()
    return current[2] if current is not None else 0


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _bounded_text(value: object, *, max_length: int = _MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:max_length]


def _number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _usage_fields(response: object) -> tuple[int | None, int | None, int | None]:
    usage = _field(response, "usage")
    return (
        _number(_field(usage, "prompt_tokens")),
        _number(_field(usage, "completion_tokens")),
        _number(_field(usage, "total_tokens")),
    )


def _request_id(response: object) -> str | None:
    return _bounded_text(
        _field(response, "_request_id") or _field(response, "request_id") or _field(response, "id")
    )


def _choice(response: object) -> object | None:
    choices = _field(response, "choices")
    if isinstance(choices, (list, tuple)) and choices:
        return choices[0]
    return None


def _message_content(response: object) -> str:
    choice = _choice(response)
    message = _field(choice, "message")
    content = _field(message, "content")
    return content if isinstance(content, str) else ""


def _delta_content(response: object) -> str:
    choice = _choice(response)
    delta = _field(choice, "delta")
    content = _field(delta, "content")
    return content if isinstance(content, str) else ""


def _finish_reason(response: object) -> str | None:
    choice = _choice(response)
    return _bounded_text(_field(choice, "finish_reason"), max_length=64)


def _counter(record: Mapping[str, object], name: str) -> int:
    """Read a non-negative integer counter from a dynamic telemetry record."""
    value = record.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _stream_options_rejected(exc: BaseException) -> bool:
    """Detect providers that reject OpenAI's optional stream usage hint."""
    status_code = getattr(exc, "status_code", None)
    text = str(exc).casefold()
    if status_code not in {400, 404, 422} and not isinstance(exc, TypeError):
        return False
    return any(
        marker in text
        for marker in ("stream_options", "include_usage", "unknown argument", "unsupported")
    )


def _emit(record: dict[str, object]) -> None:
    session = current_llm_telemetry_session()
    if session is None:
        return
    recorder = getattr(session, "record_llm_telemetry", None)
    if callable(recorder):
        try:
            recorder(record)
        except Exception:
            # Telemetry must never turn a successful provider response into a
            # failed research run.
            pass
        return


class TelemetryHelloAgentsLLM(HelloAgentsLLM):
    """OpenAI-compatible LLM client retaining the response metadata HelloAgents drops."""

    def __init__(self, *args: Any, include_stream_usage: bool = True, **kwargs: Any) -> None:
        """Initialize the pinned HelloAgents client and usage preference."""
        super().__init__(*args, **kwargs)
        self.include_stream_usage = include_stream_usage

    def invoke(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """Invoke one completion and record usage, finish reason, and timing."""
        started_at = monotonic()
        record = self._base_record(mode="invoke", started_at=started_at)
        try:
            client = getattr(self, "_client", None)
            chat = getattr(client, "chat", None)
            completions = getattr(chat, "completions", None)
            create = getattr(completions, "create", None)
            if not callable(create):
                response = super().invoke(messages, **kwargs)
                record["output_chars"] = len(response)
                record["stream_completed"] = None
                return response

            request_kwargs = dict(kwargs)
            temperature = request_kwargs.pop(
                "temperature",
                getattr(self, "temperature", None),
            )
            max_tokens = request_kwargs.pop(
                "max_tokens",
                getattr(self, "max_tokens", None),
            )
            response = create(
                model=getattr(self, "model", None),
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **request_kwargs,
            )
            prompt_tokens, output_tokens, total_tokens = _usage_fields(response)
            record.update(
                {
                    "request_id": _request_id(response),
                    "input_tokens": prompt_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "usage_available": any(
                        value is not None
                        for value in (prompt_tokens, output_tokens, total_tokens)
                    ),
                    "finish_reason": _finish_reason(response),
                }
            )
            output = _message_content(response)
            record["output_chars"] = len(output)
            record["stream_completed"] = None
            return output
        except BaseException as exc:
            record["exception_type"] = type(exc).__name__
            raise
        finally:
            record["duration_ms"] = _duration_ms(started_at)
            _emit(record)

    def stream_invoke(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> Iterator[str]:
        """Stream one completion while recording chunk and terminal metadata."""
        started_at = monotonic()
        record = self._base_record(mode="stream", started_at=started_at)
        record.update(
            {
                "chunk_count": 0,
                "first_chunk_latency_ms": None,
                "output_chars": 0,
                "stream_completed": False,
            }
        )
        response: object | None = None
        try:
            client = getattr(self, "_client", None)
            chat = getattr(client, "chat", None)
            completions = getattr(chat, "completions", None)
            create = getattr(completions, "create", None)
            if not callable(create):
                # Keep the inherited fallback usable for lightweight test or
                # embedded delegates that do not expose a raw OpenAI client.
                for fallback_chunk in super().stream_invoke(messages, **kwargs):
                    record["chunk_count"] = _counter(record, "chunk_count") + 1
                    record["output_chars"] = _counter(record, "output_chars") + len(
                        fallback_chunk
                    )
                    if record["first_chunk_latency_ms"] is None:
                        record["first_chunk_latency_ms"] = _duration_ms(started_at)
                    yield fallback_chunk
                record["stream_completed"] = True
                return

            request_kwargs = dict(kwargs)
            temperature = request_kwargs.pop(
                "temperature",
                getattr(self, "temperature", None),
            )
            max_tokens = request_kwargs.pop(
                "max_tokens",
                getattr(self, "max_tokens", None),
            )
            if self.include_stream_usage:
                stream_options = request_kwargs.get("stream_options")
                if isinstance(stream_options, Mapping):
                    request_kwargs["stream_options"] = {
                        **dict(stream_options),
                        "include_usage": True,
                    }
                else:
                    request_kwargs["stream_options"] = {"include_usage": True}
            try:
                response = create(
                    model=getattr(self, "model", None),
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                    **request_kwargs,
                )
            except Exception as exc:
                if "stream_options" not in request_kwargs or not _stream_options_rejected(exc):
                    raise
                request_kwargs.pop("stream_options", None)
                response = create(
                    model=getattr(self, "model", None),
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                    **request_kwargs,
                )
            record["request_id"] = _request_id(response)
            stream = cast(Iterator[object], response)
            for stream_chunk in stream:
                record["chunk_count"] = _counter(record, "chunk_count") + 1
                if record.get("request_id") is None:
                    record["request_id"] = _request_id(stream_chunk)
                if record["first_chunk_latency_ms"] is None:
                    record["first_chunk_latency_ms"] = _duration_ms(started_at)
                content = _delta_content(stream_chunk)
                if content:
                    record["output_chars"] = _counter(record, "output_chars") + len(content)
                    yield content
                finish_reason = _finish_reason(stream_chunk)
                if finish_reason is not None:
                    record["finish_reason"] = finish_reason
                prompt_tokens, output_tokens, total_tokens = _usage_fields(stream_chunk)
                if any(value is not None for value in (prompt_tokens, output_tokens, total_tokens)):
                    record.update(
                        {
                            "input_tokens": prompt_tokens,
                            "output_tokens": output_tokens,
                            "total_tokens": total_tokens,
                            "usage_available": True,
                        }
                    )
            record["stream_completed"] = True
        except BaseException as exc:
            record["exception_type"] = type(exc).__name__
            raise
        finally:
            if response is not None and record.get("request_id") is None:
                record["request_id"] = _request_id(response)
            record["duration_ms"] = _duration_ms(started_at)
            _emit(record)

    def _base_record(self, *, mode: str, started_at: float) -> dict[str, object]:
        """Build the bounded common telemetry envelope."""
        provider = _bounded_text(getattr(self, "provider", None))
        model = _bounded_text(getattr(self, "model", None))
        return {
            "role": _bounded_text(current_llm_telemetry_role(), max_length=32) or "llm",
            "provider": provider,
            "model": model,
            "mode": mode,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": _duration_ms(started_at),
            "request_id": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "usage_available": False,
            "output_chars": 0,
            "finish_reason": None,
            "exception_type": None,
            "stream_completed": None,
            "retry_count": current_llm_telemetry_retry_count(),
        }


def _duration_ms(started_at: float) -> float:
    return round(max(0.0, monotonic() - started_at) * 1000, 3)


__all__ = [
    "TelemetryHelloAgentsLLM",
    "current_llm_telemetry_session",
    "current_llm_telemetry_role",
    "current_llm_telemetry_retry_count",
    "llm_telemetry_scope",
]
