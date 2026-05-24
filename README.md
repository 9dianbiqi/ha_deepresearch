# HelloAgents Deep Research

A Deep Research application built on HelloAgents with a lightweight harness runtime for run governance, replay, evaluation, and compressed research memory.

## What It Does

- Accepts an open-ended research topic
- Plans the topic into actionable sub-tasks
- Searches across multiple backends
- Summarizes each task with sources
- Produces a structured Markdown research report
- Records managed runs with `run_id`, policy decisions, event logs, evaluation results, and compressed follow-up context

## Architecture

There are two stable layers in the backend:

1. Research execution layer
   `DeepResearchAgent -> Planner / Search / Summarizer / Reporter`

2. Harness governance layer
   `HarnessRunner -> Policy -> Context Compression -> Evaluation -> Persistence`

The harness is not a second business workflow.  
It is the runtime control layer around the existing Deep Research workflow.

Detailed architecture notes live in [docs/ARCHITECTURE_OPTIMIZED.md](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/docs/ARCHITECTURE_OPTIMIZED.md).

## Backend Endpoints

- `GET /healthz`
- `POST /research`
- `POST /research/stream`
- `POST /harness/run`
- `GET /harness/runs/{run_id}`
- `GET /harness/scenarios`

`/research` is the public business entrypoint.  
`/harness/run` is the internal engineering entrypoint for controlled runs.

## Project Layout

- [backend/src/agent.py](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/agent.py): core research workflow
- [backend/src/services](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/services): planner, search, summarizer, reporter, notes, tool events
- [backend/src/harness](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/backend/src/harness): runner, policy, compression, evaluation, recording, scenarios, replay
- [frontend/src/App.vue](/F:/hello-agents-main/code/chapter14/helloagents-deepresearch/frontend/src/App.vue): interactive research workspace

## Local Development

Backend:

```bash
cd backend
uv run python src/main.py
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Default backend address:

```text
http://localhost:8000
```

## Harness Outputs

Managed runs are persisted under:

```text
./output/harness_runs
```

Each run can produce:

- one final JSON record
- one append-only JSONL index entry
- one per-run event log

## Current Focus

- keep the research agent simple
- centralize run governance in the harness layer
- reuse compressed context for follow-up research
- expand evaluation and replay over time
