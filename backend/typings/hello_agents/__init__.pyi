"""Reviewed public typing surface for hello-agents 0.2.9."""

from abc import ABCMeta
from collections.abc import Callable, Iterator
from typing import Any, Literal

class HelloAgentsLLM:
    def __init__(
        self,
        model: str | None = ...,
        api_key: str | None = ...,
        base_url: str | None = ...,
        provider: Literal[
            "openai",
            "deepseek",
            "qwen",
            "modelscope",
            "kimi",
            "zhipu",
            "ollama",
            "vllm",
            "local",
            "auto",
            "custom",
        ]
        | None = ...,
        temperature: float = ...,
        max_tokens: int | None = ...,
        timeout: int | None = ...,
        **kwargs: Any,
    ) -> None: ...
    def invoke(self, messages: list[dict[str, str]], **kwargs: Any) -> str: ...
    def stream_invoke(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> Iterator[str]: ...

class SimpleAgent(metaclass=ABCMeta):
    llm: HelloAgentsLLM
    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: str | None = ...,
        config: object | None = ...,
        tool_registry: object | None = ...,
        enable_tool_calling: bool = ...,
    ) -> None: ...
    def run(
        self, input_text: str, max_tool_iterations: int = ..., **kwargs: Any
    ) -> str: ...
    def stream_run(self, input_text: str, **kwargs: Any) -> Iterator[str]: ...
    def clear_history(self) -> None: ...

class ToolAwareSimpleAgent(SimpleAgent):
    def __init__(
        self,
        *args: Any,
        tool_call_listener: Callable[[dict[str, Any]], None] | None = ...,
        **kwargs: Any,
    ) -> None: ...
    def stream_run(
        self, input_text: str, max_tool_iterations: int = ..., **kwargs: Any
    ) -> Iterator[str]: ...
