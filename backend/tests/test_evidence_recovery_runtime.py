"""Recovery preserves evidence identity through real artifact storage."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import UUID, uuid4

import pytest
from test_web_task_evidence import CapturingProvider, read

from research.artifacts import FileArtifactStore
from research.evidence_normalization import normalize_collections
from research.evidence_recovery import (
    EvidenceRecoveryError,
    persist_evidence_recovery,
    validate_evidence_recovery,
)
from research.pipeline import ResearchKernel
from research.profiles import ResearchMode, ResearchProfileRegistry
from research.sources import SourceProviderRegistry


def setup(provider):
    kernel = ResearchKernel(provider_registry=SourceProviderRegistry((provider,)))
    prepared = kernel.prepare("Hopper demand", mode=ResearchMode.WEB,
                              profile_id="web.evidence.v1", run_id=str(uuid4()))
    return kernel, prepared


@pytest.mark.parametrize("failed", [False, True])
def test_capture_and_binding_roundtrip_without_refetch(tmp_path, failed):
    provider = CapturingProvider(fallback=failed)
    kernel, prepared = setup(provider)
    collections = read(prepared, provider)
    records = normalize_collections(collections, 1200)
    prepared.start_attempt(1, 1)
    prepared.bind(task_id=1, task_attempt=1, dimension="overview", query="Hopper", evidence=records)
    prepared.accept(task_id=1, task_attempt=1)
    payload = persist_evidence_recovery(prepared, run_id=prepared.provider_context.run_id,
                                        artifact_store=FileArtifactStore(tmp_path))
    validate_evidence_recovery(payload, run_id=prepared.provider_context.run_id,
                              task_state=[{"id": 1, "status": "completed"}])
    fresh_provider = CapturingProvider(fallback=failed)
    fresh_kernel = ResearchKernel(provider_registry=SourceProviderRegistry((fresh_provider,)))
    restored = fresh_kernel.restore_prepared(payload, run_id=prepared.provider_context.run_id,
        cancellation=prepared.provider_context.cancellation, operation_scope=None,
        artifact_store=FileArtifactStore(tmp_path))
    assert restored.read_accepted(task_id=1).evidence == prepared.read_accepted(task_id=1).evidence
    assert restored.attempt_high_water(1) == 1
    before = restored.provider_context.budget.snapshot()
    read(restored, fresh_provider)
    assert fresh_provider.calls == 0
    assert restored.provider_context.budget.snapshot() == before
    assert restored.web_evidence._lock is not prepared.web_evidence._lock


@pytest.mark.parametrize("corruption", ["missing", "bytes", "profile", "identity"])
def test_recovery_rejects_corruption_without_network(tmp_path, corruption):
    provider = CapturingProvider()
    kernel, prepared = setup(provider)
    records = normalize_collections(read(prepared, provider), 1200)
    prepared.bind(task_id=1, task_attempt=1, dimension="overview", query="Hopper", evidence=records)
    store = FileArtifactStore(tmp_path)
    payload = persist_evidence_recovery(prepared, run_id=prepared.provider_context.run_id, artifact_store=store)
    if corruption in {"missing", "bytes"}:
        ref = payload["web_captures"][0]
        path = tmp_path / "artifacts" / UUID(prepared.provider_context.run_id).hex / ref["artifact_id"]
        if corruption == "missing":
            path.unlink()
        else:
            path.write_bytes(b"corrupt")
    elif corruption == "identity":
        payload["admitted_records"][0]["excerpt"] = "Different source text"
    else:
        changed = replace(prepared.profile, source_priority=("web", "different"))
        kernel = ResearchKernel(profile_registry=ResearchProfileRegistry((changed,)),
                                provider_registry=SourceProviderRegistry((provider,)))
    with pytest.raises(EvidenceRecoveryError) as exc:
        kernel.restore_prepared(payload, run_id=prepared.provider_context.run_id,
            cancellation=prepared.provider_context.cancellation, operation_scope=None,
            artifact_store=FileArtifactStore(tmp_path))
    expected = "artifact_invalid" if corruption in {"missing", "bytes"} else "profile_mismatch" if corruption == "profile" else "invalid"
    assert exc.value.code == f"evidence_recovery_{expected}"
    assert provider.calls == 1


def test_export_does_not_wait_for_inflight_page(tmp_path):
    entered, release = Event(), Event()

    class SlowProvider(CapturingProvider):
        def capture_search_result(self, result, context):
            entered.set()
            assert release.wait(5)
            return super().capture_search_result(result, context)

    provider = SlowProvider()
    _, prepared = setup(provider)
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(read, prepared, provider)
        assert entered.wait(2)
        try:
            snapshot = pool.submit(prepared.export_recovery_state).result(timeout=2)
            assert snapshot["web_captures"] == ()
        finally:
            release.set()
        future.result(timeout=3)
    assert len(prepared.export_recovery_state()["web_captures"]) == 1


def test_github_version_and_initial_collection_survive_roundtrip(tmp_path):
    from test_task_evidence_binding import (
        _collection,
        _profile,
        _record,
        _StaticProvider,
        _target,
    )

    from research.evidence_normalization import normalize_collections

    target = _target("github", "owner/repo")
    collection = _collection("github", "owner/repo", (
        _record(target, dimension="overview", title="Repository", excerpt="Pinned source text", paragraph="p1"),
    ), resolved_version="fixed-sha")
    provider = _StaticProvider("github", ResearchMode.GITHUB, (collection,))
    profile = _profile("github", ResearchMode.GITHUB)
    kernel = ResearchKernel(profile_registry=ResearchProfileRegistry((profile,)),
                            provider_registry=SourceProviderRegistry((provider,)))
    prepared = kernel.prepare("Repository research", mode=ResearchMode.GITHUB,
                              profile_id=profile.profile_id, run_id=str(uuid4()))
    binding = kernel.bind_task_evidence(prepared, task_id=1, task_attempt=1,
                                        dimension="overview", query="source", intent="source")
    prepared.accept(task_id=1, task_attempt=1)
    payload = persist_evidence_recovery(prepared, run_id=prepared.provider_context.run_id,
                                        artifact_store=FileArtifactStore(tmp_path))
    restored = kernel.restore_prepared(payload, run_id=prepared.provider_context.run_id,
        cancellation=prepared.provider_context.cancellation, operation_scope=None,
        artifact_store=FileArtifactStore(tmp_path))
    assert restored.read_accepted(task_id=1).evidence == binding.evidence
    assert normalize_collections(restored.collections, 1200) == normalize_collections(prepared.collections, 1200)
    assert restored.provider_context.budget.snapshot() == prepared.provider_context.budget.snapshot()


def test_redirected_request_key_is_preserved(tmp_path):
    class RedirectingProvider(CapturingProvider):
        def capture_search_result(self, result, context):
            return super().capture_search_result({"url": "https://example.com/final"}, context)

    provider = RedirectingProvider()
    kernel, prepared = setup(provider)
    read(prepared, provider, url="https://example.com/start")
    payload = persist_evidence_recovery(prepared, run_id=prepared.provider_context.run_id,
                                        artifact_store=FileArtifactStore(tmp_path))
    restored = kernel.restore_prepared(payload, run_id=prepared.provider_context.run_id,
        cancellation=prepared.provider_context.cancellation, operation_scope=None,
        artifact_store=FileArtifactStore(tmp_path))
    read(restored, provider, url="https://example.com/start")
    assert provider.calls == 1


def test_incomplete_dedup_keys_and_budget_are_rejected(tmp_path):
    import copy

    provider = CapturingProvider()
    _, prepared = setup(provider)
    read(prepared, provider)
    payload = persist_evidence_recovery(prepared, run_id=prepared.provider_context.run_id,
                                        artifact_store=FileArtifactStore(tmp_path))
    for field in ("keys", "budget"):
        damaged = copy.deepcopy(payload)
        if field == "keys":
            damaged["web_record_keys"] = []
        else:
            damaged["budget"]["used"]["evidence"] = 0
        with pytest.raises(EvidenceRecoveryError):
            validate_evidence_recovery(damaged, run_id=prepared.provider_context.run_id)
