# AGENTS.md — helloagents-deepresearch

## Project overview

**FastAPI backend** (`backend/src/main.py`) + **Vue 3 frontend** (`frontend/src/main.ts`).  
A local deep research assistant: topic → plan → web search → summarize → report.  
Built on [`hello-agents==0.2.9`](https://pypi.org/project/hello-agents/) (agent orchestration framework).  
Authored by Lance Martin, MIT license.

## Commands

### Backend (Python, managed via `uv`)
```bash
cd backend
uv sync                    # install deps (uses uv.lock)
uv sync --group dev        # include ruff + mypy
uv run uvicorn src.main:app --reload --port 8000   # dev server
uv run python src/main.py                           # alt startup
uv run ruff check src/      # lint
uv run mypy src/            # typecheck (no explicit config; infer from pyproject.toml)
```
- **Source location**: `backend/src/` mapped via `[tool.setuptools.package-dir] "" = "src"`
- **Linter**: Ruff with rules E, F, I, D (Google-style docstrings), D401, T201, UP
  - Ignores: E501, D417, UP006/7/35
- **No tests exist** anywhere in the repo — no test framework, no test files.

### Frontend (TypeScript + Vue 3, Vite)
```bash
cd frontend
npm install
npm run dev                 # Vite dev server on :5174
npm run build               # vue-tsc --noEmit && vite build
```

### Running both
Start backend first (port 8000), then frontend (port 5174).  
Frontend expects `VITE_API_BASE_URL` (default `http://localhost:8000`).

## Configuration

All backend config via environment variables — see `backend/.env.example` for every option.  
Key vars:
- `SEARCH_API` — `duckduckgo` (default), `tavily`, `perplexity`, `searxng`
- `LLM_PROVIDER` — `custom` (OpenAI-compatible API), `ollama`, `lmstudio`
- `LLM_MODEL_ID`, `LLM_API_KEY`, `LLM_BASE_URL`
- `MAX_WEB_RESEARCH_LOOPS` (default 3), `FETCH_FULL_PAGE` (default True)
- `HOST` / `PORT` (default `0.0.0.0:8000`)
- `CORS_ORIGINS` (default `http://localhost:5173,http://localhost:3000`)

## Architecture

```
Frontend  --POST /research/stream-->  FastAPI  --SSE events-->  Frontend
```

1. **DeepResearchAgent** (`backend/src/agent.py`) orchestrates the workflow
2. **PlanningService** generates TODO checkpoints via LLM
3. Per task: **SearchService** (DuckDuckGo/Tavily/Perplexity/SearXNG) → **SummarizationService** (LLM)
4. **ReportingService** compiles final markdown report
5. Tasks execute in **parallel via daemon threads** (`agent.py:run_stream()`)
6. **NoteTool** persists task notes to the workspace filesystem

### Harness layer (`backend/src/harness/`)
Optional controlled execution path: `HarnessRunner` → `HarnessPolicy` → agent → `ContextCompressor` → `JsonlRunRecorder`.  
Docs in `docs/ARCHITECTURE_OPTIMIZED.md`.

## Non-obvious details

- **No README** — only doc is `docs/ARCHITECTURE_OPTIMIZED.md` (harness design)
- **No CI/CD** — no workflows, no pre-commit hooks
- **No Vue Router** — single SFC (`App.vue`), layout toggled via `v-if`
- **SSE streaming** uses native `fetch` + `ReadableStream` (axios is unused despite being a dependency)
- **Tool calls are magic strings**: agents embed `[TOOL_CALL:note:{...}]` in LLM output, parsed via regex
- **Prompts and UI are in Chinese (simplified)**
- **`output/` directory** contains example resume build scripts and generated files — not part of the core app
