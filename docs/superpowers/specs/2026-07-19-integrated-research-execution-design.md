# Integrated Research Execution Design

## Status

Approved for implementation on 2026-07-19. The implementation targets the current dirty working tree on branch `codex/github-research-mode`; existing GitHub research changes are part of the baseline and must be preserved.

## Goal

Replace the outer-wrapper Harness architecture with one authoritative research execution path. Policy, context construction, events, persistence, and evaluation become responsibilities attached to the boundaries they govern instead of a second workflow that reconstructs Agent state from SSE events.

## Scope

This change will:

- keep `hello-agents==0.2.9` for this migration;
- keep current public `/research` and `/research/stream` behavior compatible;
- introduce one `ResearchApplicationService` used by synchronous and streaming routes;
- evolve the current run context into one authoritative `RunSession` rather than create a third state model;
- rename the workflow role conceptually to `ResearchCoordinator`, while retaining compatibility aliases where needed;
- publish typed internal events and project them to the existing SSE dictionary format;
- move policy checks to actual LLM, search, GitHub, and note operation boundaries;
- persist a redacted, versioned run snapshot atomically before publishing completion;
- split prompt-context assembly from follow-up-memory projection;
- keep terminal validity checks online and move quality scoring off the request-critical path;
- retain `HarnessRunner` temporarily as a compatibility facade, then remove its workflow responsibilities.

This change will not:

- upgrade to hello-agents 1.x;
- fork or vendor hello-agents;
- implement full Event Sourcing;
- rewrite the application as fully asynchronous;
- promise hard cancellation of an in-flight 0.2.9 LLM call;
- introduce a message broker, distributed worker, or database in the first migration;
- change research prompts or report-quality behavior unless required to preserve correctness.

## Current Problems

### Duplicate execution semantics

`DeepResearchAgent.run()` and `run_stream()` implement different orchestration paths. The synchronous path executes tasks sequentially while the streaming path creates daemon threads. `HarnessRunner.run()` and `stream()` duplicate the surrounding lifecycle again.

### Multiple sources of truth

The streaming Harness parses UI-facing SSE events and rebuilds `SummaryStateOutput`. That projection is lossy and can disagree with the Agent's real task state. In-process state must never be reconstructed from transport events.

### Unreliable terminal signal

The Agent currently emits `done` before context projection, evaluation, and persistence. A client can observe completion before the parent snapshot required for an immediate follow-up exists.

### Policy outside the operation boundary

Run-level preflight cannot guarantee that a real search, GitHub request, note write, or LLM call is authorized. Post-execution tool listeners are observational and cannot authorize a side effect.

### Unsafe records

The current record includes a complete configuration snapshot, accepts unvalidated run identifiers for file paths, and writes JSON/JSONL without atomic replacement or concurrency control.

### Framework mismatch

hello-agents 0.2.9 offers useful Agent and Tool primitives but no general run lifecycle, cancellation, session, policy, or persistence runtime. The project must compose around its public APIs rather than pretend that `ToolAwareSimpleAgent` supplies those missing capabilities.

## Architecture

### Components

```text
FastAPI routes
    -> ResearchApplicationService
         -> RunSession (one ResearchState + transitions + event sequence)
         -> ResearchCoordinator
              -> PlanningService
              -> SummarizationService
              -> ReportingService
              -> ResearchContextAssembler
              -> GovernedOperations
                   -> LLMPort
                   -> SearchPort
                   -> GitHubPort
                   -> NotePort
         -> FollowupContextProjector
         -> RunRepository
         -> ProjectionHub
              -> LegacySseProjector
              -> RedactedAuditProjector

OfflineEvaluationService
    -> RunRepository
    -> Assessment records
```

The first implementation should use focused modules under `backend/src/research/` while leaving existing `services/` implementations in place as adapters. It must not move every source file merely to make the directory tree look clean.

### RunSession and authoritative state

`RunSession` is the only mutable owner of one run's state. It contains:

