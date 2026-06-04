<div align="center">

**🌐 Language** &nbsp;|&nbsp; [**中文**](#中文) &nbsp;|&nbsp; [**English**](#english)

</div>

---

<a id="中文"></a>

# HelloAgents 深度研究助手

一个基于 [HelloAgents](https://github.com/hello-agents/hello-agents) 的深度研究应用，配备轻量级治理运行时（Harness Runtime），用于运行管控、回放、评估和压缩研究记忆。

## 🎯 功能

- 接受开放式研究主题
- 将主题拆解为可执行的子任务
- 跨多个搜索引擎并行检索
- 为每个任务生成带来源的总结
- 输出结构化 Markdown 研究报告
- 记录受管控的运行：`run_id`、策略决策、事件日志、评估结果、压缩上下文，支持多轮追问

## 🧱 架构

后端有两个稳定层：

| 层级 | 职责 | 组件 |
|------|------|------|
| **研究执行层** | AI 工作流 | `DeepResearchAgent → Planner / Search / Summarizer / Reporter` |
| **治理层** | 运行管控 | `HarnessRunner → Policy → Context Compression → Evaluation → Persistence` |

> 治理层不是第二个业务工作流，而是围绕研究执行层的运行时控制层。

详细架构文档：[docs/ARCHITECTURE_OPTIMIZED.md](docs/ARCHITECTURE_OPTIMIZED.md)

## 📡 后端 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/healthz` | 健康检查 |
| `POST` | `/research` | 发起研究（同步） |
| `POST` | `/research/stream` | 发起研究（SSE 流式） |
| `POST` | `/research/continue/stream` | 继续之前的研究（流式） |
| `POST` | `/harness/run` | 内部受控运行（含策略决策） |
| `GET` | `/harness/runs/{run_id}` | 查询历史运行记录 |
| `GET` | `/harness/scenarios` | 列出评估场景 |

`/research` 是公开业务入口，`/harness/run` 是内部工程入口。

## 📂 项目结构

```
helloagents-deepresearch/
├── backend/
│   ├── src/
│   │   ├── main.py                 # FastAPI 入口
│   │   ├── agent.py                # 核心研究工作流
│   │   ├── config.py               # 配置模型
│   │   ├── models.py               # 数据模型
│   │   ├── prompts.py              # LLM 提示词
│   │   ├── services/               # 业务服务
│   │   │   ├── planner.py          # 任务规划
│   │   │   ├── search.py           # 搜索分发（含重试+降级）
│   │   │   ├── summarizer.py       # 任务总结
│   │   │   ├── reporter.py         # 报告生成
│   │   │   └── note_agent.py       # 笔记管理
│   │   └── harness/                # 治理运行时
│   │       ├── runner.py           # 受控运行器
│   │       ├── policy.py           # 权限策略
│   │       ├── evaluator.py        # 运行评分
│   │       ├── compressor.py       # 上下文压缩
│   │       ├── context_manager.py  # 上下文生命周期
│   │       ├── recorder.py         # 运行持久化
│   │       ├── replay.py           # 重放
│   │       ├── scenarios.py        # 评估场景
│   │       └── event_bus.py        # 事件总线
│   └── tests/                      # 测试套件
├── frontend/
│   ├── src/
│   │   ├── App.vue                 # Vue 单文件组件
│   │   ├── main.ts                 # 应用入口
│   │   └── services/api.ts         # SSE 流式消费
│   └── package.json
├── docs/
│   ├── ARCHITECTURE_OPTIMIZED.md
│   └── TECHNICAL_DEEP_DIVE.md
├── CHANGELOG.md
└── README.md
```

## 🚀 本地开发

### 环境要求

- Python ≥ 3.10
- Node.js ≥ 18
- [uv](https://github.com/astral-sh/uv)（推荐）或 pip

### 配置

```bash
cp backend/.env.example backend/.env
# 编辑 backend/.env，填入 API Key 和模型配置
```

### 启动

**后端：**
```bash
cd backend
uv run python src/main.py
# 或：pip install -e . && python src/main.py
```

**前端：**
```bash
cd frontend
npm install
npm run dev
```

后端默认地址：`http://localhost:8000`

## 📦 治理运行时输出

受管控的运行持久化在：

```
./output/harness_runs/
```

每次运行产生：
- 一个最终 JSON 记录
- 一条 JSONL 索引条目
- 一个事件日志

## 🎯 当前阶段

- 保持研究 Agent 简单
- 将运行管控集中到治理层
- 复用压缩上下文支持多轮追问
- 持续扩展评估和回放能力

---

<a id="english"></a>

# HelloAgents Deep Research

A Deep Research application built on [HelloAgents](https://github.com/hello-agents/hello-agents) with a lightweight harness runtime for run governance, replay, evaluation, and compressed research memory.

## 🎯 What It Does

- Accepts an open-ended research topic
- Plans the topic into actionable sub-tasks
- Searches across multiple search backends with retry + fallback
- Summarizes each task with sources
- Produces a structured Markdown research report
- Records managed runs with `run_id`, policy decisions, event logs, evaluation results, and compressed follow-up context

## 🧱 Architecture

Two stable layers in the backend:

| Layer | Responsibility | Components |
|-------|---------------|------------|
| **Research Execution** | AI workflow | `DeepResearchAgent → Planner / Search / Summarizer / Reporter` |
| **Governance** | Run control | `HarnessRunner → Policy → Context Compression → Evaluation → Persistence` |

> The harness is not a second business workflow — it is the runtime control layer around the existing Deep Research workflow.

Detailed architecture notes: [docs/ARCHITECTURE_OPTIMIZED.md](docs/ARCHITECTURE_OPTIMIZED.md)

## 📡 Backend Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | Health check |
| `POST` | `/research` | Run research (sync) |
| `POST` | `/research/stream` | Run research (SSE streaming) |
| `POST` | `/research/continue/stream` | Continue previous research (streaming) |
| `POST` | `/harness/run` | Internal controlled run (with policy metadata) |
| `GET` | `/harness/runs/{run_id}` | Retrieve historical run record |
| `GET` | `/harness/scenarios` | List evaluation scenarios |

`/research` is the public business entrypoint. `/harness/run` is the internal engineering entrypoint.

## 📂 Project Layout

```
helloagents-deepresearch/
├── backend/
│   ├── src/
│   │   ├── main.py                 # FastAPI entrypoint
│   │   ├── agent.py                # Core research workflow
│   │   ├── config.py               # Configuration model
│   │   ├── models.py               # Data models
│   │   ├── prompts.py              # LLM prompts
│   │   ├── services/               # Business services
│   │   │   ├── planner.py          # Task planning
│   │   │   ├── search.py           # Search dispatch (retry + fallback)
│   │   │   ├── summarizer.py       # Task summarization
│   │   │   ├── reporter.py         # Report generation
│   │   │   └── note_agent.py       # Note management
│   │   └── harness/                # Governance runtime
│   │       ├── runner.py           # Controlled run executor
│   │       ├── policy.py           # Permission policy
│   │       ├── evaluator.py        # Run scoring
│   │       ├── compressor.py       # Context compression
│   │       ├── context_manager.py  # Context lifecycle
│   │       ├── recorder.py         # Run persistence
│   │       ├── replay.py           # Replay
│   │       ├── scenarios.py        # Evaluation scenarios
│   │       └── event_bus.py        # In-memory event bus
│   └── tests/                      # Test suite
├── frontend/
│   ├── src/
│   │   ├── App.vue                 # Vue SFC
│   │   ├── main.ts                 # App entry
│   │   └── services/api.ts         # SSE consumer
│   └── package.json
├── docs/
│   ├── ARCHITECTURE_OPTIMIZED.md
│   └── TECHNICAL_DEEP_DIVE.md
├── CHANGELOG.md
└── README.md
```

## 🚀 Local Development

### Prerequisites

- Python ≥ 3.10
- Node.js ≥ 18
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

### Configuration

```bash
cp backend/.env.example backend/.env
# Edit backend/.env with your API keys and model settings
```

### Start

**Backend:**
```bash
cd backend
uv run python src/main.py
# Or: pip install -e . && python src/main.py
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

Default backend address: `http://localhost:8000`

## 📦 Harness Outputs

Managed runs are persisted under:

```
./output/harness_runs/
```

Each run produces:
- One final JSON record
- One append-only JSONL index entry
- One per-run event log

## 🎯 Current Focus

- Keep the research agent simple
- Centralize run governance in the harness layer
- Reuse compressed context for follow-up research
- Expand evaluation and replay over time
