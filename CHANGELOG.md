# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- **GitHub line-addressable evidence** with bounded commit-pinned source file excerpts, deterministic line ranges, citation sanitization, and Evidence Drawer links.
- **GitHub evidence acceptance record** covering authenticated collection, rate-limit degradation, artifact round-trip, and SSE compatibility.

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
