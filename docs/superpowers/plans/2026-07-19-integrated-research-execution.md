# Integrated Research Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Harness-owned duplicate workflow with one authoritative, synchronous research execution service whose typed events are projected to the existing SSE protocol only after canonical state transitions.

**Architecture:** `ResearchApplicationService.execute()` is the only application lifecycle. A `RunSession` owns one canonical `ResearchState`, a coordinator changes that state through session transitions, typed observers receive safe events, and a file-backed `RunRepository` durably stores the final snapshot and follow-up context before completion is published. `HarnessRunner` remains only as a one-release compatibility facade.

**Tech Stack:** Python 3.10+, FastAPI, dataclasses, Pydantic configuration, `hello-agents==0.2.9`, pytest/unittest, Ruff, Mypy, Vue 3, TypeScript, Vite.

## Global Constraints

- Keep `hello-agents==0.2.9`; do not upgrade, fork, vendor, or depend on private framework fields.
- Preserve all pre-existing dirty-worktree GitHub research behavior and fields: `github_repository`, `source_strategy`, `repository`, GitHub report context, quality-gate configuration, and compatibility tests.
- Work in the current `codex/github-research-mode` checkout with the user's approval; never reset, stash, discard, or overwrite unrelated changes.
- Do not stage or commit pre-existing user changes. A task may commit new files and files that were clean at the recorded baseline; overlapping dirty files remain unstaged unless their hunks can be isolated safely.
- Write each behavior test first, run it, and confirm the expected failure before adding production code.
- One run has one canonical mutable `ResearchState`; SSE, audit, persistence readers, and evaluation never rebuild or mutate it.
- `ResearchApplicationService.execute()` is the only application execution entry; streaming is an observer/queue adapter around that synchronous call.
- Required snapshot and follow-up context persistence succeeds before `run_completed` becomes observable.
- Internal events are schema-versioned and redacted. API keys, tokens, full configuration, raw web bodies, HTTP headers, and complete prompts/results are never recorded in canonical events.
- Use a bounded executor and cooperative cancellation checks. Do not promise hard cancellation of an in-flight hello-agents 0.2.9 LLM call.
- Retain current HTTP routes and legacy SSE event names for the first release; add canonical `/runs/{run_id}` while keeping `/harness/runs/{run_id}` as an alias.
- Do not implement full Event Sourcing, a database, a broker, or a fully asynchronous coordinator in this migration.
- Do not delete local `backend/benchmark_runs/` artifacts. Add ignore rules and code-level redaction; the user must rotate the exposed credential separately.

---

### Task 1: Security containment and authoritative domain contracts

**Files:**
- Modify: `.gitignore`
- Create: `backend/src/research/__init__.py`
- Create: `backend/src/research/contracts.py`
- Create: `backend/src/research/session.py`
- Modify: `backend/src/models.py`
- Modify: `backend/src/harness/models.py`
- Test: `backend/tests/test_run_session.py`
- Test: `backend/tests/test_research_contracts.py`

**Interfaces:**
- Consumes: current `Configuration`, `TodoItem`, `SummaryState`, and `SummaryStateOutput`.
- Produces: `ResearchCommand`, `RunStatus`, `EventKind`, `ResearchEvent`, `RunError`, `ResearchState`, `RunSession`, `PreparedTerminal`, and compatibility aliases `HarnessRunRequest`, `HarnessEvent`, `RunContext`.

- [ ] **Step 1: Add the ignored-artifact assertion and contract tests**

```python
# backend/tests/test_research_contracts.py
from pathlib import Path
from uuid import UUID

import pytest

from config import Configuration
from research.contracts import ResearchCommand


def test_generated_run_id_is_uuid_hex() -> None:
    command = ResearchCommand(topic="topic", config=Configuration.from_env())
    assert UUID(command.run_id).hex == command.run_id


@pytest.mark.parametrize(
    "value",
    ["", "../escape", "..\\escape", "C:\\escape", "uuid.json"],
)
def test_invalid_explicit_run_id_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        ResearchCommand(topic="topic", config=Configuration.from_env(), run_id=value)


def test_benchmark_run_artifacts_are_ignored() -> None:
    patterns = (
        Path(__file__).resolve().parents[2] / ".gitignore"
    ).read_text(encoding="utf-8")
    assert "backend/benchmark_runs/" in patterns
    assert "backend/benchmark_results.json" in patterns
```

```python
# backend/tests/test_run_session.py
import pytest

from config import Configuration
from models import ResearchState, TodoItem
from research.contracts import EventKind, ResearchCommand, RunStatus
from research.session import InvalidTransitionError, RunSession


def make_session() -> RunSession:
    command = ResearchCommand(topic="topic", config=Configuration.from_env())
    return RunSession(command=command, state=ResearchState(research_topic="topic"))


def test_state_changes_before_observer_receives_event() -> None:
    observed: list[tuple[RunStatus, EventKind]] = []
    session = make_session()
    session.add_observer(lambda event: observed.append((session.status, event.kind)))
    session.start()
    assert observed == [(RunStatus.RUNNING, EventKind.RUN_STARTED)]


def test_event_sequence_is_monotonic() -> None:
    session = make_session()
    session.start()
    session.install_plan([TodoItem(id=1, title="T", intent="I", query="q")])
    assert [event.sequence for event in session.events] == [1, 2]


def test_task_failure_updates_canonical_task_before_event() -> None:
    session = make_session()
    session.start()
    session.install_plan([TodoItem(id=1, title="T", intent="I", query="q")])
    observed: list[str] = []
    session.add_observer(
        lambda event: observed.append(session.state.todo_items[0].status)
        if event.kind is EventKind.TASK_FAILED
        else None
    )
    session.fail_task(1, message="boom", code="task_failed")
    assert session.state.todo_items[0].status == "failed"
    assert observed == ["failed"]


def test_second_terminal_confirmation_is_rejected() -> None:
    session = make_session()
    session.start()
    prepared = session.prepare_terminal(RunStatus.COMPLETED, EventKind.RUN_COMPLETED)
    session.confirm_terminal(prepared)
    with pytest.raises(InvalidTransitionError):
        session.prepare_terminal(RunStatus.FAILED, EventKind.RUN_FAILED)
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_research_contracts.py tests/test_run_session.py -q
```

