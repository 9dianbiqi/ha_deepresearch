# Deep Research Optimized Architecture

## Overview

The project keeps the original Deep Research workflow intact and adds a lightweight harness layer around it.

The original workflow is still:

`Frontend -> FastAPI -> DeepResearchAgent -> Planner / Search / Summarizer / Reporter`

The optimized workflow adds a harness path for controlled runs:

`Frontend or API Client -> FastAPI -> HarnessRunner -> Policy -> DeepResearchAgent -> Context Compression -> Recorder / Evaluator`

This keeps the research agent simple while adding enough engineering structure for repeatability, observability, and controlled execution.

## Design Goals

- Preserve the existing research experience and streaming API.
- Add a lightweight harness instead of a heavy code-agent state machine.
- Make each run traceable with a `run_id`, policy decisions, events, and persisted output.
- Compress long research output into reusable context packages for follow-up runs.
- Support future scenario testing and regression checks without rewriting the agent core.

## Current Runtime Layers

### 1. Application Layer

File: [backend/src/main.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/main.py)

- Exposes the original `/research` and `/research/stream` endpoints.
- Adds `/harness/run` for managed research execution.
- Adds `/harness/runs/{run_id}` for replay-friendly record lookup.
- Adds `/harness/scenarios` for curated regression topics.

### 2. Agent Workflow Layer

Files:

- [backend/src/agent.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/agent.py)
- [backend/src/services/planner.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/services/planner.py)
- [backend/src/services/search.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/services/search.py)
- [backend/src/services/summarizer.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/services/summarizer.py)
- [backend/src/services/reporter.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/services/reporter.py)

- `DeepResearchAgent` still owns topic planning, task execution, source collection, task summarization, and final report generation.
- Existing business logic stays stable so the harness does not become a second workflow engine.

### 3. Harness Layer

Files:

- [backend/src/harness/runner.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/runner.py)
- [backend/src/harness/policy.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/policy.py)
- [backend/src/harness/context_manager.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/context_manager.py)
- [backend/src/harness/compressor.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/compressor.py)
- [backend/src/harness/recorder.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/recorder.py)
- [backend/src/harness/evaluator.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/evaluator.py)
- [backend/src/harness/scenarios.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness/scenarios.py)

Responsibilities:

- `HarnessRunner`: manages the run lifecycle and produces a durable run result.
- `HarnessPolicy`: performs lightweight permission checks before execution.
- `ContextManager`: finalizes reusable context after a run completes.
- `ContextCompressor`: compresses long outputs into shorter task briefs and report excerpts.
- `JsonlRunRecorder`: persists one JSON snapshot per run and appends a JSONL index.
- `RuleBasedEvaluator`: provides baseline quality checks for empty reports, incomplete tasks, and missing summaries.
- `HarnessScenario`: defines repeatable benchmark inputs.

## Why This Is Lightweight

This project is a research agent, not a general code-execution agent.

Because of that, the harness does not need:

- fine-grained shell sandbox state
- patch approval workflows
- interactive tool-by-tool pauses
- complex hierarchical memory reconstruction

Instead, the harness focuses on:

- controlled run entry
- simple policy checks
- compressed research memory
- persistent replay records
- baseline evaluation

## Recommended Next Steps

1. Route the synchronous `/research` endpoint through `HarnessRunner` so every run is recorded automatically.
2. Add a streaming harness path that mirrors `/research/stream` while recording events to the run log.
3. Expand `HarnessPolicy` from config-based checks to capability-based checks such as `notes:write`, `search:web`, and `report:publish`.
4. Add regression scenarios for broad topics, sparse-result topics, and time-sensitive topics.
5. Promote the compressed context package into follow-up research runs to support multi-stage research sessions.
