"""Process-level crash recovery and durable-boundary acceptance tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from uuid import uuid4

import pytest

import research.repository as repository_module
from config import Configuration
from harness import HarnessRunner
from models import ResearchState, TodoItem
from research.application import ResearchApplicationService
from research.contracts import EventKind, ResearchCommand, RunStatus
from research.repository import (
    CorruptRunRecordError,
    FileRunRepository,
)
from research.session import CheckpointPersistenceError, RunSession

VALID_REPORT = (
    "# Crash recovery report\n\n"
    "## Task 1\n"
    "The recovered task contains a complete deterministic summary with evidence, "
    "limitations, and a clear conclusion for the requested research topic.\n\n"
    "## Task 2\n"
    "The second task confirms the remaining evidence and explains what changed "
    "during recovery without repeating any already confirmed research effect.\n\n"
    "## Task 3\n"
    "The partial parallel task is completed exactly once after restart, with its "
    "replayed safe search boundary recorded in the run audit.\n\n"
    "## Sources\n"
    "Source: https://example.test/recovery-reference"
)


# This script deliberately has a blocking phase.  The parent test terminates
# it with Popen.kill(), then starts a fresh Python process against the same
# FileRunRepository.  No state is passed through Python memory between phases.
SUBPROCESS_SCRIPT = textwrap.dedent(
    r'''
    import json
    import os
    import sys
    import time
    from pathlib import Path
    from uuid import UUID

    from config import Configuration
    from models import ResearchState, TodoItem
    from research.application import RecoveryFailure, ResearchApplicationService
    from research.contracts import EventKind, ResearchCommand
    from research.operations import OperationSpec
    from research.repository import FileRunRepository
    from research.session import RunSession

    ROOT = Path(os.environ["FAULT_ROOT"])
    MARKER = Path(os.environ["FAULT_MARKER"])
    RUN_ID = os.environ["FAULT_RUN_ID"]
    MODE = os.environ["FAULT_MODE"]
    REPORT = (
        "# Crash recovery report\n\n"
        "## Task 1\n"
        "The recovered task contains a complete deterministic summary with evidence, "
        "limitations, and a clear conclusion for the requested research topic.\n\n"
        "## Task 2\n"
        "The second task confirms the remaining evidence and explains what changed "
        "during recovery without repeating any already confirmed research effect.\n\n"
        "## Task 3\n"
        "The partial parallel task is completed exactly once after restart, with its "
        "replayed safe search boundary recorded in the run audit.\n\n"
        "## Sources\n"
        "Source: https://example.test/recovery-reference"
    )
    SAFE_OPERATION_ID = "11111111111111111111111111111111"

    class AllowPolicy:
        def evaluate(self, command):
            del command
            return [{
                "capability": "research:run",
                "outcome": "allow",
                "reason": "fault test",
            }]

        def assert_executable(self, decisions):
            del decisions

    def mark(name):
        MARKER.write_text(name, encoding="utf-8")

    def block():
        while True:
            time.sleep(0.05)

    def tasks(count):
        return [
            TodoItem(id=index, title=f"Task {index}", intent="fault", query="fault")
            for index in range(1, count + 1)
        ]

    def finish_pending(session):
        for task in session.state.todo_items:
            if task.status == "pending":
                session.start_task(task.id)
                session.complete_task(
                    task.id,
                    summary="durable recovered evidence",
                    sources_summary="recovered source",
                )
        session.set_report(REPORT)

    def safe_spec(*, operation_attempt=1, task_id=1):
        return OperationSpec(
            operation_name="search.execute",
            capabilities=("search:web",),
            resource={"query_hash": "a" * 64, "backend": "fault-test"},
            operation_id=SAFE_OPERATION_ID,
            task_id=task_id,
            operation_attempt=operation_attempt,
        )

    class Coordinator:
        def execute(self, session, prior_context):
            del prior_context
            if MODE == "planning_crash":
                session.install_plan(tasks(2))
                session.persist_checkpoint("planning_completed")
                mark("planning")
                block()
            elif MODE == "parallel_crash":
                session.install_plan(tasks(3))
                session.persist_checkpoint("planning_completed")
                for task_id in (1, 2):
                    session.start_task(task_id)
                    session.complete_task(
                        task_id,
                        summary="confirmed parallel evidence",
                        sources_summary="parallel source",
                    )
                session.start_task(3)
                session.start_operation(safe_spec(task_id=3))
                session.persist_checkpoint("research_tasks_progress")
                mark("parallel")
                block()
            elif MODE == "report_crash":
                session.install_plan(tasks(2))
                for task_id in (1, 2):
                    session.start_task(task_id)
                    session.complete_task(
                        task_id,
                        summary="confirmed report evidence",
                        sources_summary="report source",
                    )
                session.persist_checkpoint("evidence_completed")
                session.record_llm_telemetry({
                    "role": "reporter",
                    "mode": "stream",
                    "stream_completed": False,
                    "finish_reason": "length",
                })
                session.set_report("# partial")
                session.persist_checkpoint("report_before_generation")
                mark("report")
                block()
            elif MODE == "repeat_crash_1":
                session.install_plan(tasks(2))
                session.start_task(1)
                session.complete_task(
                    1,
                    summary="first confirmed evidence",
                    sources_summary="first source",
                )
                session.persist_checkpoint("research_tasks_progress")
                mark("repeat-1")
                block()
            elif MODE == "safe_before_crash":
                session.install_plan(tasks(1))
                session.start_task(1)
                operation = safe_spec()
                session.start_operation(operation)
                session.complete_operation(operation, duration_seconds=0.01)
                session.complete_task(
                    1,
                    summary="safe search completed before crash",
                    sources_summary="safe source",
                )
                session.persist_checkpoint("evidence_completed")
                mark("safe-before")
                block()
            elif MODE == "safe_active_crash":
                session.install_plan(tasks(1))
                session.start_task(1)
                session.start_operation(safe_spec())
                session.persist_checkpoint("research_tasks_progress")
                mark("safe-active")
                block()
            elif MODE == "unknown_side_effect_crash":
                session.install_plan(tasks(1))
                session.start_task(1)
                operation = OperationSpec(
                    operation_name="external.commit",
                    capabilities=("notes:write",),
                    resource={"action": "create", "note_kind": "task"},
                    operation_id=SAFE_OPERATION_ID,
                    task_id=1,
                )
                session.start_operation(operation)
                session.persist_checkpoint("research_tasks_progress")
                mark("unknown")
                block()
            else:
                raise RuntimeError(f"unsupported execute mode: {MODE}")

        def resume(self, session, prior_context):
            del prior_context
            if MODE == "planning_complete":
                finish_pending(session)
            elif MODE == "parallel_complete":
                assert [task.status for task in session.state.todo_items] == [
                    "completed", "completed", "pending"
                ]
                finish_pending(session)
            elif MODE == "report_complete":
                assert all(task.status == "completed" for task in session.state.todo_items)
                session.set_report(REPORT)
            elif MODE == "repeat_crash_2":
                assert session.state.todo_items[0].status == "completed"
                session.start_task(2)
                session.complete_task(
                    2,
                    summary="second confirmed evidence",
                    sources_summary="second source",
                )
                session.persist_checkpoint("evidence_completed")
                mark("repeat-2")
                block()
            elif MODE == "repeat_complete":
                finish_pending(session)
            elif MODE == "safe_before_complete":
                assert sum(
                    event.kind is EventKind.OPERATION_STARTED
                    for event in session.events
                ) == 1
                session.set_report(REPORT)
            elif MODE == "safe_active_complete":
                assert session.metrics["operations"]["uncertain"] == 1
                replay = safe_spec(operation_attempt=2)
                session.start_operation(replay)
                session.complete_operation(replay, duration_seconds=0.01)
                session.complete_task(
                    1,
                    summary="safe search replayed after crash",
                    sources_summary="safe source",
                )
                session.set_report(REPORT)
            elif MODE == "unknown_side_effect_resume":
                raise AssertionError("unknown side effect must not reach coordinator")
            else:
                raise RuntimeError(f"unsupported resume mode: {MODE}")

    repository = FileRunRepository(ROOT)
    service = ResearchApplicationService(
        coordinator=Coordinator(),
        repository=repository,
        policy=AllowPolicy(),
    )
    command = ResearchCommand(
        topic="fault injection",
        config=Configuration(enable_notes=False),
        run_id=RUN_ID,
    )

    if MODE.endswith("_resume"):
        try:
            result = service.resume(RUN_ID)
        except RecoveryFailure as error:
            print(json.dumps({"error_code": error.code}, ensure_ascii=False), flush=True)
            raise SystemExit(0)
    elif MODE in {
        "planning_complete", "parallel_complete", "report_complete",
        "repeat_crash_2", "repeat_complete", "safe_before_complete",
        "safe_active_complete", "unknown_side_effect_resume",
    }:
        result = service.resume(RUN_ID)
    else:
        result = service.execute(command)

    snapshot = repository.load(RUN_ID)
    print(json.dumps({
        "status": result.status.value,
        "run_id": result.run_id,
        "task_starts": sum(
            event.kind is EventKind.TASK_STARTED for event in snapshot.events
        ),
        "task_start_counts": {
            str(task_id): sum(
                event.kind is EventKind.TASK_STARTED and event.task_id == task_id
                for event in snapshot.events
            )
            for task_id in range(1, 4)
        },
        "operation_starts": sum(
            event.kind is EventKind.OPERATION_STARTED for event in snapshot.events
        ),
        "recovery_events": sum(
            event.kind is EventKind.RUN_RECOVERY_STARTED for event in snapshot.events
        ),
        "attempts": snapshot.as_dict()["metrics"].get("execution_attempts", []),
        "checkpoint_phase": (
            snapshot.checkpoint_state.get("phase")
            if snapshot.checkpoint_state else None
        ),
    }, ensure_ascii=False), flush=True)
    '''
)


def _child_environment(
    *,
    root: Path,
    marker: Path,
    run_id: str,
    mode: str,
) -> dict[str, str]:
    """Build a child environment with only repository/test import paths."""
    environment = os.environ.copy()
    source_path = str(Path(__file__).resolve().parents[1] / "src")
    current_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not current_python_path
        else source_path + os.pathsep + current_python_path
    )
    environment.update(
        {
            "FAULT_ROOT": str(root),
            "FAULT_MARKER": str(marker),
            "FAULT_RUN_ID": run_id,
            "FAULT_MODE": mode,
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def _start_blocked_child(
    *,
    root: Path,
    marker: Path,
    run_id: str,
    mode: str,
) -> subprocess.Popen[str]:
    """Start one deterministic child and wait for its durable marker."""
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", SUBPROCESS_SCRIPT],
        cwd=Path(__file__).resolve().parents[1],
        env=_child_environment(root=root, marker=marker, run_id=run_id, mode=mode),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if marker.exists():
            return child
        if child.poll() is not None:
            stdout, stderr = child.communicate(timeout=2)
            raise AssertionError(
                f"fault child exited before marker ({mode}): {stdout}\n{stderr}"
            )
        time.sleep(0.02)
    child.kill()
    stdout, stderr = child.communicate(timeout=2)
    raise AssertionError(
        f"fault child did not reach marker ({mode}): {stdout}\n{stderr}"
    )


def _kill_child(child: subprocess.Popen[str]) -> None:
    """Terminate a real child process and require a clean OS-level wait."""
    if child.poll() is None:
        child.kill()
    child.wait(timeout=5)


def _resume_child(
    *,
    root: Path,
    marker: Path,
    run_id: str,
    mode: str,
) -> dict[str, object]:
    """Restart recovery in a fresh process and decode its audit summary."""
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", SUBPROCESS_SCRIPT],
        cwd=Path(__file__).resolve().parents[1],
        env=_child_environment(root=root, marker=marker, run_id=run_id, mode=mode),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        stdout, stderr = child.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill()
        stdout, stderr = child.communicate(timeout=5)
        raise AssertionError(f"recovery child timed out ({mode}): {stdout}\n{stderr}")
    assert child.returncode == 0, f"recovery child failed ({mode}): {stdout}\n{stderr}"
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, f"recovery child produced no result ({mode}): {stderr}"
    return json.loads(lines[-1])


def _run_kill_restart(
    *,
    initial_mode: str,
    recovery_mode: str,
    tmp_path: Path,
) -> dict[str, object]:
    """Execute one real kill/restart cycle against a temporary repository."""
    root = tmp_path / "runs"
    marker = tmp_path / f"{initial_mode}.marker"
    run_id = uuid4().hex
    child = _start_blocked_child(
        root=root,
        marker=marker,
        run_id=run_id,
        mode=initial_mode,
    )
    try:
        assert child.poll() is None
    finally:
        _kill_child(child)
    return _resume_child(
        root=root,
        marker=marker,
        run_id=run_id,
        mode=recovery_mode,
    )


@pytest.mark.fault_injection
def test_real_subprocess_kill_restart_recovers_planning_checkpoint(tmp_path: Path) -> None:
    """A real process kill leaves a durable planning checkpoint for restart."""
    result = _run_kill_restart(
        initial_mode="planning_crash",
        recovery_mode="planning_complete",
        tmp_path=tmp_path,
    )
    assert result["status"] == "completed"
    assert result["recovery_events"] == 1
    assert result["task_starts"] == 2


@pytest.mark.fault_injection
def test_real_subprocess_parallel_partial_recovery_does_not_repeat_tasks(
    tmp_path: Path,
) -> None:
    """Completed parallel work is not started again after a process crash."""
    result = _run_kill_restart(
        initial_mode="parallel_crash",
        recovery_mode="parallel_complete",
        tmp_path=tmp_path,
    )
    assert result["status"] == "completed"
    assert result["task_starts"] == 4
    assert result["task_start_counts"]["1"] == 1
    assert result["task_start_counts"]["2"] == 1
    assert result["task_start_counts"]["3"] == 2


@pytest.mark.fault_injection
def test_real_subprocess_report_crash_recovers_report_only(tmp_path: Path) -> None:
    """A report stream crash resumes from evidence without repeating research."""
    result = _run_kill_restart(
        initial_mode="report_crash",
        recovery_mode="report_complete",
        tmp_path=tmp_path,
    )
    assert result["status"] == "completed"
    assert result["task_starts"] == 2
    assert result["checkpoint_phase"] == "evidence_completed"


@pytest.mark.fault_injection
def test_two_real_crashes_recover_and_record_distinct_attempts(tmp_path: Path) -> None:
    """Two independent restarts retain the same run and distinct attempt IDs."""
    root = tmp_path / "runs"
    marker_one = tmp_path / "repeat-one.marker"
    marker_two = tmp_path / "repeat-two.marker"
    run_id = uuid4().hex
    first = _start_blocked_child(
        root=root,
        marker=marker_one,
        run_id=run_id,
        mode="repeat_crash_1",
    )
    _kill_child(first)
    second = _start_blocked_child(
        root=root,
        marker=marker_two,
        run_id=run_id,
        mode="repeat_crash_2",
    )
    _kill_child(second)
    result = _resume_child(
        root=root,
        marker=marker_two,
        run_id=run_id,
        mode="repeat_complete",
    )
    assert result["status"] == "completed"
    assert result["recovery_events"] == 2
    assert result["task_starts"] == 2
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    assert len(attempts) == 2
    assert len({item["attempt_id"] for item in attempts}) == 2
    assert len({item["execution_attempt_id"] for item in attempts}) == 2


@pytest.mark.fault_injection
@pytest.mark.parametrize(
    ("initial_mode", "recovery_mode", "expected_operation_starts"),
    [
        ("safe_before_crash", "safe_before_complete", 1),
        ("safe_active_crash", "safe_active_complete", 2),
    ],
)
def test_safe_search_operation_crash_boundary_is_replayable(
    tmp_path: Path,
    initial_mode: str,
    recovery_mode: str,
    expected_operation_starts: int,
) -> None:
    """Safe search work is retained or replayed without unsafe side effects."""
    result = _run_kill_restart(
        initial_mode=initial_mode,
        recovery_mode=recovery_mode,
        tmp_path=tmp_path,
    )
    assert result["status"] == "completed"
    assert result["operation_starts"] == expected_operation_starts


@pytest.mark.fault_injection
def test_unknown_side_effect_after_real_crash_fails_closed(tmp_path: Path) -> None:
    """An unknown side-effect outcome is never automatically replayed."""
    root = tmp_path / "runs"
    marker = tmp_path / "unknown.marker"
    run_id = uuid4().hex
    child = _start_blocked_child(
        root=root,
        marker=marker,
        run_id=run_id,
        mode="unknown_side_effect_crash",
    )
    _kill_child(child)
    result = _resume_child(
        root=root,
        marker=marker,
        run_id=run_id,
        mode="unknown_side_effect_resume",
    )
    assert result == {"error_code": "run_not_resumable"}


def test_atomic_checkpoint_interrupt_preserves_previous_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed replace does not destroy the last durable checkpoint."""
    repository = FileRunRepository(tmp_path / "runs")
    command = ResearchCommand(
        topic="atomic checkpoint",
        config=Configuration(enable_notes=False),
    )
    session = RunSession(
        command=command,
        state=ResearchState(research_topic=command.topic),
        checkpoint_writer=repository.save_checkpoint,
    )
    session.start()
    session.persist_checkpoint("run_created")
    original = repository.load(command.run_id)

    def interrupted_replace(_source: object, _target: object) -> None:
        raise OSError("simulated replace interruption")

    monkeypatch.setattr(repository_module.os, "replace", interrupted_replace)
    with pytest.raises(CheckpointPersistenceError):
        session.persist_checkpoint("planning_completed")
    assert repository.load(command.run_id).checkpoint == original.checkpoint
    assert not tuple(repository.runs_dir.glob("*.tmp"))