Expected: collection fails because `research.contracts`, `research.session`, and `ResearchState` do not exist.

- [ ] **Step 3: Add the runtime-artifact ignore rules**

Append exactly these entries without removing the user's existing `.superpowers/` line:

```gitignore
backend/benchmark_runs/
backend/benchmark_results.json
backend/cache/
backend/notes/
```

- [ ] **Step 4: Implement domain contracts**

Implement these exact public shapes in `backend/src/research/contracts.py`:

```python
class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class EventKind(str, Enum):
    RUN_STARTED = "run_started"
    POLICY_CHECKED = "policy_checked"
    REPOSITORY_DETECTED = "repository_detected"
    PLAN_CREATED = "plan_created"
    TASK_STARTED = "task_started"
    SOURCES_COLLECTED = "sources_collected"
    SUMMARY_DELTA = "summary_delta"
    TASK_RETRY_SCHEDULED = "task_retry_scheduled"
    TASK_COMPLETED = "task_completed"
    TASK_SKIPPED = "task_skipped"
    TASK_FAILED = "task_failed"
    REPORT_NOTE_CREATED = "report_note_created"
    REPORT_GENERATED = "report_generated"
    OPERATION_STARTED = "operation_started"
    OPERATION_COMPLETED = "operation_completed"
    OPERATION_FAILED = "operation_failed"
    OPERATION_REJECTED = "operation_rejected"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RUN_REJECTED = "run_rejected"


@dataclass(frozen=True, kw_only=True)
class ResearchCommand:
    topic: str
    config: Configuration
    run_id: str = field(default_factory=lambda: uuid4().hex)
    metadata: dict[str, Any] = field(default_factory=dict)
    permission_mode: str = "default"
    caller_mode: str = "public"
    parent_run_id: str | None = None

    def __post_init__(self) -> None:
        if not self.topic.strip():
            raise ValueError("Research topic must not be empty.")
        object.__setattr__(self, "run_id", normalize_run_id(self.run_id))
        if self.parent_run_id is not None:
            object.__setattr__(self, "parent_run_id", normalize_run_id(self.parent_run_id))


@dataclass(frozen=True, kw_only=True)
class ResearchEvent:
    kind: EventKind
    run_id: str
    sequence: int
    occurred_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: int | None = None
    operation_id: str | None = None
    schema_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        json.dumps(self.payload)
        return {
            "schema_version": self.schema_version,
            "type": self.kind.value,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "operation_id": self.operation_id,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": self.payload,
        }


@dataclass(frozen=True, kw_only=True)
class PreparedTerminal:
    status: RunStatus
    event: ResearchEvent
    snapshot: "RunSnapshot"


@dataclass(frozen=True, kw_only=True)
class RunError:
    code: str
    message: str


@dataclass(frozen=True, kw_only=True)
class RunSnapshot:
    run_id: str
    topic: str
    status: RunStatus
    started_at: datetime
    completed_at: datetime | None
    parent_run_id: str | None
    output: dict[str, Any]
    followup_context: dict[str, Any]
    metrics: dict[str, Any]
    policy_decisions: tuple[dict[str, Any], ...]
    config_snapshot: dict[str, Any]
    events: tuple[ResearchEvent, ...]
    error: RunError | None = None
    schema_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "topic": self.topic,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "parent_run_id": self.parent_run_id,
            "output": self.output,
            "followup_context": self.followup_context,
            "metrics": self.metrics,
            "policy_decisions": list(self.policy_decisions),
            "config_snapshot": self.config_snapshot,
            "events": [event.as_dict() for event in self.events],
            "error": asdict(self.error) if self.error else None,
        }


@dataclass(frozen=True, kw_only=True)
class ResearchRunResult:
    run_id: str
    status: RunStatus
    output: SummaryStateOutput | None
    error: RunError | None
    metrics: dict[str, Any]
    followup_context: dict[str, Any]
    policy_decisions: tuple[dict[str, Any], ...]
    evaluation_status: str = "pending"
```

`normalize_run_id()` must parse with `UUID`, require that the input contains only a canonical UUID string or its 32-character hex form, and return `UUID(value).hex`. `ResearchEvent.as_dict()` must call `json.dumps(payload)` before returning so non-serializable payloads fail at emission time.

- [ ] **Step 5: Evolve the canonical state without creating a copy**

In `backend/src/models.py`, rename the class object and retain an identity alias:

```python
@dataclass(kw_only=True)
class ResearchState:
    research_topic: str | None = None
    search_query: str | None = None
    web_research_results: list[str] = field(default_factory=list)
    sources_gathered: list[str] = field(default_factory=list)
    research_loop_count: int = 0
    running_summary: str | None = None
    todo_items: list[TodoItem] = field(default_factory=list)
    structured_report: str | None = None
    report_note_id: str | None = None
    report_note_path: str | None = None
    github_context: dict[str, Any] = field(default_factory=dict)


SummaryState = ResearchState
```

