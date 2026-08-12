"""Safe, descriptor-first storage for research artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, cast

from .contracts import normalize_run_id
from .intelligence import (
    ArtifactDescriptorV2,
    ArtifactManifestV2,
    ResearchIntelligenceBundle,
)

_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _now_iso() -> str:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, *, field_name: str, limit: int = 2048) -> str:
    """Validate one bounded text field at the artifact trust boundary."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty.")
    normalized = value.strip()
    if len(normalized) > limit:
        raise ValueError(f"{field_name} exceeds its bound.")
    return normalized


def _artifact_id(value: object) -> str:
    """Validate an internally generated, path-safe artifact identifier."""
    normalized = _text(value, field_name="artifact_id", limit=128)
    if (
        _ARTIFACT_ID_RE.fullmatch(normalized) is None
        or normalized in {".", ".."}
        or ".." in normalized
    ):
        raise ValueError("artifact_id must be a path-safe generated identifier.")
    return normalized


def normalize_artifact_id(value: object) -> str:
    """Validate and return one canonical, path-safe artifact identifier."""
    return _artifact_id(value)


def _source_ids(value: object) -> tuple[str, ...]:
    """Detach source IDs without accepting path-like values."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TypeError("source_ids must be a sequence.")
    result: list[str] = []
    for item in value:
        normalized = _text(item, field_name="source_id", limit=512)
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactPayload:
    """Content and safe metadata supplied to an :class:`ArtifactStore`."""

    artifact_id: str
    artifact_type: str
    mime_type: str
    title: str
    content: bytes | str
    description: str = ""
    source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Normalize content and reject untrusted path metadata."""
        object.__setattr__(self, "artifact_id", _artifact_id(self.artifact_id))
        for field_name in ("artifact_type", "mime_type", "title"):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name=field_name),
            )
        if not isinstance(self.description, str):
            raise TypeError("description must be text.")
        if isinstance(self.content, str):
            content = self.content.encode("utf-8")
        elif isinstance(self.content, (bytes, bytearray, memoryview)):
            content = bytes(self.content)
        else:
            raise TypeError("artifact content must be bytes or text.")
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "source_ids", _source_ids(self.source_ids))


class ArtifactStore(Protocol):
    """External artifact storage boundary used by schema-v2 manifests."""

    def put(self, run_id: str, artifact: ArtifactPayload) -> ArtifactDescriptorV2:
        """Atomically store one artifact and return its descriptor."""
        raise NotImplementedError

    def get(self, run_id: str, artifact_id: str) -> bytes:
        """Read one stored artifact by its canonical run and artifact IDs."""
        raise NotImplementedError


