"""Built-in source-provider adapters."""

from .github import GitHubSourceProvider
from .web import WebSourceProvider

__all__ = ["GitHubSourceProvider", "WebSourceProvider"]
