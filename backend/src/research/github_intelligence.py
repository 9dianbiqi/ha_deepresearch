"""Deterministic analysis modules for GitHub evidence intelligence."""

from __future__ import annotations

from collections.abc import Sequence

from .evidence import EvidenceItem, RepositorySnapshot, ResearchClaim, _stable_id


def _claim(
    snapshot: RepositorySnapshot,
    evidence: Sequence[EvidenceItem],
    *,
    category: str,
    statement: str,
    evidence_types: tuple[str, ...],
    confidence: str,
) -> ResearchClaim | None:
    """Create a claim only when the requested evidence exists."""
    evidence_ids = tuple(
        item.evidence_id
        for item in evidence
        if item.snapshot_id == snapshot.snapshot_id
        and item.evidence_type in evidence_types
    )
    if not evidence_ids:
        return None
    limitation = (
        "结论基于固定快照和可访问的 GitHub 公开数据。"
        if snapshot.collection_status != "complete"
        else "结论基于固定 commit 快照。"
    )
    return ResearchClaim(
        claim_id=_stable_id("claim", snapshot.snapshot_id, category),
        category=category,
        statement=statement,
        confidence=confidence,
        evidence_ids=evidence_ids[:8],
        limitations=(limitation,),
    )


def analyze_overview(
    snapshot: RepositorySnapshot,
    evidence: Sequence[EvidenceItem],
) -> ResearchClaim | None:
    """Analyze project identity, README, and repository metadata."""
    return _claim(
        snapshot,
        evidence,
        category="overview",
        statement=f"{snapshot.repository} 的公开仓库元数据和 README 已固定到研究快照。",
        evidence_types=("repository_metadata", "readme"),
        confidence="medium",
    )


def analyze_architecture(
    snapshot: RepositorySnapshot,
    evidence: Sequence[EvidenceItem],
) -> ResearchClaim | None:
    """Analyze the source tree and file manifest without executing code."""
    return _claim(
        snapshot,
        evidence,
        category="architecture",
        statement=f"{snapshot.repository} 的目录树和关键文件清单可用于初步架构分析。",
        evidence_types=("repository_tree", "source_file"),
        confidence="medium",
    )


def analyze_maintenance(
    snapshot: RepositorySnapshot,
    evidence: Sequence[EvidenceItem],
) -> ResearchClaim | None:
    """Analyze commit and release cadence signals."""
    return _claim(
        snapshot,
        evidence,
        category="maintenance",
        statement=f"{snapshot.repository} 的提交与发布活动可用于评估维护节奏。",
        evidence_types=("commit", "release"),
        confidence="medium",
    )


def analyze_issue_pr_intelligence(
    snapshot: RepositorySnapshot,
    evidence: Sequence[EvidenceItem],
) -> ResearchClaim | None:
    """Analyze issue and pull-request signals for community risk."""
    return _claim(
        snapshot,
        evidence,
        category="community",
        statement=f"{snapshot.repository} 的 Issue/PR 活动可用于识别社区信号与维护风险。",
        evidence_types=("issue", "pull_request", "external_web"),
        confidence=(
            "medium"
            if any(
                item.snapshot_id == snapshot.snapshot_id
                and item.evidence_type in {"issue", "pull_request"}
                for item in evidence
            )
            else "low"
        ),
    )


def analyze_license(
    snapshot: RepositorySnapshot,
    evidence: Sequence[EvidenceItem],
) -> ResearchClaim | None:
    """Analyze the license field exposed by repository metadata."""
    return _claim(
        snapshot,
        evidence,
        category="license",
        statement=f"{snapshot.repository} 的许可证信息来自 GitHub 仓库元数据。",
        evidence_types=("repository_metadata",),
        confidence="high" if snapshot.metadata.get("license") else "low",
    )


def analyze_repository(
    snapshot: RepositorySnapshot,
    evidence: Sequence[EvidenceItem],
) -> list[ResearchClaim]:
    """Run the bounded overview, architecture, maintenance, issue/PR, and license analyzers."""
    claims: list[ResearchClaim] = []
    for analyzer in (
        analyze_overview,
        analyze_architecture,
        analyze_maintenance,
        analyze_issue_pr_intelligence,
        analyze_license,
    ):
        claim = analyzer(snapshot, evidence)
        if claim is not None:
            claims.append(claim)
    return claims


__all__ = [
    "analyze_architecture",
    "analyze_issue_pr_intelligence",
    "analyze_license",
    "analyze_maintenance",
    "analyze_overview",
    "analyze_repository",
]
