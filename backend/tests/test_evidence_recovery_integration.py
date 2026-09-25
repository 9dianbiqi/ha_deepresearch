"""Disk-backed task recovery through fresh coordinators and providers."""

import json
from uuid import uuid4

import pytest
from test_task_evidence_integration import (
    _agent,
    _config,
    _EvidenceSummarizer,
    _one_web_result,
    _Planner,
    _SearchDispatcher,
    _session,
    _StructuredReporter,
    _SupportedJudge,
)

from models import TodoItem
from research.artifacts import FileArtifactStore
from research.evidence_recovery import EvidenceRecoveryError
from research.intelligence import ResearchIntelligenceBundle
from research.operations import GovernedOperations, OperationScope, OperationSpec
from research.profiles import ResearchMode, built_in_profile_registry
from research.quality import EvidenceGateBlockedError
from research.repository import FileRunRepository
from research.session import RunSession


class ProcessStopped(BaseException):
    pass


def make_agent(root, *, search=None):
    config = _config()
    profile = built_in_profile_registry().get("web.evidence.v1")
    search = search or _SearchDispatcher(lambda *_: _one_web_result("Recovery evidence remains stable."))
    summary, judge = _EvidenceSummarizer(), _SupportedJudge()
    agent = _agent(config, profile, search, summary, judge,
                   reporter=_StructuredReporter(),
                   planner=_Planner(tuple(TodoItem(id=i, title=f"Task {i}", intent="overview", query=f"query {i}") for i in (1, 2))))
    agent._artifact_store = FileArtifactStore(root)
    return agent, summary, judge, search


def start(agent, repo):
    session = _session(agent.config, topic="Recovery evidence", mode=ResearchMode.WEB, profile_id="web.evidence.v1")
    session.checkpoint_writer = repo.save_checkpoint
    return session


def forbid(*args, **kwargs):
    pytest.fail("Recovery must not repeat completed planning, collection, or capture")


@pytest.mark.parametrize("phase", ["planning_completed", "research_tasks_progress"])
def test_new_coordinator_restores_plan_and_evidence_from_disk(tmp_path, monkeypatch, phase):
    repo = FileRunRepository(tmp_path)
    # Task 2 may start before the coordinator drains task 1's completion.
    def search_result(query, index):
        if query == "query 2":
            raise RuntimeError("Interrupted task 2 network operation")
        return _one_web_result("Recovery evidence remains stable.")
    agent, _, _, _ = make_agent(tmp_path, search=_SearchDispatcher(search_result))
    session = start(agent, repo)

    def save_then_stop(snapshot):
        repo.save_checkpoint(snapshot)
        if snapshot.checkpoint_state["phase"] == phase:
            if phase == "planning_completed" or snapshot.checkpoint_state["task_state"][0]["status"] == "completed":
                raise ProcessStopped
    session.checkpoint_writer = save_then_stop
    with pytest.raises(ProcessStopped):
        agent.execute(session, None)
    saved = FileRunRepository(tmp_path).load(session.run_id)
    recovery = saved.checkpoint_state["evidence_recovery"]
    old_ids = {item["evidence_id"] for item in recovery["admitted_records"]}
    run_json = json.dumps(saved.as_dict())
    assert "<html>" not in run_json
    assert saved.recovery_resumable is True

    fresh, summary, judge, search = make_agent(tmp_path)
    monkeypatch.setattr(fresh.planner, "plan_todo_list", forbid)
    monkeypatch.setattr(fresh._research_kernel, "prepare", forbid)
    provider = fresh._research_kernel.provider_registry.get("web")
    if old_ids:
        monkeypatch.setattr(provider, "capture_search_result", forbid)
    restored = RunSession.restore_from_snapshot(saved, checkpoint_writer=FileRunRepository(tmp_path).save_checkpoint)
    fresh.resume(restored, None)
    assert all(task.status == "completed" for task in restored.state.todo_items)
    assert len(summary.requests) == (2 if phase == "planning_completed" else 1)
    assert len(judge.calls) == len(summary.requests)
    if phase == "research_tasks_progress":
        assert all(request.title == "Task 2" for request in summary.requests)
    bundle = ResearchIntelligenceBundle.from_dict(restored.state.research_intelligence)
    assert bundle.evidence_frozen
    assert old_ids <= {record.evidence_id for record in bundle.evidence}
    latest = FileRunRepository(tmp_path).load(session.run_id)
    assert latest.checkpoint_state["phase"] == "report_generated"
    assert latest.checkpoint_state["evidence_recovery"]["budget"]["used"]["requests"] >= recovery["budget"]["used"]["requests"]


def test_restore_uses_checkpoint_tasks_and_quality_not_terminal_output(tmp_path):
    agent, _, _, _ = make_agent(tmp_path)
    session = start(agent, FileRunRepository(tmp_path))
    session.install_plan([TodoItem(id=1, title="Saved", intent="intent", query="saved query")])
    session.start_task(1)
    session.start_operation(OperationSpec(operation_name="search.execute", capabilities=("search:web",),
        resource={"query_hash": "a" * 64, "backend": "fake"}, operation_id=uuid4().hex, task_id=1))
    session.append_task_summary(1, "Partial text")
    session.metrics["task_quality"] = {"1": [{"action": "retrieve_gaps"}]}
    session.persist_checkpoint("research_tasks_progress")
    session.complete_task(1, summary="Later result", sources_summary="Later source")
    session.metrics["task_quality"] = {"1": [{"action": "accept"}]}
    FileRunRepository(tmp_path).save(session.to_snapshot())
    restored = RunSession.restore_from_snapshot(FileRunRepository(tmp_path).load(session.run_id))
    assert restored.state.todo_items[0].status == "pending"
    assert restored.state.todo_items[0].summary is None
    assert restored.metrics["task_quality"]["1"][0]["action"] == "retrieve_gaps"


