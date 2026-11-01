# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- **Lightweight offline Semantic Gate** checking only explicit coverage, semantic redundancy, and relevance with one model, validated fault references, actionable feedback and non-blocking uncertain/technical outcomes. Twelve obvious development fixtures replace the earlier broad calibration scope for this first version.
- **Component-and-quote review calibration** with frozen coverage components, exact task-field citation checks, explicit semantic-verification limits, and original/positive-candidate model observations. Uncertain or conflicting cases stay outside primary metrics.
- **Distinct-model feedback and blinded API judge protocol** with explicit role configuration, actual model ID separation checks, stateless anonymous judge payloads, exhaustive task/dimension/pair annotations, and cross-provider credential isolation. Legacy same-chat feedback reviews remain exploratory.
- **Frozen-prompt semantic feedback experiment** with matched old-plan context, independent histories, safe provider-failure records, and exact-condition retry. One GLM-4.7-Flash control/feedback pair improved AI-assessed coverage; failed repetitions remain explicitly unscored.
- **Explicit uncertain annotations and Core/Boundary isolation** for all planning review axes, three-way pair relations, boundary-safe agreement and adjudication, and separate uncertainty ratios. GLM-4.7-Flash interface and planner sampling were exercised; sampler thinking/output budgets are recorded explicitly.
- **Two-reviewer planner annotation workflow** with four-axis agreement counts, explicit undefined kappa cases, disagreement lists and human adjudication audit trails. The six-plan review remains an AI draft, separate from human references.
- **Planner sampling and review workflow** using the production planner, bounded feedback retries, usage observations, blank review packets, annotation import validation, and human-labeled paired comparisons. Provider failures stay separate from semantic plan quality.
- **Offline planner evaluation seed corpus** with 8 topic rubrics, 48 annotated synthetic candidates, topic-level development/holdout splits, and reproducible Plan Gate baselines. Labels are AI drafts pending human review; no runtime semantic judge is enabled. See [planner evaluation](docs/PLANNER_EVALUATION.md).
- **Deterministic planner plan gate** with a strict fail-open plan schema boundary, near-verbatim duplicate task repair, lexical topic-coverage re-plan with bounded feedback, and query-complexity task sizing. Observations are recorded in `metrics.plan_quality`. See [planner plan gate](docs/PLAN_QUALITY_GATE.md).
- **GitHub line-addressable evidence** with bounded commit-pinned source file excerpts, deterministic line ranges, citation sanitization, and Evidence Drawer links.
- **GitHub evidence acceptance record** covering authenticated collection, rate-limit degradation, artifact round-trip, and SSE compatibility.
- **Production single-instance MVP closure** with fail-closed Bearer authentication, session-scoped frontend key entry, shared non-blocking run admission, non-root backend images, internal-only backend networking, and persistent Compose operations. See [production deployment](docs/PRODUCTION_MVP.md) and the [MVP acceptance checklist](docs/acceptance/MVP_PRODUCTION_CHECKLIST.md).

### Changed
- Planned tasks that exceed a profile's retrieval budget now trim to the budget and continue, instead of aborting the run. The budget outcome and the `plan_budget_exceeded` reason code are recorded in `metrics.plan_quality`.

## [1.1.0] — 2026-08-12

### Added
- **Durable schema-v2 artifact downloads** through `GET /runs/{run_id}/artifacts/{artifact_id}`, with Run ownership checks, MIME types, download names, stable `artifact_not_found` responses, and restart persistence coverage.
- **Single-instance production protection** with Bearer `APP_API_KEY` authentication, public `/healthz` and `/readyz`, bounded concurrent runs, request-size limits, and secret-safe validation/logging.
- **Unified `DATA_DIR` operations** with retention-aware cleanup, path safety checks, backup/restore guidance, Dockerfiles, Compose volume persistence, and CI verification.

### Changed
- Backend and frontend versions are now `1.1.0`.
- The frontend downloads artifact bytes from the durable API instead of relying on inline artifact content.

### Not included
- Paper retrieval and research-provider expansion remain out of scope for v1.1.

## [1.0.0] — 2026-08-11

### Added
- **Unified research runtime** with one canonical `ResearchApplicationService` / `RunSession` lifecycle for synchronous, Harness, and SSE execution.
- **Durable run lifecycle and recovery** with atomic, validated checkpoints, continuation anchors, explicit parent error classes, and real process crash acceptance coverage.
- **Report integrity validation** for empty, truncated, underspecified, and citation-free reports, with one bounded retry and `report_incomplete` terminal state.
- **Runtime telemetry** for LLM usage, finish reasons, stream boundaries, SSE transport timing, and execution/recovery attempt identity.
- **User-controlled history and memory** with bounded recall and explicit confirmation for memory writes.
- **Formal MIT license** and release acceptance records.

### Changed
- Completed runs now require completed research, a generated report, and a passing deterministic report validation.
- Failed, cancelled, rejected, and interrupted runs are persisted; only validated and resumable checkpoints may be recovered.
- Safe operations may replay after a crash, while unknown or side-effecting outcomes fail closed.
- Follow-up continuation keeps the last valid anchor when a newer child run fails.

### Not included
- Continual learning, Hermes-style experience learning, and autonomous self-training remain future work and are not part of the v1.0 contract.

## [0.1.0] — 2026-06-04

### Added
- **Multi-turn research** via `/research/continue/stream` endpoint with `parent_run_id`
- **Compressed context** injection (`reasoning_memory`) for follow-up research continuity
- **Harness governance** layer (Policy → Context Compression → Evaluation → Persistence)
- **Harness endpoints**: `/harness/run`, `/harness/runs/{run_id}`, `/harness/scenarios`
- **Search retry + backend fallback**: automatic retry with exponential backoff, failover to DuckDuckGo when primary search backend is unavailable
- **Reporter error handling**: graceful degradation when LLM call fails, with `clear_history()` leak fix
- **Frontend follow-up bar**: "Download Report" button (exports `.md`), redesigned "Continue Inquiry" button with gradient styling
- **Frontend downloadReport()**: one-click export of final report as Markdown file
- **Unit tests**: `test_evaluator.py` (8 scoring tests), `test_policy.py` (12 policy tests), `test_harness_api.py` (5 HTTP integration tests)
- **Search cache TTL**: 24-hour automatic expiration for search result caches

### Changed
- **Summarizer** simplified with `<think>` token stripping and quality gate
- **Reporter** model configurable via `LLM_REPORTER_MODEL_ID` env var
- **LLM timeout** increased to 300s; `max_tokens` limits per agent type
- **Search dispatch** refactored with `_try_single_search()` helper for clean retry loop

### Removed
- **`[TOOL_CALL]` magic strings** replaced with `NoteSubAgent` abstraction
- **`services/notes.py`**, **`text_processing.py`**, **`tool_events.py`** — dead code removed
- **Frontend "研究新主题" button** — removed from follow-up bar (duplicate of sidebar)

### Fixed
- `load_dotenv` race condition on startup
- `SearchAPI` enum serialization in API responses
- Reporter prompt optimization for shorter, higher-quality output
- Harness logging, stream event persistence, XSS protection in Markdown rendering
- Frontend button styling: follow-up bar buttons now use proper gradient styles (previously unstyled)
