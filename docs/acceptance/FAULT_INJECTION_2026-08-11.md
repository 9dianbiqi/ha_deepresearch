# Runtime fault-injection acceptance

Date: 2026-08-11 (Asia/Shanghai)

## Scope

Deterministic local fault tests exercise the existing `FileRunRepository`,
`ResearchApplicationService`, `HarnessRunner`, and SSE cancellation boundary.
The process tests launch an independent Python child, wait for an atomically
written checkpoint marker, terminate the child with an OS process kill, and
recover the same run from a fresh Python process. No network, model key, or
source content is used.

## Results

| Acceptance case | Result | Evidence |
| --- | --- | --- |
| Real subprocess kill/restart | PASS | planning checkpoint recovered in a new process |
| Planning crash | PASS | pending tasks completed after recovery |
| Parallel partial recovery | PASS | confirmed tasks started once; only the in-flight task retried |
| Safe search before/after crash | PASS | completed search retained; active search replayed with a new operation attempt |
| Report-stream crash | PASS | recovery resumed from `evidence_completed`; research was not rerun |
| Atomic interruption / truncated JSON | PASS | previous checkpoint remained readable; half-written record rejected |
| SSE disconnect | PASS | run persisted as `cancelled`; no `completed` event |
| Two consecutive crashes | PASS | same run recovered twice; recovery attempt IDs were distinct |
| Unknown side effect | PASS | recovery returned `run_not_resumable`; coordinator replay was not reached |

## Invariants

- Only validated and resumable checkpoints were accepted.
- Completed task and confirmed safe-operation effects were not duplicated.
- Safe in-flight search was replayable; unknown side effects failed closed.
- Report failure did not replay completed research, and no false `completed` state was observed.
- Recovery event payloads and bounded `metrics.execution_attempts` distinguish attempts.

## Automated verification

- Fault injection: `10 passed`
- Recovery/SSE/telemetry targeted regression: `113 passed`
- Backend suite: `415 passed, 2 skipped`
- Ruff: passed
- mypy: passed
- compileall: passed
- Frontend build: passed

No secrets or sensitive runtime payloads are included in this record.