def test_old_evidence_task_checkpoint_rejected_but_compatibility_not_misclassified(tmp_path):
    agent, _, _, _ = make_agent(tmp_path)
    session = start(agent, FileRunRepository(tmp_path))
    session.install_plan([TodoItem(id=1, title="Task", intent="overview", query="query")])
    session.persist_checkpoint("planning_completed")
    restored = RunSession.restore_from_snapshot(FileRunRepository(tmp_path).load(session.run_id))
    with pytest.raises(EvidenceRecoveryError, match="missing") as exc:
        agent.resume(restored, None)
    assert exc.value.code == "evidence_recovery_missing"
    restored.state.research_profile_id = "web.default.v1"
    assert agent._is_kernel_recovery_checkpoint(restored) is False


def test_no_artifact_store_marks_task_checkpoint_unrecoverable(tmp_path):
    agent, _, _, _ = make_agent(tmp_path)
    agent._artifact_store = None
    session = start(agent, FileRunRepository(tmp_path))
    saved = []
    session.checkpoint_writer = saved.append
    agent.execute(session, None)
    planning = next(item for item in saved if item.checkpoint_state["phase"] == "planning_completed")
    assert planning.recovery_resumable is False
    assert planning.checkpoint_state["recovery_blocked_reason"] == "evidence_recovery_unavailable"
    assert saved[-1].checkpoint_state["phase"] == "report_generated"


def test_known_attempts_are_not_reset_and_notes_do_not_consume_attempts(tmp_path):
    agent, _, _, _ = make_agent(tmp_path)
    session = start(agent, FileRunRepository(tmp_path))
    session.install_plan([TodoItem(id=1, title="Task", intent="overview", query="query", retry_count=1)])
    session.persist_checkpoint("planning_completed")
    session.checkpoint_state["operation_state"] = [
        {"task_id": 1, "operation_name": "notes.read", "pairing_key": ["notes", 9, 1, 1]},
        {"task_id": 1, "operation_name": "search.execute", "pairing_key": ["search", 3, 1, 1]},
    ]
    assert agent._task_attempt_high_water(session, 1, None) == 3


def test_artifact_failure_keeps_last_checkpoint(tmp_path):
    agent, _, _, _ = make_agent(tmp_path)
    repo = FileRunRepository(tmp_path)
    session = start(agent, repo)
    good_store = agent._artifact_store

    class FailingStore:
        def put(self, *args, **kwargs):
            raise OSError("Disk unavailable")
        def get(self, *args, **kwargs):
            return good_store.get(*args, **kwargs)

    def save_and_fail_store(snapshot):
        repo.save_checkpoint(snapshot)
        if snapshot.checkpoint_state["phase"] == "planning_completed":
            agent._artifact_store = FailingStore()
    session.checkpoint_writer = save_and_fail_store
    with pytest.raises(EvidenceRecoveryError):
        agent.execute(session, None)
    assert FileRunRepository(tmp_path).load(session.run_id).checkpoint_state["phase"] == "planning_completed"


def test_exhausted_attempts_never_restart_search_after_disk_restore(tmp_path, monkeypatch):
    agent, _, _, _ = make_agent(tmp_path)
    session = start(agent, FileRunRepository(tmp_path))
    session.install_plan([TodoItem(id=1, title="Task", intent="overview", query="query")])
    scope = OperationScope(operations=GovernedOperations(session, agent._operation_authorizer))
    prepared = agent._prepare_kernel_research(session, operation_scope=scope)
    agent._install_kernel_baseline(session, prepared)
    prepared.start_attempt(1, 3)
    agent._persist_research_checkpoint(session, "research_tasks_progress", prepared)
    fresh, summary, judge, search = make_agent(tmp_path)
    monkeypatch.setattr(fresh._research_kernel, "search", forbid)
    restored = RunSession.restore_from_snapshot(FileRunRepository(tmp_path).load(session.run_id))
    with pytest.raises(EvidenceGateBlockedError):
        fresh.resume(restored, None)
    assert restored.state.todo_items[0].status == "failed"
    assert not summary.requests and not judge.calls and not search.calls


def test_report_only_recovery_does_not_read_capture_artifacts(tmp_path, monkeypatch):
    agent, _, _, _ = make_agent(tmp_path)
    session = start(agent, FileRunRepository(tmp_path))
    saved = []
    session.checkpoint_writer = saved.append
    agent.execute(session, None)
    report_checkpoint = next(s for s in saved if s.checkpoint_state["phase"] == "evidence_completed")
    fresh, summary, judge, search = make_agent(tmp_path)
    monkeypatch.setattr(fresh._research_kernel, "restore_prepared", forbid)
    monkeypatch.setattr(fresh._research_kernel, "prepare", forbid)
    fresh._artifact_store = None
    restored = RunSession.restore_from_snapshot(report_checkpoint)
    fresh.resume(restored, None)
    assert restored.state.structured_report
    assert not summary.requests and not judge.calls and not search.calls
