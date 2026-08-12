"""Versioned research modes, task profiles, and profile registry contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Iterable, Sequence

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ResearchMode(str, Enum):
    """Top-level research families supported by the shared runtime."""

    WEB = "web"
    GITHUB = "github"
    PAPER = "paper"


def _normalize_mode(value: ResearchMode | str) -> ResearchMode:
    """Normalize a mode supplied by Python callers or a JSON boundary."""
    if isinstance(value, ResearchMode):
        return value
    try:
        return ResearchMode(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Unsupported research mode: {value!r}.") from exc


def _validate_identifier(value: str, *, field_name: str) -> str:
    """Validate one bounded registry or dimension identifier."""
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value.strip()) is None:
        raise ValueError(f"{field_name} must be a lowercase registry identifier.")
    return value.strip()


@dataclass(frozen=True, kw_only=True)
class ResearchDimension:
    """One reportable dimension covered by a research profile."""

    id: str
    title: str
    required: bool = True
    weight: float = 1.0

    def __post_init__(self) -> None:
        """Validate dimension identity and deterministic coverage weight."""
        object.__setattr__(
            self,
            "id",
            _validate_identifier(self.id, field_name="Dimension ID"),
        )
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("Dimension title must not be empty.")
        if not isinstance(self.required, bool):
            raise TypeError("Dimension required flag must be boolean.")
        if (
            isinstance(self.weight, bool)
            or not isinstance(self.weight, (int, float))
            or not isfinite(float(self.weight))
            or float(self.weight) <= 0
        ):
            raise ValueError("Dimension weight must be a positive finite number.")
        object.__setattr__(self, "weight", float(self.weight))


@dataclass(frozen=True, kw_only=True)
class ResearchTaskTemplate:
    """A profile-owned task template rendered into the existing TODO model."""

    template_id: str
    dimension: str
    title: str
    intent: str
    query_template: str
    source_strategy: str
    supported_modes: frozenset[ResearchMode] = field(
        default_factory=lambda: frozenset(ResearchMode)
    )
    comparison_only: bool = False

    def __post_init__(self) -> None:
        """Validate template identity, text, and mode declarations."""
        object.__setattr__(
            self,
            "template_id",
            _validate_identifier(self.template_id, field_name="Task template ID"),
        )
        object.__setattr__(
            self,
            "dimension",
            _validate_identifier(self.dimension, field_name="Task dimension"),
        )
        for field_name in ("title", "intent", "query_template", "source_strategy"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Task template {field_name} must not be empty.")
        if not isinstance(self.comparison_only, bool):
            raise TypeError("Task template comparison_only flag must be boolean.")
        normalized_modes = frozenset(_normalize_mode(mode) for mode in self.supported_modes)
        if not normalized_modes:
            raise ValueError("Task template must support at least one research mode.")
        object.__setattr__(self, "supported_modes", normalized_modes)


@dataclass(frozen=True, kw_only=True)
class RenderedResearchTask:
    """A concrete task produced from one profile template."""

    id: int
    template_id: str
    dimension: str
    title: str
    intent: str
    query: str
    source_strategy: str
    repository: str | None = None


@dataclass(frozen=True, kw_only=True)
class RetrievalBudget:
    """Bounded retrieval allowances owned by one research profile."""

    max_requests: int = 60
    max_results: int = 100
    max_tasks: int = 8
    max_evidence: int = 180
    max_enrich_passes: int = 1
    max_excerpt_chars: int = 1200

    def __post_init__(self) -> None:
        """Reject zero, negative, boolean, or otherwise unusable limits."""
        for field_name in (
            "max_requests",
            "max_results",
            "max_tasks",
            "max_evidence",
            "max_excerpt_chars",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"Retrieval budget {field_name} must be positive.")
        if (
            not isinstance(self.max_enrich_passes, int)
            or isinstance(self.max_enrich_passes, bool)
            or self.max_enrich_passes < 0
        ):
            raise ValueError("Retrieval budget max_enrich_passes must be non-negative.")


@dataclass(frozen=True, kw_only=True)
class CoveragePolicy:
    """Deterministic coverage thresholds for one profile."""

    required_dimensions: tuple[str, ...] = ()
    min_coverage_score: float = 1.0
    min_evidence_per_dimension: int = 1
    min_independent_sources: int = 1

    def __post_init__(self) -> None:
        """Validate dimension references and bounded quality thresholds."""
        dimensions = tuple(
            _validate_identifier(item, field_name="Coverage dimension")
            for item in self.required_dimensions
        )
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("Coverage dimensions must be unique.")
        object.__setattr__(self, "required_dimensions", dimensions)
        if (
            isinstance(self.min_coverage_score, bool)
            or not isinstance(self.min_coverage_score, (int, float))
            or not isfinite(float(self.min_coverage_score))
            or not 0 <= float(self.min_coverage_score) <= 1
        ):
            raise ValueError("Coverage score threshold must be between zero and one.")
        object.__setattr__(self, "min_coverage_score", float(self.min_coverage_score))
        for field_name in ("min_evidence_per_dimension", "min_independent_sources"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"Coverage policy {field_name} must be positive.")


@dataclass(frozen=True, kw_only=True)
class CitationPolicy:
    """Report citation rules declared by a profile."""

    require_evidence_ids: bool = True
    allow_external_urls: bool = False
    require_locator: bool = False

    def __post_init__(self) -> None:
        """Validate citation policy flags at profile construction time."""
        for field_name in (
            "require_evidence_ids",
            "allow_external_urls",
            "require_locator",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"Citation policy {field_name} must be boolean.")


@dataclass(frozen=True, kw_only=True)
class ReportSectionSpec:
    """One deterministic section declaration for a profile report."""

    id: str
    title: str
    dimension: str
    required: bool = True

    def __post_init__(self) -> None:
        """Validate report section identifiers and labels."""
        object.__setattr__(
            self,
            "id",
            _validate_identifier(self.id, field_name="Report section ID"),
        )
        object.__setattr__(
            self,
            "dimension",
            _validate_identifier(self.dimension, field_name="Report section dimension"),
        )
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("Report section title must not be empty.")
        if not isinstance(self.required, bool):
            raise TypeError("Report section required flag must be boolean.")


@dataclass(frozen=True, kw_only=True)
class ResearchProfile:
    """Immutable, versioned policy for one research mode."""

    profile_id: str
    version: int
    mode: ResearchMode
    dimensions: tuple[ResearchDimension, ...]
    task_templates: tuple[ResearchTaskTemplate, ...]
    source_priority: tuple[str, ...]
    retrieval_budget: RetrievalBudget = field(default_factory=RetrievalBudget)
    coverage_policy: CoveragePolicy = field(default_factory=CoveragePolicy)
    citation_policy: CitationPolicy = field(default_factory=CitationPolicy)
    report_sections: tuple[ReportSectionSpec, ...] = ()

    def __post_init__(self) -> None:
        """Validate all cross-references before a profile enters a registry."""
        if _PROFILE_ID_RE.fullmatch(self.profile_id.strip()) is None:
            raise ValueError("Profile ID must be a lowercase versioned identifier.")
        object.__setattr__(self, "profile_id", self.profile_id.strip())
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ValueError("Profile version must be a positive integer.")
        normalized_mode = _normalize_mode(self.mode)
        object.__setattr__(self, "mode", normalized_mode)
        dimension_ids = tuple(item.id for item in self.dimensions)
        if len(dimension_ids) != len(set(dimension_ids)):
            raise ValueError("Profile dimensions must be unique.")
        known_dimensions = set(dimension_ids)
        for required in self.coverage_policy.required_dimensions:
            if required not in known_dimensions:
                raise ValueError("Coverage policy references an unknown dimension.")
        template_ids = tuple(item.template_id for item in self.task_templates)
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("Task template IDs must be unique.")
        for template in self.task_templates:
            if template.dimension not in known_dimensions:
                raise ValueError("Task template references an unknown dimension.")
            if normalized_mode not in template.supported_modes:
                raise ValueError("Task template mode does not match profile mode.")
        priorities = tuple(item.strip() for item in self.source_priority)
        if not priorities or any(not item for item in priorities):
            raise ValueError("Profile source priority must not be empty.")
        if len(priorities) != len(set(priorities)):
            raise ValueError("Profile source priority must be unique.")
        object.__setattr__(self, "source_priority", priorities)
        section_ids = tuple(item.id for item in self.report_sections)
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("Report section IDs must be unique.")
        for section in self.report_sections:
            if section.dimension not in known_dimensions:
                raise ValueError("Report section references an unknown dimension.")

    def render_tasks(
        self,
        *,
        repository: str | None = None,
        comparison_repositories: Sequence[str] = (),
    ) -> list[RenderedResearchTask]:
        """Render bounded profile templates for one target or comparison."""
        primary = repository.strip() if isinstance(repository, str) else None
        comparisons = tuple(
            item.strip()
            for item in comparison_repositories
            if isinstance(item, str) and item.strip()
        )
        repositories = ", ".join(item for item in ((primary,) + comparisons) if item)
        rendered: list[RenderedResearchTask] = []
        for template in self.task_templates:
            if template.comparison_only and not comparisons:
                continue
            try:
                query = template.query_template.format(
                    repository=primary or "",
                    repositories=repositories,
                )
            except (KeyError, IndexError, ValueError) as exc:
                raise ValueError(
                    f"Task template {template.template_id} has an invalid query template."
                ) from exc
            task_repository = repositories if template.comparison_only else primary
            rendered.append(
                RenderedResearchTask(
                    id=len(rendered) + 1,
                    template_id=template.template_id,
                    dimension=template.dimension,
                    title=template.title,
                    intent=template.intent,
                    query=query,
                    source_strategy=template.source_strategy,
                    repository=task_repository,
                )
            )
        if len(rendered) > self.retrieval_budget.max_tasks:
            raise ValueError("Rendered profile tasks exceed the retrieval budget.")
        return rendered


class ResearchProfileRegistry:
    """Process-local registry for immutable, explicitly approved profiles."""

    def __init__(self, profiles: Iterable[ResearchProfile] = ()) -> None:
        """Register an optional initial set without allowing replacements."""
        self._profiles: dict[str, ResearchProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: ResearchProfile) -> None:
        """Add one profile and reject duplicate IDs."""
        if not isinstance(profile, ResearchProfile):
            raise TypeError("Only ResearchProfile instances can be registered.")
        if profile.profile_id in self._profiles:
            raise ValueError(f"Profile {profile.profile_id!r} is already registered.")
        self._profiles[profile.profile_id] = profile

    def get(self, profile_id: str) -> ResearchProfile:
        """Return one registered profile or raise a stable lookup error."""
        try:
            return self._profiles[profile_id]
        except (KeyError, TypeError) as exc:
            raise KeyError(f"Unknown research profile: {profile_id!r}.") from exc

    def resolve(
        self,
        *,
        profile_id: str | None = None,
        mode: ResearchMode | str | None = None,
    ) -> ResearchProfile:
        """Resolve an explicit profile, mode default, or the Web default."""
        normalized_mode = _normalize_mode(mode) if mode is not None else None
        if profile_id is not None:
            profile = self.get(profile_id)
            if normalized_mode is not None and profile.mode is not normalized_mode:
                raise ValueError("Research profile mode does not match research mode.")
            return profile
        if normalized_mode is None:
            return self.get("web.default.v1")
        matches = tuple(
            profile
            for profile in self._profiles.values()
            if profile.mode is normalized_mode
        )
        if not matches:
            raise KeyError(f"No profile is registered for mode {normalized_mode.value!r}.")
        return matches[0]

    def all(self) -> tuple[ResearchProfile, ...]:
        """Return profiles in deterministic registration order."""
        return tuple(self._profiles.values())


def _github_profile() -> ResearchProfile:
    """Build the first versioned GitHub repository profile."""
    dimensions = (
        ResearchDimension(id="overview", title="仓库概览与定位"),
        ResearchDimension(id="architecture", title="架构与代码结构"),
        ResearchDimension(id="maintenance", title="演进时间线与路线图"),
        ResearchDimension(id="community", title="社区评价与替代方案"),
        ResearchDimension(id="license", title="许可证"),
        ResearchDimension(id="comparison", title="多仓库统一维度对比", required=False),
    )
    github_modes = frozenset({ResearchMode.GITHUB})
    templates = (
        ResearchTaskTemplate(
            template_id="repository_overview",
            dimension="overview",
            title="仓库概览与定位",
            intent="梳理项目用途、核心能力、许可证、语言栈、README 中的主要承诺与当前活跃度。",
            query_template="{repository} GitHub repository overview README features",
            source_strategy="github_api_then_web",
            supported_modes=github_modes,
        ),
        ResearchTaskTemplate(
            template_id="architecture",
            dimension="architecture",
            title="架构与代码结构",
            intent="结合目录树和文档分析项目的主要模块、运行方式、扩展点与技术边界。",
            query_template="{repository} architecture directory structure modules",
            source_strategy="github_api_then_web",
            supported_modes=github_modes,
        ),
        ResearchTaskTemplate(
            template_id="maintenance",
            dimension="maintenance",
            title="演进时间线与路线图",
            intent="根据 commits、releases、issues 和 PRs 梳理近期变化、维护节奏、待解决问题和路线图信号。",
            query_template="{repository} commits releases issues roadmap",
            source_strategy="github_api_then_web",
            supported_modes=github_modes,
        ),
        ResearchTaskTemplate(
            template_id="community",
            dimension="community",
            title="社区评价与替代方案",
            intent="补充外部文章、社区讨论和竞品信息，评估采用价值、风险与适用场景。",
            query_template="{repository} community adoption alternatives comparison",
            source_strategy="github_api_then_web",
            supported_modes=github_modes,
        ),
        ResearchTaskTemplate(
            template_id="comparison",
            dimension="comparison",
            title="多仓库统一维度对比",
            intent="使用统一维度比较候选仓库的架构、维护状态、扩展性和适用场景。",
            query_template="{repositories} compare architecture maintenance extensibility risk",
            source_strategy="github_api_compare",
            supported_modes=github_modes,
            comparison_only=True,
        ),
    )
    return ResearchProfile(
        profile_id="github.repository.v1",
        version=1,
        mode=ResearchMode.GITHUB,
        dimensions=dimensions,
        task_templates=templates,
        source_priority=("github", "web"),
        retrieval_budget=RetrievalBudget(max_tasks=5),
        coverage_policy=CoveragePolicy(
            required_dimensions=("overview", "architecture", "maintenance", "community", "license"),
            min_coverage_score=0.8,
        ),
        citation_policy=CitationPolicy(require_locator=True),
        report_sections=tuple(
            ReportSectionSpec(id=item.id, title=item.title, dimension=item.id)
            for item in dimensions
            if item.required
        ),
    )


def _web_profile() -> ResearchProfile:
    """Build the compatibility profile for existing planner-driven Web research."""
    return ResearchProfile(
        profile_id="web.default.v1",
        version=1,
        mode=ResearchMode.WEB,
        dimensions=(ResearchDimension(id="overview", title="研究概览"),),
        task_templates=(),
        source_priority=("web",),
        retrieval_budget=RetrievalBudget(),
        coverage_policy=CoveragePolicy(required_dimensions=()),
        report_sections=(ReportSectionSpec(id="overview", title="研究概览", dimension="overview"),),
    )


def _paper_profile() -> ResearchProfile:
    """Build the contract-only profile reserved for the future Paper Provider."""
    return ResearchProfile(
        profile_id="paper.abstract.v1",
        version=1,
        mode=ResearchMode.PAPER,
        dimensions=(
            ResearchDimension(id="relevance", title="相关性"),
            ResearchDimension(id="method", title="研究方法"),
            ResearchDimension(id="findings", title="主要发现"),
            ResearchDimension(id="limitations", title="局限性"),
        ),
        task_templates=(),
        source_priority=("openalex", "crossref"),
        retrieval_budget=RetrievalBudget(max_requests=40, max_results=50, max_tasks=6),
        coverage_policy=CoveragePolicy(
            required_dimensions=("relevance", "method", "findings", "limitations"),
            min_coverage_score=0.75,
        ),
        citation_policy=CitationPolicy(require_locator=False),
        report_sections=tuple(
            ReportSectionSpec(id=item.id, title=item.title, dimension=item.id)
            for item in (
                ResearchDimension(id="relevance", title="相关性"),
                ResearchDimension(id="method", title="研究方法"),
                ResearchDimension(id="findings", title="主要发现"),
                ResearchDimension(id="limitations", title="局限性"),
            )
        ),
    )


def built_in_profile_registry() -> ResearchProfileRegistry:
    """Return a fresh registry containing only approved built-in profiles."""
    return ResearchProfileRegistry((_web_profile(), _github_profile(), _paper_profile()))


__all__ = [
    "CitationPolicy",
    "CoveragePolicy",
    "RenderedResearchTask",
    "ReportSectionSpec",
    "ResearchDimension",
    "ResearchMode",
    "ResearchProfile",
    "ResearchProfileRegistry",
    "ResearchTaskTemplate",
    "RetrievalBudget",
    "built_in_profile_registry",
]
