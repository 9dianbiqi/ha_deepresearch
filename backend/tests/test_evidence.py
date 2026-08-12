"""Tests for the versioned GitHub evidence intelligence layer."""

from __future__ import annotations

from types import SimpleNamespace

from research.evidence import (
    build_github_evidence_bundle,
    canonicalize_github_report,
    freeze_github_evidence,
    github_evidence_bundle_from_dict,
    render_github_artifacts,
    supplement_github_evidence,
)
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


def test_report_urls_and_citations_are_derived_from_evidence() -> None:
    """Unlinked GitHub URLs are removed and the citation block is deterministic."""
    bundle = build_github_evidence_bundle([make_context()])
    report = "# Report\n\n" + ("Evidence text. " * 20)
    report += "\nSee https://github.com/not-in-evidence/project."
    canonical = canonicalize_github_report(report, bundle)
    assert "not-in-evidence" not in canonical
    assert "## 参考证据" in canonical
    assert next(iter(bundle.evidence)).evidence_id in canonical
