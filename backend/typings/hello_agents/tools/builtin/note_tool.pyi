"""Reviewed NoteTool typing surface for hello-agents 0.2.9."""

from abc import ABCMeta
from typing import Any

class NoteTool(metaclass=ABCMeta):
    def __init__(
        self,
        workspace: str = ...,
        auto_backup: bool = ...,
        max_notes: int = ...,
        expandable: bool = ...,
    ) -> None: ...
    def run(self, parameters: dict[str, Any]) -> str: ...