Add `notices` to `TodoItem.to_dict()` because it is part of canonical task state. Do not remove `source_strategy`, `repository`, or any current GitHub fields.

- [ ] **Step 6: Implement RunSession transitions**

`RunSession` must hold `command`, `state`, lifecycle fields, a private `RLock`, the event list, the next sequence, observers, policy decisions, metrics, error, follow-up context, and cancellation state. Implement explicit methods:

The concrete public method signatures are `start() -> ResearchEvent`,
`install_plan(tasks: Sequence[TodoItem]) -> ResearchEvent`,
`start_task(task_id: int) -> ResearchEvent`,
`record_sources(task_id: int, **safe_payload: object) -> ResearchEvent`,
`append_task_summary(task_id: int, chunk: str) -> ResearchEvent`,
`record_retry(task_id: int, *, previous_query: str, refined_query: str, attempt: int, reason: str) -> ResearchEvent`,
`complete_task(task_id: int, *, summary: str, sources_summary: str | None) -> ResearchEvent`,
`skip_task(task_id: int, *, reason: str) -> ResearchEvent`,
`fail_task(task_id: int, *, message: str, code: str) -> ResearchEvent`,
`set_report(report: str, *, note_id: str | None = None, note_path: str | None = None) -> ResearchEvent`,
`prepare_terminal(status: RunStatus, kind: EventKind, **payload: object) -> PreparedTerminal`,
`confirm_terminal(prepared: PreparedTerminal) -> ResearchEvent`,
`request_cancellation() -> None`, `raise_if_cancelled() -> None`, and
`to_legacy_output() -> SummaryStateOutput`, plus
`to_snapshot(*, status: RunStatus | None = None, terminal_event: ResearchEvent | None = None) -> RunSnapshot`.

`prepare_terminal()` must not change status, append the event, or notify observers. `confirm_terminal()` performs those actions exactly once. All nonterminal methods mutate canonical state under the lock before notifying observers.

Implement a small `CancellationToken` backed by `threading.Event` with `cancel()`, `is_cancelled`, and `raise_if_cancelled()`. Define singleton `NEVER_CANCELLED` using a token whose event is never set.

- [ ] **Step 7: Replace Harness model definitions with identity aliases**

Keep `EvaluationFinding`, `HarnessRunResult`, and legacy record types for compatibility, but define:

```python
from research.contracts import ResearchCommand as HarnessRunRequest
from research.contracts import ResearchEvent as HarnessEvent
from research.session import RunSession as RunContext
```

Update compatibility serialization to use `event.as_dict()` and `context.to_legacy_output()`; do not create wrapper subclasses.

- [ ] **Step 8: Run focused tests and the existing policy/evaluator tests**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_research_contracts.py tests/test_run_session.py tests/test_policy.py tests/test_evaluator.py -q
```

Expected: all selected tests pass; update evaluator fixtures to construct state through session methods if direct `context.result` assignment no longer exists.

- [ ] **Step 9: Review and record the task boundary**

Run `git diff --check`. Commit only newly created research files and clean-baseline files. Leave overlapping `backend/src/models.py` changes unstaged if isolating them would include the user's prior GitHub fields.

---

### Task 2: Atomic redacted repository, follow-up context, and terminal validation

**Files:**
- Create: `backend/src/research/context.py`
- Create: `backend/src/research/repository.py`
- Create: `backend/src/research/validation.py`
- Modify: `backend/src/harness/recorder.py`
- Modify: `backend/src/harness/compressor.py`
- Test: `backend/tests/test_run_repository.py`
- Test: `backend/tests/test_followup_context.py`
- Test: `backend/tests/test_terminal_validation.py`

**Interfaces:**
- Consumes: `RunSession`, `ResearchCommand`, `SummaryStateOutput`, `PreparedTerminal`.
- Produces: `FollowupContext`, `FollowupContextProjector`, `ResearchContextAssembler`, `RunSnapshot`, `FileRunRepository`, explicit repository errors, `validate_terminal_state()`.

- [ ] **Step 1: Write repository redaction, validation, and atomicity tests**

```python
def test_snapshot_configuration_uses_allowlist(tmp_path, completed_session):
    completed_session.command.config.llm_api_key = "secret-llm"
    completed_session.command.config.github_token = "secret-github"
    repository = FileRunRepository(tmp_path)
    repository.save(completed_session.to_snapshot(status=RunStatus.COMPLETED))
    raw = (tmp_path / "runs" / f"{completed_session.run_id}.json").read_text("utf-8")
    assert "secret-llm" not in raw
    assert "secret-github" not in raw
    assert "llm_api_key" not in raw
    assert "github_token" not in raw


@pytest.mark.parametrize("value", ["../escape", "..\\escape", "C:\\escape", "bad.json"])
def test_repository_rejects_path_like_run_ids(tmp_path, value):
    with pytest.raises(InvalidRunIdError):
        FileRunRepository(tmp_path).load(value)


def test_failed_replace_keeps_previous_snapshot_readable(tmp_path, monkeypatch, snapshot):
    repository = FileRunRepository(tmp_path)
    repository.save(snapshot)
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("replace failed")))
    with pytest.raises(RunRepositoryError):
        repository.save(replace(snapshot, metrics={"new": True}))
    assert FileRunRepository(tmp_path).load(snapshot.run_id).metrics == snapshot.metrics