- server-generated UUID run ID;
- parent run ID;
- topic and caller metadata;
- lifecycle status;
- canonical tasks and report;
- typed follow-up context;
- monotonic event sequence;
- timestamps, error classification, and lightweight metrics;
- cancellation/deadline state.

Existing `RunContext` data should be migrated into this model. During compatibility, adapters may expose the old shape, but no second mutable copy may be maintained.

State transitions occur first. A typed immutable event is emitted only after a valid transition. Events are projections and audit facts, not the persistence mechanism for reconstructing live state.

### ResearchApplicationService

The service owns the run lifecycle:

1. validate the command and parent identifier;
2. load a required parent snapshot or return an explicit not-found/conflict result;
3. perform command-level preflight;
4. create a `RunSession`;
5. run the coordinator once;
6. validate the terminal state;
7. build typed follow-up context;
8. atomically persist the required snapshot;
9. publish exactly one terminal event;
10. schedule or mark quality evaluation as pending.

Synchronous and streaming HTTP routes call this same lifecycle. Streaming differs only by subscribing an SSE projection.

### ResearchCoordinator

The existing `DeepResearchAgent` is a workflow coordinator, not a hello-agents conversational Agent. Its orchestration responsibilities move behind a `ResearchCoordinator` interface. Planner, summarizer, and reporter may continue using hello-agents role Agents internally.

The migration retains `DeepResearchAgent` as a compatibility name until callers and benchmarks are migrated.

The coordinator changes state only through `RunSession` transition methods and calls external operations only through typed ports.

### Governed operations and typed ports

Search, GitHub, Note, and LLM retain distinct request and response types. They share middleware semantics, not one `dict -> Any` gateway:

```text
authorize
-> check cancellation/deadline
-> publish operation_started
-> invoke typed adapter
-> collect duration and safe metadata
-> publish operation_completed or operation_failed
```

Policy is checked before every side effect. A hello-agents post-tool listener may enrich observability but is never an authorization boundary.

### Context

Context is separated into two deterministic services:

- `ResearchContextAssembler`: constructs each role's prompt input from typed task state, prior follow-up context, notes, and source references under explicit budgets;
- `FollowupContextProjector`: creates the compact, versioned context persisted for the next run.

Neither service reads SSE events. The 0.2.9 `ContextBuilder` may inspire packet and budget concepts, but its I/O and compression implementation are not used directly.

### Events and projections

Internal events use a versioned envelope containing:

- `schema_version`;
- `run_id`;
- optional `task_id` and `operation_id`;
- monotonic `sequence`;
- UTC timestamp;
- discriminated event type;
- a JSON-serializable, redacted payload.

Initial event types cover run, plan, task, operation, source, report, failure, cancellation, and terminal completion. The legacy SSE projector preserves existing frontend event names during migration. Raw page bodies, API keys, full configuration, and complete prompts are not event payloads.

### Persistence

`RunRepository` stores a versioned canonical snapshot. The first implementation remains file-backed but must:

- validate UUID identifiers;
- resolve paths beneath the configured root;
- redact configuration using an allowlist;
- write to a temporary file and atomically replace the target;
- serialize writes that share files;
- distinguish canonical snapshot, compact follow-up context, and optional audit events;
- validate loaded records and reject corrupt or unsupported schemas explicitly.

SQLite is a later option if multiple processes or richer querying become requirements.

### Completion semantics

A run is complete only when:

- the canonical report exists;
- all task states are terminal;
- terminal validity checks pass;
- the canonical snapshot and follow-up context are durably stored.

Verbose audit projection and offline quality scoring may be eventually consistent and must not delay completion. A persistence failure produces `run_failed`; the system must never emit `run_completed` first.

### Cancellation and concurrency

Task execution uses an application-owned bounded executor. Daemon threads and an unbounded event queue are removed.

Cancellation and deadlines are checked before and after each blocking operation and before starting another task. Because hello-agents 0.2.9 does not expose cooperative cancellation for an in-flight LLM request, such a run is represented as cancellation pending until the call returns; it is not falsely reported as already cancelled.

Each concurrent task receives its own hello-agents Agent instance because Agent history is mutable.

