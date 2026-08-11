"""Reviewed public tool typing surface for hello-agents 0.2.9."""

from abc import ABCMeta
from typing import Any

class SearchTool(metaclass=ABCMeta):
    def __init__(
        self,
        backend: str = ...,
        tavily_key: str | None = ...,
        serpapi_key: str | None = ...,
        perplexity_key: str | None = ...,
    ) -> None: ...
    def run(self, parameters: dict[str, Any]) -> str | dict[str, Any]: ...

class NoteTool(metaclass=ABCMeta):
    def __init__(
        self,
        workspace: str = ...,
        auto_backup: bool = ...,
        max_notes: int = ...,
        expandable: bool = ...,
    ) -> None: ...
    def run(self, parameters: dict[str, Any]) -> str: ...