```

Add concurrent-save coverage using `ThreadPoolExecutor(max_workers=4)` and assert every read parses as schema version 1.

- [ ] **Step 2: Write follow-up projection and terminal validation tests**

```python
def test_followup_projection_is_versioned_and_bounded(completed_session):
    context = FollowupContextProjector().project(completed_session)
    assert context.schema_version == 1
    assert len(context.key_findings) <= 5
    assert len(context.key_sources) <= 3
    assert len(context.open_questions) <= 10


def test_failed_and_skipped_tasks_become_open_questions(session_with_terminal_tasks):
    context = FollowupContextProjector().project(session_with_terminal_tasks)
    assert "Failed task" in context.open_questions
    assert "Skipped task" in context.open_questions


def test_context_assembler_returns_only_budgeted_typed_memory(followup_context):
    assembled = ResearchContextAssembler().assemble(followup_context)
    assert set(assembled) == {"key_findings", "key_sources", "open_questions"}
    assert len(assembled["key_findings"]) <= 5
    assert "raw_context" not in json.dumps(assembled)


@pytest.mark.parametrize("status", ["pending", "in_progress"])
def test_terminal_validator_rejects_nonterminal_task(status, completed_session):
    completed_session.state.todo_items[0].status = status
    with pytest.raises(TerminalStateError):
        validate_terminal_state(completed_session)
```

- [ ] **Step 3: Run the three test files and verify RED**

Expected: imports fail because repository/context/validation modules do not exist.

- [ ] **Step 4: Implement deterministic follow-up context**

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class FollowupContext:
    source_run_id: str
    key_findings: tuple[str, ...]
    key_sources: tuple[str, ...]
    open_questions: tuple[str, ...]
    schema_version: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_run_id": self.source_run_id,
            "key_findings": list(self.key_findings),
            "key_sources": list(self.key_sources),
            "open_questions": list(self.open_questions),
        }


class FollowupContextProjector:
    MAX_FINDINGS = 5
    MAX_SOURCES = 3
    MAX_OPEN_QUESTIONS = 10

    def project(self, session: RunSession) -> FollowupContext:
        findings = tuple(
            task.summary.strip()[:180]
            for task in session.state.todo_items
            if task.summary and task.status == "completed"
        )[: self.MAX_FINDINGS]
        sources = tuple(
            task.sources_summary.splitlines()[0].strip()[:180]
            for task in session.state.todo_items
            if task.sources_summary
        )[: self.MAX_SOURCES]
        questions = tuple(
            task.title for task in session.state.todo_items
            if task.status != "completed"
        )[: self.MAX_OPEN_QUESTIONS]
        return FollowupContext(
            source_run_id=session.run_id,
            key_findings=findings,
            key_sources=sources,
            open_questions=questions,
        )

    def to_legacy_reasoning_memory(self, context: FollowupContext) -> dict[str, object]:
        return {
            "key_findings": list(context.key_findings),
            "key_sources": list(context.key_sources),
            "open_questions": list(context.open_questions),
        }


class ResearchContextAssembler:
    def assemble(self, context: FollowupContext | None) -> dict[str, object] | None:
        if context is None:
            return None
        return {
            "key_findings": list(context.key_findings[:5]),
            "key_sources": list(context.key_sources[:3]),
            "open_questions": list(context.open_questions[:10]),
        }
```

Use canonical task summaries and source summaries only. Truncate individual findings/sources to the existing 180-character compatibility budget. Do not read events or raw web context.

- [ ] **Step 5: Implement terminal validation**

`validate_terminal_state(session)` must require a non-empty canonical report and every task status in `{"completed", "failed", "skipped", "cancelled"}`. It must not reject an empty task list or score summary/source quality; those are offline assessment concerns.

- [ ] **Step 6: Implement the atomic file repository**

Use an envelope:

```json
{"schema_version": 1, "snapshot": {}, "followup_context": {}}
```

The exact save sequence is:

```python
with shared_root_lock:
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=runs_dir, delete=False
    ) as handle:
        json.dump(envelope, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        temporary_path = Path(handle.name)
    os.replace(temporary_path, target_path)
```

All exceptions remove only the validated temporary file and raise `RunRepositoryError`. Never delete or replace the previous target after a failed write. Define explicit `InvalidRunIdError`, `RunNotFoundError`, `CorruptRunRecordError`, and `UnsupportedSchemaError`.

Persist only this configuration allowlist:

```python
SAFE_CONFIG_FIELDS = (
    "llm_provider",
    "llm_model_id",
    "llm_reporter_model_id",
    "search_api",
    "max_web_research_loops",
    "fetch_full_page",
    "strip_thinking_tokens",
    "use_tool_calling",
    "enable_notes",
    "enable_quality_gate",
    "enable_github_research",
)
```

Exclude URLs, workspaces, request metadata, keys, tokens, prompt text, and raw source bodies.

- [ ] **Step 7: Turn legacy recorder/compressor into adapters**

`JsonlRunRecorder.persist()` delegates canonical snapshot storage to `FileRunRepository`. Its `load()` returns the legacy record dictionary derived from the loaded v1 snapshot. `ContextCompressor.compress_output()` delegates to a compatibility projection helper and is marked deprecated in its docstring. Do not keep a second full event JSONL containing unsafe payloads.

- [ ] **Step 8: Verify focused and compatibility tests**

Run the three new test files plus existing evaluator/policy tests. Then run `git diff --check` and review only the files in this task.

---

### Task 3: Single application lifecycle with typed observers

**Files:**
- Create: `backend/src/research/ports.py`
- Create: `backend/src/research/observers.py`
- Create: `backend/src/research/application.py`
- Test: `backend/tests/test_research_application.py`