def test_half_written_checkpoint_is_rejected_as_corrupt(tmp_path: Path) -> None:
    """A truncated JSON file cannot become a recovery target."""
    repository = FileRunRepository(tmp_path / "runs")
    run_id = uuid4().hex
    repository.runs_dir.mkdir(parents=True, exist_ok=True)
    path = repository.runs_dir / f"{run_id}.json"
    path.write_text('{"schema_version":1,"snapshot":', encoding="utf-8")
    with pytest.raises(CorruptRunRecordError):
        repository.load(run_id)


def test_sse_disconnect_persists_cancelled_run_without_false_completion(
    tmp_path: Path,
) -> None:
    """Closing a live SSE iterator cancels the run and durably records it."""
    repository = FileRunRepository(tmp_path / "runs")

    class BlockingCoordinator:
        def execute(self, session: RunSession, prior_context: object) -> None:
            del prior_context
            session.install_plan(
                [TodoItem(id=1, title="Task 1", intent="intent", query="query")]
            )
            while not session.cancellation.is_cancelled:
                session.wait(0.02)
            session.raise_if_cancelled()

    class AllowPolicy:
        def evaluate(self, command: ResearchCommand) -> list[dict[str, str]]:
            del command
            return [
                {
                    "capability": "research:run",
                    "outcome": "allow",
                    "reason": "fault test",
                }
            ]

        def assert_executable(self, decisions: object) -> None:
            del decisions

    service = ResearchApplicationService(
        coordinator=BlockingCoordinator(),
        repository=repository,
        policy=AllowPolicy(),
    )
    runner = HarnessRunner(application=service, repository=repository)
    command = ResearchCommand(
        topic="SSE disconnect",
        config=Configuration(enable_notes=False),
    )
    stream = runner.stream(command)
    try:
        assert next(stream)["type"] == "status"
        stream.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = repository.load(command.run_id)
            if snapshot.status is RunStatus.CANCELLED:
                break
            time.sleep(0.02)
        snapshot = repository.load(command.run_id)
        assert snapshot.status is RunStatus.CANCELLED
        assert not any(event.kind is EventKind.RUN_COMPLETED for event in snapshot.events)
    finally:
        stream.close()
        runner._executor.shutdown(wait=True, cancel_futures=True)