### Evaluation

Online code retains `validate_terminal_state()` for correctness requirements such as a non-empty report and terminal task statuses. Quality scores and findings are produced by an offline evaluation service from persisted snapshots and stored separately as assessments. Evaluation cannot rewrite the report or run status.

## Compatibility Strategy

- Existing HTTP routes and legacy SSE event names remain during the first release.
- `HarnessRunner` delegates to `ResearchApplicationService` and exposes old response models only at the compatibility boundary.
- Existing `DeepResearchAgent` imports remain available as an alias or adapter.
- Existing record readers support current records while new records carry `schema_version`.
- A run-level `RESEARCH_APPLICATION_V2` switch permits rollback; old and new coordinators must never dual-run the same request.
- `/harness/*` routes may be retained as deprecated aliases until `/runs/*` consumers migrate.

## Architecture Invariants

1. One run has one authoritative mutable research state.
2. SSE, audit, and evaluation projections never rebuild or mutate that state.
3. A state transition succeeds before its event is published.
4. `run_completed`, `run_failed`, `run_cancelled`, and `run_rejected` are mutually exclusive.
5. Required persistence succeeds before `run_completed` is observable.
6. Synchronous and streaming routes invoke the same coordinator lifecycle.
7. External effects cross typed ports and pre-execution policy middleware.
8. Secrets and raw source bodies never enter ordinary events or canonical configuration snapshots.
9. Concurrency is bounded; no new daemon worker is created.
10. Context is built from typed state and snapshots, never parsed from transport chunks.
11. Retries carry stable operation IDs and explicit attempt numbers.
12. Offline assessment cannot modify canonical run state.

## Error Semantics

- invalid command or run ID: HTTP 400;
- rejected capability: HTTP 403 or typed SSE `run_rejected`;
- missing parent snapshot: HTTP 404, or 409 if the parent is known but not yet durable;
- tool or task failure: canonical failed task state and typed operation/task failure events;
- required persistence failure: `run_failed`, never `run_completed`;
- client disconnect: request cancellation is recorded and no new operation starts, subject to the in-flight 0.2.9 limitation;
- corrupt or unsupported stored record: explicit repository error, not silent fallback.

## Test Strategy

Tests are written first and must observe the intended failure before implementation. Acceptance coverage includes:

- RunSession transition and single-terminal-event rules;
- typed event serialization and monotonic sequence;
- synchronous/streaming final snapshot equivalence;
- immediate GET and follow-up success after `run_completed`;
- persistence failure emitting only failure;
- task exception consistency across state, SSE, and report;
- policy denial before a typed adapter executes;
- cancellation preventing new operations;
- no secret values or raw source bodies in persisted snapshots/events;
- UUID and path traversal rejection;
- concurrent repository writes remaining valid;
- legacy SSE and HTTP response compatibility;
- real hello-agents 0.2.9 contract tests without global `sys.modules` fakes;
- backend Ruff, Mypy, full Pytest, and frontend production build.

## Migration Sequence

### Stage 0: safety and characterization

Add characterization tests, redact configuration, validate identifiers, add atomic persistence, and correct completion ordering without changing research prompts.

### Stage 1: contracts and compatibility service

Introduce RunSession, typed events, repository/context ports, and ResearchApplicationService. Reduce HarnessRunner to a compatibility facade while keeping public API shapes stable.

### Stage 2: authoritative state and operation boundaries

Move coordinator mutations into RunSession, remove the streaming event reducer, unify synchronous and streaming execution, and wrap LLM/Search/GitHub/Note adapters with governed operation middleware and bounded concurrency.

### Stage 3: cleanup

Move quality evaluation out of the request path, expose canonical run APIs, migrate frontend types, retain compatibility aliases for one release, then delete obsolete Harness workflow modules and stale documentation.

## Rollback

Rollback is selected per run using `RESEARCH_APPLICATION_V2`; it never executes both paths. New snapshots retain the current report/task fields required by the old reader. Compatibility facades and old import names remain until the new path passes all acceptance tests and one release cycle completes.