**Interfaces:**
- Consumes: Task 1 session/contracts and Task 2 repository/projector/validator.
- Produces: `ResearchCoordinator` protocol, `ResearchApplicationService.execute()`, observer protocol, `ResearchRunResult`.

- [ ] **Step 1: Write lifecycle ordering tests with deterministic fakes**

```python
class RecordingRepository:
    def __init__(self, log): self.log = log; self.snapshots = {}
    def save(self, snapshot): self.log.append("save"); self.snapshots[snapshot.run_id] = snapshot
    def load(self, run_id):
        if run_id not in self.snapshots: raise RunNotFoundError(run_id)
        return self.snapshots[run_id]


class FakeCoordinator:
    def execute(self, session, prior_context):
        session.install_plan([TodoItem(id=1, title="T", intent="I", query="q")])
        session.start_task(1)
        session.complete_task(1, summary="summary", sources_summary="source")
        session.set_report("# report")


def test_completion_is_observed_after_save(configuration):
    log = []
    repository = RecordingRepository(log)
    observer = lambda event: log.append(event.kind.value)
    service = make_service(repository=repository, coordinator=FakeCoordinator())
    result = service.execute(
        ResearchCommand(topic="topic", config=configuration), observer=observer
    )
    assert result.status is RunStatus.COMPLETED
    assert log.index("save") < log.index("run_completed")


def test_persistence_failure_emits_failure_only(configuration):
    observer_events = []
    service = make_service(repository=FailingRepository(), coordinator=FakeCoordinator())
    result = service.execute(
        ResearchCommand(topic="topic", config=configuration),
        observer=lambda event: observer_events.append(event.kind),
    )
    assert result.status is RunStatus.FAILED
    assert EventKind.RUN_COMPLETED not in observer_events
    assert observer_events.count(EventKind.RUN_FAILED) == 1


def test_missing_parent_does_not_call_coordinator(configuration):
    coordinator = SpyCoordinator()
    result = make_service(repository=RecordingRepository([]), coordinator=coordinator).execute(
        ResearchCommand(
            topic="follow-up", config=configuration, parent_run_id=uuid4().hex
        )
    )
    assert result.status is RunStatus.FAILED
    assert coordinator.call_count == 0
```

- [ ] **Step 2: Run application tests and verify RED**

Expected: `research.application` and port definitions are missing.

- [ ] **Step 3: Define focused ports**

```python
class ResearchCoordinator(Protocol):
    def execute(self, session: RunSession, prior_context: FollowupContext | None) -> None:
        raise NotImplementedError


class RunRepository(Protocol):
    def save(self, snapshot: RunSnapshot) -> None:
        raise NotImplementedError

    def load(self, run_id: str) -> RunSnapshot:
        raise NotImplementedError


class ResearchEventObserver(Protocol):
    def __call__(self, event: ResearchEvent) -> None:
        raise NotImplementedError
```

Use `NullObserver` and `CompositeObserver`; observers must not mutate sessions.

- [ ] **Step 4: Implement the only application execution method**

Implement `ResearchApplicationService.execute(command: ResearchCommand, *, observer: ResearchEventObserver = NULL_OBSERVER, cancellation: CancellationToken = NEVER_CANCELLED) -> ResearchRunResult` as the only public execution method.

The implementation order is command policy preflight, strict parent load, session start, coordinator execute, terminal validation, follow-up projection, prepared completed snapshot, repository save, terminal confirmation, result projection. A `PermissionError` becomes `REJECTED`; repository/not-found/validation/operation exceptions become typed failed results. There is no `stream()` method on this service.

Maintain a lock-protected set of active run IDs. If a requested parent is active but not durable, return `parent_pending` for HTTP 409; if it is neither active nor stored, return `parent_not_found` for HTTP 404.

When a required save fails, publish one `RUN_FAILED` event without attempting another required save through the same failing repository.

- [ ] **Step 5: Verify focused tests and run all Task 1-3 tests**

Run all new research tests. Confirm no network, LLM, SearchTool, NoteTool, or GitHub calls occur.

---

### Task 4: Migrate DeepResearchAgent into one coordinator execution path

**Files:**
- Modify: `backend/src/agent.py`
- Modify: `backend/src/services/planner.py`
- Modify: `backend/src/services/summarizer.py`
- Modify: `backend/src/services/reporter.py`
- Modify: `backend/src/config.py`
- Modify: `backend/.env.example`
- Create: `backend/tests/test_research_coordinator.py`
- Modify: `backend/tests/test_agent_github_stream.py`

**Interfaces:**
- Consumes: `ResearchCoordinator`, `RunSession`, typed transitions, existing planning/search/summarization/report/note/GitHub behavior.
- Produces: `DeepResearchAgent.execute(session, prior_context) -> None`; legacy `run()` and `run_stream()` become adapters over that same method.

- [ ] **Step 1: Write deterministic coordinator equivalence and failure-state tests**

Build a coordinator with fake planner, search adapter, summarizer, reporter, note adapter, and GitHub adapter. Do not globally fake `hello_agents`. Assert:

```python
def test_legacy_run_and_stream_use_identical_canonical_output(fake_coordinator_factory):
    sync_agent = fake_coordinator_factory()
    stream_agent = fake_coordinator_factory()
    sync_output = sync_agent.run("topic")
    events = list(stream_agent.run_stream("topic"))
    assert stream_agent.last_session.to_legacy_output() == sync_output
    assert events[-1]["type"] == "done"


def test_worker_exception_marks_canonical_task_failed(fake_coordinator_factory):
    agent = fake_coordinator_factory(search_error=RuntimeError("boom"))
    session = make_started_session()
    agent.execute(session, None)
    assert session.state.todo_items[0].status == "failed"
    assert any(event.kind is EventKind.TASK_FAILED for event in session.events)
```

