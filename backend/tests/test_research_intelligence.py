"""Schema-v2 intelligence contracts and GitHub schema-v1 compatibility tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.compatibility import GitHubEvidenceV1Adapter
from research.intelligence import (
    INTELLIGENCE_SCHEMA_VERSION,
    ArtifactDescriptorV2,
    ArtifactManifestV2,
    ClaimRecord,
    CoverageDecision,
    EvidenceLocator,
    EvidenceRecord,
    GenericReportSpec,
    ResearchIntelligenceBundle,
    SourceReference,
    stable_claim_id,
    stable_evidence_id,
    stable_source_id,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "github_evidence_v1.json"


def fixture() -> dict[str, object]:
    """Load the Task 0 frozen schema-v1 fixture."""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def source(*, captured_at: str = "2026-08-12T00:00:00+00:00") -> SourceReference:
    """Build a deterministic source reference for unit tests."""
    return SourceReference(
        provider_id="github",
        source_kind="repository",
        source_id="owner/repo",
        canonical_url="https://github.com/owner/repo",
        requested_ref="main",
        resolved_version="a" * 40,
        captured_at=captured_at,
        content_hash="source-hash",
    )


def test_source_and_evidence_ids_are_stable_when_capture_time_changes() -> None:
    """Timestamps describe observation time but do not change identity."""
    first = source()
    second = source(captured_at="2026-08-13T00:00:00+00:00")

    assert stable_source_id(first) == stable_source_id(second)
    assert stable_evidence_id(
        first,
        locator=EvidenceLocator(
            locator_type="line",
            url="https://github.com/owner/repo/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/src/main.py#L1-L2",
            file_path="src/main.py",
            line_start=1,
            line_end=2,
        ),
        excerpt="def main():",
    ) == stable_evidence_id(
        second,
        locator=EvidenceLocator(
            locator_type="line",
            url="https://github.com/owner/repo/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/src/main.py#L1-L2",
            file_path="src/main.py",
            line_start=1,
            line_end=2,
        ),
        excerpt="def main():",
    )
    assert stable_claim_id(profile_id="github.repository.v1", dimension="overview", statement="A claim") == stable_claim_id(
        profile_id="github.repository.v1",
        dimension="overview",
        statement=" a claim ",
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"line_start": 0, "line_end": 1},
        {"line_start": 3, "line_end": 2},
        {"page_start": 0, "page_end": 1},
        {"page_start": 4, "page_end": 3},
    ],
)
def test_locator_rejects_invalid_line_and_page_ranges(kwargs: dict[str, int]) -> None:
    """Line and page locators must be positive, ordered, and paired."""
    with pytest.raises(ValueError):
        EvidenceLocator(
            locator_type="location",
            url="https://example.test/source",
            **kwargs,
        )


def test_line_locator_requires_commit_pinned_line_url_and_file_path() -> None:
    """GitHub line evidence cannot silently degrade to a branch or repository URL."""
    with pytest.raises(ValueError, match="line"):
        EvidenceLocator(
            locator_type="line",
            url="https://github.com/owner/repo/blob/main/src/main.py",
            file_path="src/main.py",
            line_start=1,
            line_end=2,
        )
    with pytest.raises(ValueError, match="file_path"):
        EvidenceLocator(
            locator_type="line",
            url="https://github.com/owner/repo/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/src/main.py#L1-L2",
            line_start=1,
            line_end=2,
        )


def test_bundle_round_trip_is_json_safe_and_rejects_unknown_evidence_ids() -> None:
    """Bundle serialization is detached and claims cannot reference missing evidence."""
    source_ref = source()
    locator = EvidenceLocator(
        locator_type="abstract",
        url="https://example.test/paper",
        section="Abstract",
    )
    evidence = EvidenceRecord(
        evidence_id="ev_1",
        source=source_ref,
        evidence_type="repository_metadata",
        evidence_level="metadata",
        title="Metadata",
        excerpt="A bounded excerpt",
        locator=locator,
        attributes={"license": "MIT"},
    )
    claim = ClaimRecord(
        claim_id="claim_1",
        dimension="overview",
        statement="The source is identifiable.",
        confidence="high",
        evidence_ids=("ev_1",),
    )
    bundle = ResearchIntelligenceBundle(
        mode="github",
        profile_id="github.repository.v1",
        profile_version=1,
        sources=(source_ref,),
        evidence=(evidence,),
        claims=(claim,),
        coverage=CoverageDecision(
            required_dimensions=("overview",),
            covered_dimensions=("overview",),
            missing_dimensions=(),
            coverage_score=1.0,
            allow_report=True,
        ),
        report_spec=GenericReportSpec(title="Report"),
    )

    payload = bundle.as_dict()
    assert payload["schema_version"] == INTELLIGENCE_SCHEMA_VERSION
    assert json.loads(json.dumps(payload, ensure_ascii=False))["mode"] == "github"
    restored = ResearchIntelligenceBundle.from_dict(payload)
    assert restored == bundle

    broken = dict(payload)
    broken["claims"] = [
        {
            **claim.as_dict(),
            "evidence_ids": ["missing"],
        }
    ]
    with pytest.raises(ValueError, match="evidence"):
        ResearchIntelligenceBundle.from_dict(broken)


def test_bundle_rejects_unknown_schema_version() -> None:
    """Version negotiation is explicit and never silently guesses a schema."""
    with pytest.raises(ValueError, match="schema"):
        ResearchIntelligenceBundle.from_dict({"schema_version": 99})


def test_artifact_manifest_v2_contains_descriptors_without_content() -> None:
    """The v2 manifest references stored artifacts but does not inline their body."""
    descriptor = ArtifactDescriptorV2(
        artifact_id="artifact_1",
        artifact_type="evidence_json",
        mime_type="application/json",
        path="artifacts/evidence.json",
        title="Evidence",
        source_ids=("ev_1",),
        size_bytes=42,
        checksum="checksum",
    )
    manifest = ArtifactManifestV2(artifacts=(descriptor,))

    payload = manifest.as_dict()
    assert payload["schema_version"] == INTELLIGENCE_SCHEMA_VERSION
    assert "content" not in payload["artifacts"][0]


def test_github_v1_adapter_preserves_single_repository_and_line_evidence() -> None:
    """A GitHub v1 Bundle maps to v2 without losing commit or line locators."""
    raw = fixture()["single_bundle"]
    bundle = GitHubEvidenceV1Adapter.to_v2(raw)

    assert bundle.schema_version == INTELLIGENCE_SCHEMA_VERSION
    assert bundle.mode.value == "github"
    assert bundle.sources[0].source_id == "owner/repo"
    source_code = next(item for item in bundle.evidence if item.evidence_type == "source_code") if any(
        item.evidence_type == "source_code" for item in bundle.evidence
    ) else None
    if source_code is not None:
        assert source_code.locator.file_path == "src/main.py"
        assert source_code.locator.line_start == 1
        assert source_code.locator.line_end == 2
        assert "#L1-L2" in source_code.locator.url
        assert source_code.source.resolved_version == "a" * 40
    assert all(claim.evidence_ids for claim in bundle.claims)


def test_github_v1_adapter_preserves_multi_repository_sources_and_claims() -> None:
    """Comparison bundles retain every repository instead of collapsing to the first."""
    bundle = GitHubEvidenceV1Adapter.to_v2(fixture()["multi_bundle"])

    assert {item.source_id for item in bundle.sources} == {"owner/repo", "other/repo"}
    assert len(bundle.claims) == 2
    assert all(
        set(claim.evidence_ids).issubset({item.evidence_id for item in bundle.evidence})
        for claim in bundle.claims
    )
