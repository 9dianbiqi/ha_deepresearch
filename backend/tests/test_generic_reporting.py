"""Task 6 red/green tests for generic reporting and artifact boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from models import TodoItem
from research.artifacts import (
    ArtifactPayload,
    FileArtifactStore,
    persist_research_artifacts,
)
from research.intelligence import (
    ArtifactManifestV2,
    ClaimRecord,
    CoverageDecision,
    EvidenceLocator,
    EvidenceRecord,
    GenericReportSpec,
    ResearchIntelligenceBundle,
    SourceReference,
)
from research.profiles import ResearchMode, built_in_profile_registry
from research.report_validation import CitationGate, validate_citations
from services.reporter import GenericReportingContext, ReportingService


def _bundle() -> ResearchIntelligenceBundle:
    source = SourceReference(
        provider_id="github",
        source_kind="repository_source",
        source_id="owner/repo:src/main.py",
        canonical_url="https://github.com/owner/repo",
        resolved_version="0123456789abcdef0123456789abcdef01234567",
        content_hash="sha256:source",
    )
    locator = EvidenceLocator(
        locator_type="line",
        url=(
            "https://github.com/owner/repo/blob/"
            "0123456789abcdef0123456789abcdef01234567/src/main.py#L4-L8"
        ),
        file_path="src/main.py",
        line_start=4,
        line_end=8,
    )
    evidence = EvidenceRecord(
        evidence_id="ev_allowed",
        source=source,
        evidence_type="source_excerpt",
        evidence_level="full_text",
        title="Main module",
        excerpt="The application starts from the main module.",
        locator=locator,
    )
    claim = ClaimRecord(
        claim_id="claim_overview",
        dimension="overview",
        statement="The repository exposes a main application module.",
        confidence="high",
        evidence_ids=(evidence.evidence_id,),
    )
    return ResearchIntelligenceBundle(
        mode=ResearchMode.GITHUB,
        profile_id="github.repository.v1",
        profile_version=1,
        sources=(source,),
        evidence=(evidence,),
        claims=(claim,),
        coverage=CoverageDecision(
            required_dimensions=("overview",),
            covered_dimensions=("overview",),
            coverage_score=1.0,
            allow_report=True,
        ),
        report_spec=GenericReportSpec(
            title="Repository report",
            claim_ids=(claim.claim_id,),
            citation_ids=(evidence.evidence_id,),
            limitations=("Only the captured source excerpt was reviewed.",),
        ),
        artifact_manifest=ArtifactManifestV2(),
        evidence_frozen=True,
    )


class _ReporterAgent:
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def run(self, prompt: str, **_: object) -> str:
        self.prompts.append(prompt)
        return self.response

    def clear_history(self) -> None:
        return None


def test_generic_reporter_uses_profile_and_deterministic_citations() -> None:
    """The reporter consumes generic context and cannot invent source links."""
    bundle = _bundle()
    profile = built_in_profile_registry().get("github.repository.v1")
    agent = _ReporterAgent(
        "# Repository report\n\n"
        "## Overview\n"
        "The repository is described by the captured source. "
        "See ev_allowed and ev_unknown at https://evil.example.invalid."
    )
    service = ReportingService(agent, type("Config", (), {"strip_thinking_tokens": False})())  # type: ignore[arg-type]
    context = GenericReportingContext(
        topic="owner/repo",
        profile=profile,
        bundle=bundle,
        tasks=(
            TodoItem(
                id=1,
                title="Repository overview",
                intent="Describe the repository",
                query="owner/repo overview",
                status="completed",
            ),
        ),
    )

    report = service.generate_report(context)

    assert agent.prompts
    assert "overview" in agent.prompts[0].casefold()
    assert "GitHub Evidence Contract" not in agent.prompts[0]
    assert "ev_allowed" in report
    assert "ev_unknown" not in report
    assert "evil.example.invalid" not in report
    assert "## Evidence References" in report
    assert "## Coverage and Limitations" in report
    assert "#L4-L8" in report


def test_citation_gate_rejects_unknown_ids_and_external_urls() -> None:
    bundle = _bundle()
    result = validate_citations(
        "Claim ev_allowed and ev_unknown; https://evil.example.invalid",
        bundle,
    )

    assert isinstance(result, CitationGate)
    assert not result.valid
    assert "ev_unknown" in result.unknown_evidence_ids
    assert "https://evil.example.invalid" in result.illegal_urls
    assert "ev_unknown" not in result.sanitized_report
    assert "evil.example.invalid" not in result.sanitized_report


def test_file_artifact_store_is_path_safe_and_descriptor_only(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path)
    run_id = "12345678-1234-5678-1234-567812345678"
    descriptor = store.put(
        run_id,
        ArtifactPayload(
            artifact_id="artifact_report",
            artifact_type="report_markdown",
            mime_type="text/markdown",
            title="Report",
            content="# Report\n",
        ),
    )

    assert descriptor.size_bytes == len(b"# Report\n")
    assert descriptor.checksum
    assert "content" not in descriptor.as_dict()
    assert store.get(run_id, "artifact_report") == b"# Report\n"
    assert (tmp_path / "artifacts" / "12345678123456781234567812345678" / "artifact_report").is_file()

    with pytest.raises(ValueError):
        store.put(
            run_id,
            ArtifactPayload(
                artifact_id="../escape",
                artifact_type="report_markdown",
                mime_type="text/markdown",
                title="Bad",
                content="bad",
            ),
        )


def test_v2_artifact_manifest_is_descriptor_only_and_body_is_external(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path)
    run_id = "12345678-1234-5678-1234-567812345678"
    bundle = persist_research_artifacts(
        store,
        run_id,
        _bundle(),
        report_markdown="# Repository report\n",
    )

    payload = bundle.as_dict()
    assert payload["evidence_frozen"] is True
    assert all("content" not in item for item in payload["artifact_manifest"]["artifacts"])
    for descriptor in bundle.artifact_manifest.artifacts:
        assert store.get(run_id, descriptor.artifact_id)