Retain the current GitHub repository event assertions and verify `source_strategy` and `repository` survive canonical projection.

- [ ] **Step 2: Run coordinator tests and verify RED**

Expected: `DeepResearchAgent.execute()` and `last_session` do not exist, and sync/stream still have separate semantics.

- [ ] **Step 3: Add bounded concurrency configuration**

Add `max_concurrent_tasks: int` with environment variable `MAX_CONCURRENT_TASKS`, default 4, lower bound 1, upper bound 16. Add the same documented default to `.env.example`.

- [ ] **Step 4: Implement one coordinator method**

`execute(session, prior_context)` performs planning, task execution, report generation, and session transitions. Planner returns a task list instead of mutating state. Reporting returns report text; the coordinator calls `session.set_report()`.

Legacy adapters are exact projections:

```python
def run(self, topic: str, prior_context: dict[str, Any] | None = None) -> SummaryStateOutput:
    session = self._new_legacy_session(topic)
    self.execute(session, FollowupContext.from_legacy(prior_context))
    self.last_session = session
    return session.to_legacy_output()


def run_stream(self, topic: str, prior_context: dict[str, Any] | None = None):
    observer = LegacyCollectingObserver()
    session = self._new_legacy_session(topic, observer=observer)
    worker = self._legacy_executor.submit(
        self.execute, session, FollowupContext.from_legacy(prior_context)
    )
    yield from observer.iter_legacy_events_until(worker)
    self.last_session = session
    yield {"type": "done"}
```

`done` remains only for the legacy direct Agent method. `ResearchApplicationService` never calls `run_stream()` and owns the real terminal event.

- [ ] **Step 5: Replace daemon threads with a bounded executor**

Use `ThreadPoolExecutor(max_workers=min(config.max_concurrent_tasks, len(tasks)))`. No worker mutates global event sequence. Worker exceptions return typed task failure data to the coordinator, which calls `session.fail_task()`. Note writes and canonical merges occur on the coordinating thread.

Use a bounded event/result queue and short timeout cancellation checks. Replace search backoff `time.sleep(delay)` with `cancellation.event.wait(delay)` when a cancellation token is supplied.

- [ ] **Step 6: Preserve GitHub and quality-gate behavior**

Keep current repository detection, four fixed GitHub research tasks, GitHub context augmentation, report prompt additions, `enable_quality_gate`, retry fields, note metadata, `source_strategy`, and `repository`. Remove `raw_context` from ordinary streamed event payloads; retain `latest_sources`.

- [ ] **Step 7: Verify coordinator tests, GitHub tests, and existing backend suite**

Run coordinator/GitHub tests first, then the complete backend test suite. Record any expected fixture migrations separately from behavior failures.

---

### Task 5: Compatibility facade, legacy SSE projection, and reliable HTTP completion

**Files:**
- Create: `backend/src/research/legacy_sse.py`
- Modify: `backend/src/harness/runner.py`
- Modify: `backend/src/harness/__init__.py`
- Modify: `backend/src/main.py`
- Create: `backend/tests/test_harness_runner_compat.py`
- Modify: `backend/tests/test_harness_api.py`
- Modify: `frontend/src/services/api.ts`

**Interfaces:**
- Consumes: application service, observers, canonical snapshots.
- Produces: compatibility `HarnessRunner.run/stream/load_record`, canonical `/runs/{run_id}`, legacy typed SSE events.

- [ ] **Step 1: Write real-facade terminal-order and SSE golden tests**

Do not replace the `harness` module in `sys.modules`. Instantiate a real facade with a deterministic application service.

```python
def test_done_is_yielded_only_after_record_is_loadable(real_facade, command):
    for event in real_facade.stream(command):
        if event["type"] == "done":
            record = real_facade.load_record(command.run_id)
            assert record["status"] == "completed"


def test_failed_run_has_error_without_done(real_facade_with_failing_repository, command):
    events = list(real_facade_with_failing_repository.stream(command))
    assert [event["type"] for event in events].count("error") == 1
    assert not any(event["type"] == "done" for event in events)


def test_legacy_projection_keeps_github_fields(legacy_projector, github_events):
    projected = [legacy_projector.project(event) for event in github_events]
    assert projected[0]["type"] == "github_repository"
    assert projected[0]["repository"]["full_name"] == "bytedance/deer-flow"
```

- [ ] **Step 2: Run facade/API tests and verify RED**

Expected: current facade sends `done` before persistence and still reconstructs state from stream dictionaries.

- [ ] **Step 3: Implement the legacy SSE projector**

Map typed kinds exactly:

```text
RUN_STARTED/Search notice -> status
REPOSITORY_DETECTED -> github_repository
PLAN_CREATED -> todo_list
TASK_STARTED/COMPLETED/SKIPPED/FAILED -> task_status
SOURCES_COLLECTED -> sources
SUMMARY_DELTA -> task_summary_chunk
TASK_RETRY_SCHEDULED -> task_retry
REPORT_NOTE_CREATED -> report_note
REPORT_GENERATED -> final_report
RUN_COMPLETED -> done
RUN_FAILED/RUN_REJECTED -> error
```

