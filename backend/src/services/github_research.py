"""GitHub repository research helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import requests

GITHUB_URL_RE = re.compile(
    r"github\.com[/:](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
OWNER_REPO_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$"
)
OWNER_REPO_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<owner>[A-Za-z0-9_.-]{1,39})/(?P<repo>[A-Za-z0-9_.-]{1,100})(?![A-Za-z0-9_.-])"
)
MAX_README_CHARS = 6000
MAX_TREE_LINES = 120


class HTTPSession(Protocol):
    """Minimal requests-like interface used by the GitHub client."""

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        timeout: int,
    ) -> Any:
        """Execute an HTTP GET request."""


@dataclass(frozen=True)
class GitHubRepositoryTarget:
    """A normalized GitHub repository identifier."""

    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        """Return ``owner/repo``."""
        return f"{self.owner}/{self.repo}"

    @property
    def html_url(self) -> str:
        """Return the public GitHub repository URL."""
        return f"https://github.com/{self.full_name}"


@dataclass(kw_only=True)
class GitHubRepositoryContext:
    """Structured GitHub data collected for repository research."""

    target: GitHubRepositoryTarget
    repository: dict[str, Any] = field(default_factory=dict)
    commit_sha: str | None = None
    readme_excerpt: str = ""
    tree_excerpt: str = ""
    file_manifest: list[dict[str, Any]] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)
    contributors: list[dict[str, Any]] = field(default_factory=list)
    commits: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    pull_requests: list[dict[str, Any]] = field(default_factory=list)
    releases: list[dict[str, Any]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    notice_codes: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        """Format the context for downstream LLM prompts."""
        parts = [
            "## GitHub Repository Context",
            f"- Repository: {self.target.full_name}",
            f"- URL: {self.target.html_url}",
        ]

        if self.repository:
            parts.append("\n### Repository Information")
            for key in (
                "description",
                "stars",
                "forks",
                "open_issues",
                "language",
                "license",
                "created_at",
                "updated_at",
                "pushed_at",
                "default_branch",
            ):
                value = self.repository.get(key)
                if value not in (None, "", []):
                    parts.append(f"- {key}: {value}")
            topics = self.repository.get("topics") or []
            if topics:
                parts.append(f"- topics: {', '.join(str(item) for item in topics)}")

        if self.commit_sha:
            parts.append(f"- snapshot_commit: {self.commit_sha}")

        if self.languages:
            parts.append("\n### Languages")
            parts.extend(f"- {name}: {size}" for name, size in self.languages.items())

        if self.readme_excerpt:
            parts.append("\n### README Excerpt")
            parts.append(self.readme_excerpt)

        if self.tree_excerpt:
            parts.append("\n### Repository Tree")
            parts.append(f"```text\n{self.tree_excerpt}\n```")

        if self.commits:
            parts.append("\n### Recent Commits")
            for item in self.commits[:10]:
                parts.append(
                    f"- {item.get('date', '')} {item.get('sha', '')}: "
                    f"{item.get('message', '')} ({item.get('author', '')})"
                )

        if self.issues:
            parts.append("\n### Recent Issues")
            for item in self.issues[:10]:
                parts.append(
                    f"- #{item.get('number')}: {item.get('title')} "
                    f"[{item.get('state')}] {item.get('url')}"
                )

        if self.pull_requests:
            parts.append("\n### Recent Pull Requests")
            for item in self.pull_requests[:10]:
                parts.append(
                    f"- #{item.get('number')}: {item.get('title')} "
                    f"[{item.get('state')}] {item.get('url')}"
                )

        if self.releases:
            parts.append("\n### Releases")
            for item in self.releases[:10]:
                parts.append(
                    f"- {item.get('tag')}: {item.get('name')} "
                    f"({item.get('published_at')}) {item.get('url')}"
                )

        if self.notices:
            parts.append("\n### GitHub API Notices")
            parts.extend(f"- {notice}" for notice in self.notices)

        return "\n".join(parts).strip()


def parse_github_repository(topic: str | None) -> GitHubRepositoryTarget | None:
    """Return a GitHub repository target when the topic names one."""
    if not topic:
        return None

    url_match = GITHUB_URL_RE.search(topic)
    if url_match:
        return _target_from_match(url_match)

    cleaned = topic.strip().strip("`'\"")
    shorthand_match = OWNER_REPO_RE.match(cleaned)
    if shorthand_match:
        return _target_from_match(shorthand_match)

    return None


def parse_github_repositories(topic: str | None, *, limit: int = 5) -> list[GitHubRepositoryTarget]:
    """Return up to five distinct GitHub repositories named by a topic."""
    if not topic:
        return []
    candidates: list[tuple[int, GitHubRepositoryTarget]] = []
    for match in GITHUB_URL_RE.finditer(topic):
        candidates.append((match.start(), _target_from_match(match)))
    for match in OWNER_REPO_TOKEN_RE.finditer(topic):
        target = _target_from_match(match)
        if target.owner.casefold() != "github.com":
            candidates.append((match.start(), target))
    candidates.sort(key=lambda item: item[0])
    targets: list[GitHubRepositoryTarget] = []
    seen: set[str] = set()
    for _, target in candidates:
        if target.full_name.casefold() not in seen:
            targets.append(target)
            seen.add(target.full_name.casefold())
    if not targets:
        fallback_target = parse_github_repository(topic)
        if fallback_target is not None:
            targets.append(fallback_target)
    return targets[: max(0, min(limit, 5))]


def _target_from_match(match: re.Match[str]) -> GitHubRepositoryTarget:
    owner = match.group("owner").strip()
    repo = match.group("repo").strip()
    if repo.endswith(".git"):
        repo = repo[:-4]
    return GitHubRepositoryTarget(owner=owner, repo=repo)


class GitHubResearchClient:
    """Small read-only GitHub REST API client for repository research."""

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str = "https://api.github.com",
        session: HTTPSession | None = None,
        timeout: int = 30,
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        """Configure the GitHub API client and optional operation checkpoint."""
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._timeout = timeout
        self._checkpoint = checkpoint or (lambda: None)

    def collect_repository_context(
        self,
        target: GitHubRepositoryTarget,
    ) -> GitHubRepositoryContext:
        """Collect GitHub API data for a repository."""
        notices: list[str] = []
        raw_info = self._get_json(f"/repos/{target.full_name}", notices=notices)
        if not isinstance(raw_info, dict) or not raw_info:
            return GitHubRepositoryContext(
                target=target,
                notices=notices,
                notice_codes=self._notice_codes(notices),
            )

        repository = self._summarize_repository(raw_info)
        default_branch = str(repository.get("default_branch") or "main")

        readme = self._get_raw(f"/repos/{target.full_name}/readme", notices=notices)
        tree = self._get_json(
            f"/repos/{target.full_name}/git/trees/{default_branch}",
            notices=notices,
            params={"recursive": "1"},
        )
        languages = self._get_json(f"/repos/{target.full_name}/languages", notices=notices)
        contributors = self._get_json(
            f"/repos/{target.full_name}/contributors",
            notices=notices,
            params={"per_page": 30},
        )
        commits = self._get_json(
            f"/repos/{target.full_name}/commits",
            notices=notices,
            params={"per_page": 30},
        )
        issues = self._get_json(
            f"/repos/{target.full_name}/issues",
            notices=notices,
            params={"state": "all", "per_page": 30},
        )
        pull_requests = self._get_json(
            f"/repos/{target.full_name}/pulls",
            notices=notices,
            params={"state": "all", "per_page": 30},
        )
        releases = self._get_json(
            f"/repos/{target.full_name}/releases",
            notices=notices,
            params={"per_page": 10},
        )

        commit_sha = self._latest_commit_sha(commits)
        file_manifest = self._format_file_manifest(tree)

        return GitHubRepositoryContext(
            target=target,
            repository=repository,
            commit_sha=commit_sha,
            readme_excerpt=self._truncate(readme or "", MAX_README_CHARS),
            tree_excerpt=self._format_tree(tree),
            file_manifest=file_manifest,
            languages=languages if isinstance(languages, dict) else {},
            contributors=self._summarize_contributors(contributors),
            commits=self._summarize_commits(commits),
            issues=self._summarize_issues(issues),
            pull_requests=self._summarize_pull_requests(pull_requests),
            releases=self._summarize_releases(releases),
            notices=notices,
            notice_codes=self._notice_codes(notices),
        )

    def _headers(self, *, accept: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": accept or "application/vnd.github.v3+json",
            "User-Agent": "helloagents-deepresearch/1.0",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get_json(
        self,
        path: str,
        *,
        notices: list[str],
        params: dict[str, Any] | None = None,
    ) -> Any:
        response = self._request(path, notices=notices, params=params)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            notices.append(f"GitHub API returned invalid JSON for {path}.")
            return None

    def _get_raw(self, path: str, *, notices: list[str]) -> str | None:
        response = self._request(
            path,
            notices=notices,
            accept="application/vnd.github.raw",
        )
        if response is None:
            return None
        return str(getattr(response, "text", "") or "")

    def _request(
        self,
        path: str,
        *,
        notices: list[str],
        params: dict[str, Any] | None = None,
        accept: str | None = None,
    ) -> Any | None:
        url = f"{self._base_url}{path}"
        self._checkpoint()
        try:
            response = self._session.get(
                url,
                headers=self._headers(accept=accept),
                params=params,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            self._checkpoint()
            notices.append(f"GitHub API request failed for {path}: {exc}")
            return None
        self._checkpoint()

        status_code = int(getattr(response, "status_code", 0) or 0)
        if 200 <= status_code < 300:
            return response

        message = self._response_message(response)
        headers = getattr(response, "headers", {})
        remaining = headers.get("X-RateLimit-Remaining") if isinstance(headers, dict) else None
        is_rate_limited = (
            status_code == 429
            or "rate limit" in message.lower()
            or str(remaining).strip() == "0"
        )
        if status_code == 404:
            notices.append(f"GitHub API resource not found for {path}: {message}")
        elif status_code in {403, 429} and is_rate_limited:
            suffix = " Set GITHUB_TOKEN to increase the API rate limit." if not self._token else ""
            notices.append(f"GitHub API rate limit reached for {path}: {message}.{suffix}")
        else:
            notices.append(f"GitHub API request failed for {path}: HTTP {status_code} {message}")
        return None

    @staticmethod
    def _response_message(response: Any) -> str:
        try:
            payload = response.json()
        except ValueError:
            return str(getattr(response, "text", "") or "").strip()
        if isinstance(payload, dict):
            return str(payload.get("message") or getattr(response, "text", "") or "").strip()
        return str(getattr(response, "text", "") or "").strip()

    @staticmethod
    def _notice_codes(notices: list[str]) -> list[str]:
        """Map provider notices to stable UI and telemetry codes."""
        codes: list[str] = []
        for notice in notices:
            lowered = notice.casefold()
            if "rate limit" in lowered:
                code = "github_rate_limited"
            elif "not found" in lowered:
                code = "github_not_found"
            elif "invalid json" in lowered:
                code = "github_invalid_json"
            else:
                code = "github_request_failed"
            if code not in codes:
                codes.append(code)
        return codes

    @staticmethod
    def _summarize_repository(info: dict[str, Any]) -> dict[str, Any]:
        license_info = info.get("license") if isinstance(info.get("license"), dict) else {}
        return {
            "name": info.get("full_name"),
            "description": info.get("description"),
            "url": info.get("html_url"),
            "stars": info.get("stargazers_count"),
            "forks": info.get("forks_count"),
            "open_issues": info.get("open_issues_count"),
            "language": info.get("language"),
            "license": license_info.get("spdx_id") if license_info else None,
            "created_at": info.get("created_at"),
            "updated_at": info.get("updated_at"),
            "pushed_at": info.get("pushed_at"),
            "default_branch": info.get("default_branch"),
            "topics": info.get("topics") or [],
        }

    @staticmethod
    def _summarize_contributors(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        return [
            {
                "login": item.get("login"),
                "contributions": item.get("contributions"),
                "url": item.get("html_url"),
            }
            for item in payload
            if isinstance(item, dict)
        ]

    @staticmethod
    def _summarize_commits(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        commits: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            commit_payload = item.get("commit")
            commit = commit_payload if isinstance(commit_payload, dict) else {}
            author_payload = commit.get("author")
            author = author_payload if isinstance(author_payload, dict) else {}
            commits.append(
                {
                    "sha": str(item.get("sha") or ""),
                    "message": str(commit.get("message") or "").splitlines()[0],
                    "author": author.get("name"),
                    "date": author.get("date"),
                    "url": item.get("html_url"),
                }
            )
        return commits

    @staticmethod
    def _latest_commit_sha(payload: Any) -> str | None:
        """Return the full SHA of the latest default-branch commit when present."""
        if not isinstance(payload, list):
            return None
        for item in payload:
            if not isinstance(item, dict):
                continue
            sha = item.get("sha")
            if isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
                return sha.lower()
        return None

    @staticmethod
    def _summarize_issues(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        issues: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict) or item.get("pull_request"):
                continue
            issues.append(
                {
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "state": item.get("state"),
                    "url": item.get("html_url"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                }
            )
        return issues

    @staticmethod
    def _summarize_pull_requests(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        return [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "url": item.get("html_url"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
            for item in payload
            if isinstance(item, dict)
        ]

    @staticmethod
    def _summarize_releases(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        return [
            {
                "tag": item.get("tag_name"),
                "name": item.get("name"),
                "published_at": item.get("published_at"),
                "url": item.get("html_url"),
            }
            for item in payload
            if isinstance(item, dict)
        ]

    @staticmethod
    def _format_tree(payload: Any) -> str:
        if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
            return ""
        lines: list[str] = []
        for item in payload["tree"]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if not path:
                continue
            depth = path.count("/")
            if depth >= 3:
                continue
            suffix = "/" if item.get("type") == "tree" else ""
            lines.append(f"{'  ' * depth}{path}{suffix}")
            if len(lines) >= MAX_TREE_LINES:
                break
        return "\n".join(lines)

    @staticmethod
    def _format_file_manifest(payload: Any) -> list[dict[str, Any]]:
        """Return a bounded, safe file manifest from a recursive tree response."""
        if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
            return []
        manifest: list[dict[str, Any]] = []
        for item in payload["tree"]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            item_type = str(item.get("type") or "").strip()
            if not path or item_type not in {"blob", "tree"}:
                continue
            entry: dict[str, Any] = {"path": path, "type": item_type}
            sha = item.get("sha")
            if isinstance(sha, str) and sha:
                entry["sha"] = sha
            size = item.get("size")
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                entry["size"] = size
            manifest.append(entry)
            if len(manifest) >= MAX_TREE_LINES:
                break
        return manifest

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[:limit]}... [truncated]"