@dataclass(slots=True)
class FileArtifactStore:
    """Store artifact bodies beneath a repository root with atomic writes."""

    root: str | Path | Any
    _artifact_root: Path = field(init=False, repr=False)
    _lock: RLock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Resolve the repository root without accepting a caller path."""
        raw_root = (
            self.root
            if isinstance(self.root, (str, Path))
            else getattr(self.root, "root", self.root)
        )
        if not isinstance(raw_root, (str, Path)):
            raise TypeError("Artifact store root must be a path or FileRunRepository.")
        self.root = Path(raw_root).resolve(strict=False)
        self._artifact_root = self.root / "artifacts"
        self._lock = RLock()

    @property
    def artifact_root(self) -> Path:
        """Return the resolved artifact directory."""
        return self._artifact_root

    @staticmethod
    def _run_id(run_id: str) -> str:
        """Normalize a canonical UUID and reject path-like run IDs."""
        try:
            return normalize_run_id(run_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("run_id must be a canonical UUID string.") from exc

    @staticmethod
    def _safe_artifact_id(artifact_id: str) -> str:
        """Validate a generated artifact ID at every read/write boundary."""
        return _artifact_id(artifact_id)

    def _path(self, run_id: str, artifact_id: str) -> tuple[str, Path]:
        """Build and validate one path wholly beneath the artifact root."""
        normalized_run_id = self._run_id(run_id)
        normalized_artifact_id = self._safe_artifact_id(artifact_id)
        run_dir = self._artifact_root / normalized_run_id
        path = run_dir / normalized_artifact_id
        try:
            path.resolve(strict=False).relative_to(self._artifact_root.resolve(strict=False))
        except ValueError as exc:  # pragma: no cover - defensive after ID checks
            raise ValueError("Artifact path escapes the repository root.") from exc
        return normalized_run_id, path

    def put(self, run_id: str, artifact: ArtifactPayload) -> ArtifactDescriptorV2:
        """Atomically write an artifact and return a descriptor-only manifest entry."""
        if not isinstance(artifact, ArtifactPayload):
            raise TypeError("artifact must be an ArtifactPayload.")
        normalized_run_id, path = self._path(run_id, artifact.artifact_id)
        body = cast(bytes, artifact.content)
        checksum = hashlib.sha256(body).hexdigest()
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{artifact.artifact_id}.",
                    dir=path.parent,
                    delete=False,
                ) as handle:
                    temporary_path = Path(handle.name)
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, path)
                temporary_path = None
                if os.name != "nt":
                    try:
                        directory_fd = os.open(path.parent, os.O_RDONLY)
                    except OSError:
                        directory_fd = None
                    if directory_fd is not None:
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        return ArtifactDescriptorV2(
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type,
            mime_type=artifact.mime_type,
            path=(Path("artifacts") / normalized_run_id / artifact.artifact_id).as_posix(),
            title=artifact.title,
            description=artifact.description,
            source_ids=artifact.source_ids,
            size_bytes=len(body),
            checksum=checksum,
            created_at=_now_iso(),
        )

    def get(self, run_id: str, artifact_id: str) -> bytes:
        """Read one artifact body after revalidating both path components."""
        _, path = self._path(run_id, artifact_id)
        with self._lock:
            try:
                return path.read_bytes()
            except FileNotFoundError as exc:
                raise FileNotFoundError("Artifact was not found.") from exc

    def descriptor(
        self,
        run_id: str,
        artifact: ArtifactPayload,
    ) -> ArtifactDescriptorV2:
        """Return a descriptor for existing content without rewriting it."""
        body = self.get(run_id, artifact.artifact_id)
        checksum = hashlib.sha256(body).hexdigest()
        normalized_run_id, _ = self._path(run_id, artifact.artifact_id)
        return ArtifactDescriptorV2(
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type,
            mime_type=artifact.mime_type,
            path=(Path("artifacts") / normalized_run_id / artifact.artifact_id).as_posix(),
            title=artifact.title,
            description=artifact.description,
            source_ids=artifact.source_ids,
            size_bytes=len(body),
            checksum=checksum,
            created_at=_now_iso(),
        )


def persist_research_artifacts(
    store: ArtifactStore,
    run_id: str,
    bundle: ResearchIntelligenceBundle,
    *,
    report_markdown: str | None = None,
) -> ResearchIntelligenceBundle:
    """Double-write safe v2 artifact bodies and return a descriptor-only bundle.

    The returned bundle contains only descriptors.  The corresponding bodies
    are written through ``ArtifactStore`` and can be fetched independently.
    """
    if not isinstance(bundle, ResearchIntelligenceBundle):
        raise TypeError("bundle must be a ResearchIntelligenceBundle.")
    payloads: list[ArtifactPayload] = []
    if report_markdown is not None:
        payloads.append(
            ArtifactPayload(
                artifact_id="artifact_report_markdown",
                artifact_type="report_markdown",
                mime_type="text/markdown",
                title=bundle.report_spec.title,
                content=report_markdown,
                source_ids=tuple(item.source_id for item in bundle.sources),
            )
        )
    payloads.append(
        ArtifactPayload(
            artifact_id="artifact_evidence_json",
            artifact_type="evidence_json",
            mime_type="application/json",
            title="Research evidence",
            content=json.dumps(bundle.as_dict(), ensure_ascii=False, sort_keys=True),
            source_ids=tuple(item.source_id for item in bundle.sources),
        )
    )
    descriptors = tuple(store.put(run_id, payload) for payload in payloads)
    return replace(
        bundle,
        artifact_manifest=ArtifactManifestV2(artifacts=descriptors),
    )

__all__ = [
    "ArtifactPayload",
    "ArtifactStore",
    "FileArtifactStore",
    "normalize_artifact_id",
    "persist_research_artifacts",
]