Every projected event includes `run_id`, `schema_version`, and `sequence`, plus current `step`, `stream_token`, note, `source_strategy`, and repository fields where applicable. Never project internal worker markers or raw source bodies.

- [ ] **Step 4: Reduce HarnessRunner to a facade**

The class retains only application service, repository, a bounded stream executor, and queue capacity. `run()` calls `application.execute()`. `stream()` submits that same method once with a queue observer and yields projected events until the future completes. `load_record()` delegates to the repository.

On generator close, request cooperative cancellation and do not start new operations. Do not wait indefinitely for an in-flight 0.2.9 LLM call.

Delete `_ingest_stream_event()`, `_coerce_task_id()`, `_optional_str()`, duplicated policy/compression/evaluation methods, and the second mutable stream state.

- [ ] **Step 5: Update the HTTP composition root and error mapping**

`create_app()` keeps accepting a runner-compatible dependency. Map typed run status/errors to 400, 403, 404/409, and 500 as defined by the design. Add `GET /runs/{run_id}` and retain `GET /harness/runs/{run_id}` as a deprecated alias.

Both streaming routes use one shared SSE iterator helper rather than duplicate exception handling.

- [ ] **Step 6: Type the frontend envelope without changing UI behavior**

```typescript
export interface ResearchStreamEvent {
  type: string;
  run_id: string;
  schema_version?: number;
  sequence?: number;
  status?: string;
  detail?: string;
  [key: string]: unknown;
}
```

Keep termination on `done` or `error`. Do not change the large dirty `App.vue` unless a compilation error proves it is required.

- [ ] **Step 7: Verify real facade, API, frontend, and full backend tests**

Run focused facade/API tests, complete backend tests, and `npm run build`.

---

### Task 6: Govern actual LLM, Search, GitHub, and Note operations

**Files:**
- Create: `backend/src/research/operations.py`
- Create: `backend/src/research/adapters.py`
- Modify: `backend/src/harness/policy.py`
- Modify: `backend/src/agent.py`
- Modify: `backend/src/services/search.py`
- Modify: `backend/src/services/note_agent.py`
- Modify: `backend/src/services/github_research.py`
- Test: `backend/tests/test_governed_operations.py`
- Modify: `backend/tests/test_github_research.py`

**Interfaces:**
- Consumes: session, cancellation, existing HarnessPolicy decisions, hello-agents LLM/SearchTool/NoteTool and GitHub client.
- Produces: typed operation contexts, middleware, and adapters whose existing business methods remain typed.

- [ ] **Step 1: Write deny-before-side-effect and safe-event tests**

```python
@pytest.mark.parametrize(
    ("capability", "operation_name"),
    [
        ("llm:invoke", "planner.complete"),
        ("search:web", "search.execute"),
        ("github:read", "github.collect"),
        ("notes:write", "notes.create"),
    ],
)
def test_denied_operation_never_calls_adapter(capability, operation_name, session):
    delegate = Mock(return_value="result")
    operations = GovernedOperations(session, DenyAuthorizer(capability))
    with pytest.raises(PermissionError):
        operations.call(capability, operation_name, delegate, resource={"kind": "safe"})
    delegate.assert_not_called()


def test_operation_events_exclude_sensitive_payload(session):
    operations = GovernedOperations(session, AllowAuthorizer())
    operations.call(
        "search:web", "search.execute", lambda: {"raw": "body"},
        resource={"query_hash": "abc", "token": "must-not-appear"},
    )
    serialized = json.dumps([event.as_dict() for event in session.events])
    assert "must-not-appear" not in serialized
    assert '"raw": "body"' not in serialized
```

Add one test each for cancellation/deadline, Note read versus write capability, and Perplexity requiring `search:premium` at the real search operation.

- [ ] **Step 2: Run governed-operation tests and verify RED**

Expected: operations and adapters modules do not exist.

- [ ] **Step 3: Implement shared middleware semantics without a super gateway**

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class OperationContext:
    run_id: str
    operation_id: str
    task_id: int | None
    attempt: int
    permission_mode: str
    caller_mode: str


class GovernedOperations:
    def call(
        self,
        capability: str,
        operation_name: str,
        operation: Callable[[], T],
        *,
        resource: Mapping[str, object],
        task_id: int | None = None,
        attempt: int = 1,
    ) -> T:
        raise NotImplementedError

    def stream(
        self,
        capability: str,
        operation_name: str,
        operation: Callable[[], Iterator[str]],
        *,
        resource: Mapping[str, object],
        task_id: int | None = None,
        attempt: int = 1,
    ) -> Iterator[str]:
        raise NotImplementedError
