"""Tests for the versioned GitHub evidence intelligence layer."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from agent import DeepResearchAgent
from config import Configuration
from models import ResearchState
from research.artifacts import FileArtifactStore
from research.claim_verifier import FactualVerification, SupportSpan
from research.compatibility import GitHubEvidenceV1Adapter
from research.contracts import ResearchCommand
from research.evidence import (
    build_github_evidence_bundle,
    canonicalize_github_report,
    freeze_github_evidence,
    github_evidence_bundle_from_dict,
    render_github_artifacts,
    supplement_github_evidence,
)
from research.report_document import StructuredSummaryDocument, SummaryParagraph
from research.session import RunSession
from research.summary_quality import SummaryQualityGateV1
from services.github_research import GitHubRepositoryTarget


def make_context(
    repository: str = "owner/repo",
    *,
    with_activity: bool = True,
) -> SimpleNamespace:
    """Build a small deterministic context without making HTTP requests."""
    owner, repo = repository.split("/", 1)
    target = GitHubRepositoryTarget(owner=owner, repo=repo)
    return SimpleNamespace(
        target=target,
        repository={"full_name": repository, "license": "MIT", "default_branch": "main"},
        commit_sha="a" * 40,
        readme_excerpt="# Project",
        tree_excerpt="src/main.py",
        file_manifest=[{"path": "src/main.py", "type": "blob"}],
        file_contents=[
            {
                "path": "src/main.py",
                "sha": "a" * 40,
                "content": "def main():\n    return 1\n",
            }
        ],
        languages={"Python": 10},
        contributors=[{"login": "alice"}],
        commits=[
            {"sha": "a" * 40, "message": "initial", "url": f"https://github.com/{repository}/commit/{'a' * 40}"
            }
        ]
        if with_activity
        else [],
        issues=[{"number": 1, "title": "Issue", "url": f"https://github.com/{repository}/issues/1"}]
        if with_activity
        else [],
        pull_requests=[],
        releases=[{"tag": "v1.0.0", "url": f"https://github.com/{repository}/releases/tag/v1.0.0"}]
        if with_activity
        else [],
        notices=[],
    )


def test_bundle_pins_source_file_evidence_to_commit_sha() -> None:
    """Source-file evidence must use a commit permalink, not a branch URL."""
    bundle = build_github_evidence_bundle([make_context()])

    source = next(item for item in bundle.evidence if item.evidence_type == "source_file")
    assert source.commit_sha == "a" * 40
    assert f"/blob/{'a' * 40}/src/main.py" in source.source_url
    assert all(
        claim.evidence_ids and set(claim.evidence_ids).issubset({item.evidence_id for item in bundle.evidence})
        for claim in bundle.claims
    )


def test_bundle_contains_line_addressable_source_code_evidence() -> None:
    """Fetched source excerpts become commit-pinned line-range evidence."""
    bundle = build_github_evidence_bundle([make_context()])

    source = next(item for item in bundle.evidence if item.evidence_type == "source_code")
    assert source.file_path == "src/main.py"
    assert source.line_start == 1
    assert source.line_end == 2
    assert "1 | def main()" in source.excerpt
    assert f"/blob/{'a' * 40}/src/main.py" in source.source_url

    restored = github_evidence_bundle_from_dict(bundle.as_dict())
    assert restored is not None
    restored_source = next(
        item for item in restored.evidence if item.evidence_type == "source_code"
    )
    assert (restored_source.line_start, restored_source.line_end) == (1, 2)


def test_gap_enrichment_is_bounded_to_one_pass_and_freeze_blocks_more() -> None:
    """Coverage retry count is monotonic and frozen evidence cannot be changed."""
    bundle = build_github_evidence_bundle([make_context(with_activity=False)])
    assert bundle.coverage.retry_count == 0
    enriched = supplement_github_evidence(
        bundle,
        ["https://github.com/another/project/issues/2"],
    )
    assert enriched.coverage.retry_count == 1
    again = supplement_github_evidence(enriched, ["https://github.com/third/project"])
    assert again == enriched
    frozen = freeze_github_evidence(enriched)
    assert frozen.evidence_frozen is True
    assert supplement_github_evidence(frozen, ["https://github.com/fourth/project"]) == frozen


def test_artifact_manifest_round_trips_and_supports_comparison() -> None:
    """Two repositories produce comparison claims and deterministic artifact types."""
    bundle = build_github_evidence_bundle(
        [make_context(), make_context("other/repo")]
    )
    rendered = render_github_artifacts(bundle, report_markdown="# Report")
    assert any(claim.category == "comparison" for claim in rendered.claims)
    assert {artifact.artifact_type for artifact in rendered.artifacts} == {
        "report_markdown",
        "evidence_json",
        "report_html",
        "chart_svg",
        "mermaid",
    }
    restored = github_evidence_bundle_from_dict(rendered.as_dict())
    assert restored is not None
    assert len(restored.artifacts) == 5
    assert restored.artifacts[0].checksum == rendered.artifacts[0].checksum


def test_every_mapped_github_artifact_descriptor_has_external_body(tmp_path) -> None:
    """The v2 manifest must never advertise an inline v1 artifact that was not stored."""
    rendered = render_github_artifacts(
        build_github_evidence_bundle([make_context()]),
        report_markdown="# Repository report",
    )
    generic = GitHubEvidenceV1Adapter.to_v2(rendered)
    store = FileArtifactStore(tmp_path)
    config = Configuration.from_env(overrides={"enable_notes": False})
    command = ResearchCommand(topic="owner/repo", config=config)
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=command.topic),
    )
    session.start()
    agent = DeepResearchAgent(
        config=config,
        planner=object(),
        summarizer=object(),
        reporting=object(),
        note_agent=None,
        github_adapter=None,
        artifact_store=store,
    )

    persisted = agent._persist_legacy_github_artifacts(
        session,
        bundle=generic,
        legacy_bundle=rendered,
    )

    assert len(persisted.artifact_manifest.artifacts) == len(rendered.artifacts)
    for descriptor in persisted.artifact_manifest.artifacts:
        body = store.get(session.run_id, descriptor.artifact_id)
        assert body
        assert len(body) == descriptor.size_bytes
        assert hashlib.sha256(body).hexdigest() == descriptor.checksum


def test_real_single_repository_primary_claim_can_pass_default_quality_gate() -> None:
    """A real adapter bundle can retain precise code findings after quality gating."""
    generic = GitHubEvidenceV1Adapter.to_v2(
        freeze_github_evidence(build_github_evidence_bundle([make_context()]))
    )
    claim = next(item for item in generic.claims if item.dimension == "architecture")
    evidence_by_id = {item.evidence_id: item for item in generic.evidence}
    selected = next(
        evidence_by_id[item]
        for item in claim.evidence_ids
        if evidence_by_id[item].evidence_level == "full_text"
        and evidence_by_id[item].locator.line_start is not None
    )

    class SemanticScorer:
        name = "test-semantic"
        version = "1"

        @staticmethod
        def score(claim, evidence) -> float:
            del claim, evidence
            return 1.0

    class PrimaryEvidenceVerifier:
        name = "test-factual"
        version = "1"
        prompt_version = "1"

        @staticmethod
        def verify(claim, evidence) -> FactualVerification:
            del claim
            record = next(item for item in evidence if item.evidence_id == selected.evidence_id)
            return FactualVerification(
                verdict="supported",
                factual_score=1.0,
                supporting_evidence_ids=(record.evidence_id,),
                support_spans=(
                    SupportSpan(
                        evidence_id=record.evidence_id,
                        exact_text=record.excerpt,
                    ),
                ),
            )

    document = StructuredSummaryDocument(
        task_id="report",
        paragraphs=(
            SummaryParagraph(
                section_id="architecture",
                paragraph_type="factual",
                text=claim.statement,
                claim_ids=(claim.claim_id,),
                citation_ids=(selected.evidence_id,),
            ),
        ),
        claim_ids=(claim.claim_id,),
    )
    assessment = SummaryQualityGateV1(
        semantic_scorer=SemanticScorer(),
        factual_verifier=PrimaryEvidenceVerifier(),
    ).evaluate(document, generic)

    assert assessment.passed is True
    assert assessment.overall_score == 0.79
    assert assessment.paragraph_assessments[0].blockers == ()


def test_report_urls_and_citations_are_derived_from_evidence() -> None:
    """Unlinked GitHub URLs are removed and the citation block is deterministic."""
    bundle = build_github_evidence_bundle([make_context()])
    report = "# Report\n\n" + ("Evidence text. " * 20)
    report += "\nSee https://github.com/not-in-evidence/project."
    canonical = canonicalize_github_report(report, bundle)
    assert "not-in-evidence" not in canonical
    assert "## 参考证据" in canonical
    assert next(iter(bundle.evidence)).evidence_id in canonical
