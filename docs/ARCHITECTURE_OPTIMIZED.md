# Deep Research Harness Architecture

## Definition

Harness is not a second business workflow.  
Harness is the runtime governance layer around the Deep Research workflow.

The stable execution shape is:

`FastAPI -> request normalization -> HarnessRunner -> DeepResearchAgent -> compression / evaluation / persistence`

`DeepResearchAgent` remains responsible for research execution.  
`HarnessRunner` is responsible for run governance.

## Runtime Responsibilities

### HTTP layer

File: [backend/src/main.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/main.py)

- `/research` is the public business entrypoint.
- `/research` now normalizes input and delegates execution to `HarnessRunner`.
- `/harness/run` is retained as an internal engineering entrypoint for controlled runs.
- `/harness/runs/{run_id}` exposes persisted run records.
- `/harness/scenarios` exposes curated regression topics.
- `main.py` only performs HTTP normalization and response shaping, not workflow orchestration.

### Research workflow layer

Files:

- [backend/src/agent.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/agent.py)
- [backend/src/services/planner.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/services/planner.py)
- [backend/src/services/search.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/services/search.py)
- [backend/src/services/summarizer.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/services/summarizer.py)
- [backend/src/services/reporter.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/services/reporter.py)

- `DeepResearchAgent` remains the single research executor.
- Planning, search, summarization, and reporting stay in the existing service layer.
- The agent does not own persistence, policy, or replay concerns.

### Harness governance layer

Files:

- [backend/src/harness/runner.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/runner.py)
- [backend/src/harness/policy.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/policy.py)
- [backend/src/harness/context_manager.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/context_manager.py)
- [backend/src/harness/compressor.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/compressor.py)
- [backend/src/harness/evaluator.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/evaluator.py)
- [backend/src/harness/recorder.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/recorder.py)

- `HarnessRunner` orchestrates the fixed lifecycle:
  `normalize request -> policy check -> execute agent -> compress context -> evaluate -> persist`
- `HarnessPolicy` evaluates capability-oriented permissions such as `research:run`, `search:web`, `search:premium`, `notes:read`, `notes:write`, and `report:export`.
- `ContextManager` produces compressed follow-up context after execution.
- `ContextCompressor` emits two outputs:
  - `run_summary` for replay and result display
  - `reasoning_memory` for future follow-up research
- `RuleBasedEvaluator` can evaluate either an in-memory run context or a persisted run record.
- `JsonlRunRecorder` persists:
  - a final run record
  - an append-only run index
  - a per-run event log

## Compressed Context Contract

Compressed context is intended for follow-up research, not only record display.

The stable payload contains:

- `run_summary`
- `reasoning_memory`

`run_summary` includes:

- `completed_tasks`
- `incomplete_tasks`
- `report_excerpt`

`reasoning_memory` includes:

- `key_findings`
- `key_sources`
- `open_questions`

## Policy Model

The current policy layer is intentionally lightweight.

- It uses capability decisions rather than raw config checks.
- It keeps the three outcomes `allow`, `deny`, and `ask`.
- It does not implement an approval UI yet.
- In strict mode, `ask` is treated as a blocking outcome.

This keeps the model compatible with future capability growth without turning the project into a heavy code-agent runtime.

## Persistence and Replay

Harness persistence is split by purpose:

- final record for lookup and auditing
- event log for replay and debugging
- compressed context for future research reuse
- evaluation result for regression analysis

This separation prevents the persisted output model from becoming tightly coupled to live execution code.

## Near-Term Direction

1. Add a streaming harness path so `/research/stream` can also inherit run governance and recording.
2. Extend scenario coverage for sparse-source, premium-search, and disabled-notes cases.
3. Reuse `reasoning_memory` as follow-up input for multi-stage research sessions.