```

The fixed order is authorize, cancellation/deadline check, safe start event, typed delegate, duration, safe completed/failed event. Only allowlisted resource keys are recorded; operation results are never serialized into events.

- [ ] **Step 4: Wrap hello-agents LLM at its public methods**

`GovernedHelloAgentsLLM` delegates all attributes but explicitly wraps `invoke()` and `stream_invoke()`. It does not access `_client`, `_history`, or other private members. Assign one role name per wrapper (`planner`, `summarizer`, `reporter`).

- [ ] **Step 5: Replace the global search tool with an injected adapter**

Create one `HelloAgentsSearchAdapter` per coordinator/run. `dispatch_search()` accepts that typed adapter and operation middleware. Preserve retry, fallback, cache, and structured-result behavior. Cache events contain only query hash/backend/hit status.

- [ ] **Step 6: Wrap GitHub and Note operations**

`GitHubResearchClient` remains the HTTP implementation but is called through a `GitHubPort` adapter before network access. Rename `NoteSubAgent` conceptually to `NoteToolAdapter`, retain `NoteSubAgent = NoteToolAdapter` for compatibility, and wrap each actual `NoteTool.run()` call with the correct read/write capability.

- [ ] **Step 7: Move capability decisions to the real operation**

Retain command-level preflight for fast rejection, but add `HarnessPolicy.authorize_operation()`. Add allowed capabilities `llm:invoke` and the current read-only operations. `ask` remains blocking because no approval workflow exists.

- [ ] **Step 8: Verify operation, policy, GitHub, coordinator, and full backend tests**

Run focused tests, then the complete suite. Confirm no test asserts only that a mock was called; every mock assertion must prove that authorization prevented or permitted a real boundary.

---

### Task 7: Evaluation separation, real framework contract, dependency lock, and documentation cleanup

**Files:**
- Create: `backend/src/research/evaluation.py`
- Modify: `backend/src/harness/evaluator.py`
- Modify: `backend/src/harness/scenarios.py`
- Delete: `backend/src/harness/context_manager.py`
- Delete: `backend/src/harness/event_bus.py`
- Delete: `backend/src/harness/replay.py`
- Modify: `backend/tests/conftest.py`
- Create: `backend/tests/test_hello_agents_contract.py`
- Modify: `backend/tests/test_evaluator.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE_OPTIMIZED.md`
- Modify: `docs/TECHNICAL_DEEP_DIVE.md`

**Interfaces:**
- Consumes: persisted `RunSnapshot` and existing scoring rules.
- Produces: offline `ResearchAssessment`, real hello-agents 0.2.9 contract coverage, reproducible lock, updated architecture documentation.

- [ ] **Step 1: Write offline-evaluation and real-framework contract tests**

```python
def test_evaluation_does_not_change_canonical_snapshot(snapshot):
    before = snapshot.as_dict()
    assessment = OfflineEvaluationService().evaluate(snapshot)
    assert snapshot.as_dict() == before
    assert assessment.run_id == snapshot.run_id


def test_real_hello_agents_version_and_public_contract():
    from importlib.metadata import version
    from hello_agents import HelloAgentsLLM, ToolAwareSimpleAgent
    from hello_agents.tools import SearchTool

    assert version("hello-agents") == "0.2.9"
    assert callable(getattr(HelloAgentsLLM, "invoke"))
    assert callable(getattr(HelloAgentsLLM, "stream_invoke"))
    assert callable(getattr(ToolAwareSimpleAgent, "run"))
    assert callable(getattr(ToolAwareSimpleAgent, "stream_run"))
    assert callable(getattr(SearchTool, "run"))
```

- [ ] **Step 2: Run the two tests and verify the intended failures**

Expected: offline evaluation module is missing; the framework contract test must reveal the current global fake if `conftest.py` still injects it.

- [ ] **Step 3: Move quality scoring off the request path**

Create immutable `ResearchAssessment` with its own timestamp, schema version, score, and findings. `OfflineEvaluationService.evaluate(snapshot)` reproduces existing quality findings but cannot change run status or report. The application result sets `evaluation_status="pending"`; it does not synchronously score the run.

`harness/evaluator.py` re-exports compatibility names and delegates record evaluation to the offline service.

- [ ] **Step 4: Remove obsolete Harness workflow modules**

After confirming there are no runtime imports, delete the one-method `ContextManager`, collector-only `InMemoryEventBus`, and non-replay JSON loader. Keep scenarios under evaluation/benchmark ownership and re-export only for the deprecated endpoint.

- [ ] **Step 5: Stop globally replacing installed framework modules in tests**

Change `conftest.py` to import installed `loguru` and `hello_agents` normally. Only create a minimal fallback when `ModuleNotFoundError` is actually raised. Unit tests inject fake coordinator/ports at project boundaries instead of mutating global `sys.modules`.

Run the previous order-sensitive test combination and confirm collection succeeds:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_harness_api.py tests/test_policy.py tests/test_evaluator.py -q
```

- [ ] **Step 6: Regenerate and freeze the dependency lock**

Before locking, replace the ineffective single-package declaration with explicit module/package discovery:

```toml
[tool.setuptools]
py-modules = ["agent", "config", "main", "models", "prompts", "utils"]

[tool.setuptools.packages.find]
where = ["src"]
include = ["research*", "harness*", "services*"]
```

Run:

```powershell
uv lock
uv sync --frozen --group dev
.\.venv\Scripts\python.exe -c "from importlib.metadata import version; assert version('hello-agents') == '0.2.9'"
```

Verify `uv.lock` resolves hello-agents 0.2.9 and the root package metadata specifies `==0.2.9`.

- [ ] **Step 7: Rewrite architecture documentation**

Update README and both architecture documents to describe the application service, canonical session, typed operation ports, durable terminal semantics, compatibility facade, offline assessment, and 0.2.9 limitations. Remove statements claiming Harness is an independent governance runtime or that stream/follow-up integration is future work.

- [ ] **Step 8: Run full verification**

Run, in order:

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src
cd ..\frontend
npm run build
```

Also run `git diff --check`, inspect `git status --short`, confirm the exposed benchmark artifacts remain untracked and ignored, and search the new snapshot/event fixtures for `llm_api_key`, `github_token`, and known secret sentinels.

- [ ] **Step 9: Perform final whole-change code review**

Review the complete diff against `docs/superpowers/specs/2026-07-19-integrated-research-execution-design.md`. Fix every Critical or Important finding, rerun covering tests, then rerun the full verification commands before reporting completion.
