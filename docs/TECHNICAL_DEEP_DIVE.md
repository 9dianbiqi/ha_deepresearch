# HelloAgents Deep Research — 技术深度分析

> 版本: 0.0.1 | 作者: 基于源码逆向分析 | 日期: 2026-05-31

---

## 目录

1. [系统概览](#1-系统概览)
2. [架构设计](#2-架构设计)
3. [多智能体协作体系](#3-多智能体协作体系)
4. [上下文工程](#4-上下文工程)
5. [记忆系统](#5-记忆系统)
6. [流式事件协议与实时通信](#6-流式事件协议与实时通信)
7. [治理与安全层 (Harness)](#7-治理与安全层-harness)
8. [搜索与知识检索](#8-搜索与知识检索)
9. [数据模型与状态管理](#9-数据模型与状态管理)
10. [设计模式与工程实践](#10-设计模式与工程实践)
11. [前端架构](#11-前端架构)
12. [数据流全景](#12-数据流全景)

---

## 1. 系统概览

HelloAgents Deep Research 是一个**全本地化的深度研究助手**，接收一个开放研究主题，自动将其拆解为可执行的子任务，调度多搜索引擎检索信息，对每个任务进行 LLM 摘要，最终生成结构化 Markdown 研究报告。

### 1.1 技术栈

| 层 | 技术 |
|---|------|
| HTTP 框架 | FastAPI (Python 3.10+) |
| Agent 框架 | `hello-agents` v0.2.9 |
| LLM 后端 | 兼容 OpenAI API（支持 Ollama / LMStudio / 智谱 GLM 等） |
| 搜索引擎 | DuckDuckGo / Tavily / Perplexity / SearXNG |
| 前端 | Vue 3 + TypeScript + Vite 6 |
| Markdown 渲染 | marked.js v15 |
| 持久化 | 文件系统 JSONL / JSON |
| 日志 | loguru (后端) |

### 1.2 核心能力

```
用户输入: "开源Agent框架在2026年的发展现状"
     │
     ▼
  规划 → [任务1: 主流框架对比, 任务2: 技术趋势, 任务3: 应用案例, ...]
     │
     ▼
  并行执行 → 每个任务: 搜索 → 摘要 → 笔记持久化
     │
     ▼
  报告生成 → 结构化 Markdown 报告（背景/洞见/证据/风险/参考来源）
```

---

## 2. 架构设计

### 2.1 分层架构

系统采用严格的**三层分离**架构：

```
┌──────────────────────────────────────────────────────┐
│  HTTP 传输层 (main.py)                                │
│  - 请求标准化 (HarnessRunRequest)                      │
│  - 响应序列化 (ResearchResponse / HarnessResponse)     │
│  - SSE 流式传输                                       │
│  - CORS / 异常处理                                    │
└────────────────────┬─────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────┐
│  Harness 治理层 (harness/)                             │
│  - 策略执行 (HarnessPolicy)                            │
│  - 事件记录 (InMemoryEventBus)                         │
│  - 上下文压缩 (ContextCompressor)                       │
│  - 质量评估 (RuleBasedEvaluator)                       │
│  - 持久化 (JsonlRunRecorder)                           │
│  - 生命周期编排 (HarnessRunner)                         │
└────────────────────┬─────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────┐
│  研究执行层 (agent.py + services/)                      │
│  - 任务规划 (PlanningService)                          │
│  - 搜索调度 (dispatch_search)                          │
│  - 任务摘要 (SummarizationService)                     │
│  - 报告生成 (ReportingService)                         │
│  - 笔记管理 (NoteSubAgent)                             │
└──────────────────────────────────────────────────────┘
```

**关键设计原则：层间单向依赖。** 研究执行层完全不知道 Harness 层的存在——它只接收 `Configuration` 对象，返回 `SummaryStateOutput`。Harness 层通过**装饰器/外观模式**在研究执行前后插入治理逻辑。

### 2.2 核心执行路径

#### 同步路径 (`POST /research`)

```python
# main.py:185-200
def run_research(payload: ResearchRequest) -> ResearchResponse:
    request = _normalize_harness_request(payload, caller_mode="public")
    result = harness_runner.run(request)    # ← Harness 接管
    # ...
```

`HarnessRunner.run()` 内部执行的固定生命周期（[runner.py:45-82](backend/src/harness/runner.py#L45-L82)）：

```
1. RunContext 创建 → event_bus.emit("run_started")
2. _evaluate_policy()    → 权限检查，拒绝则抛 PermissionError
3. _execute_agent()      → new DeepResearchAgent(config).run(topic)
4. _compress_context()   → ContextCompressor 生成压缩记忆
5. finally:
     _evaluate_run()     → RuleBasedEvaluator 打分
     _persist_run()      → JsonlRunRecorder 写盘
```

#### 流式路径 (`POST /research/stream`)

流式路径的差异在于步骤 3 —— 不走 `agent.run()`，而是直接迭代 `agent.run_stream()`：

```python
# runner.py:94-105
agent = DeepResearchAgent(config=context.request.config)
for event in agent.run_stream(context.request.topic):
    self.event_bus.emit(context, "research_event", ...)   # 旁路记录
    self._ingest_stream_event(stream_state, event)         # 状态重建
    yield {"run_id": context.run_id, **event}              # SSE 透传
```

这里有一个**精妙的设计**：harness 在消费 agent 流的同时做了三件事：
1. **记录** — 每个事件写入 event_bus
2. **重建** — 从流事件中增量重建 `TodoItem` 列表（供后续评估/持久化使用）
3. **透传** — 不做修改直接 yield 给上层（FastAPI → SSE → 前端）

### 2.3 架构决策记录 (ADR)

| 决策 | 理由 | 权衡 |
|------|------|------|
| Harness 不侵入 Agent | 保持研究逻辑独立可测试 | 流事件协议成为隐式契约，需手动保持同步 |
| 使用 dataclass 而非 Pydantic 做领域模型 | 轻量、无验证开销 | 缺少自动序列化/反序列化 |
| 同步和流式两套执行路径 | 同时支持批量和实时场景 | 代码有一定重复 |
| 文件系统持久化而非数据库 | 零依赖、易调试 | 不支持并发写入、无查询能力 |
| 单文件 Vue 组件 (2416行) | 快速开发 | 可维护性挑战 |

---

## 3. 多智能体协作体系

本系统是一个**异质多智能体协作系统**，包含 **4 个功能性 Agent** 和 **1 个工具型 SubAgent**，各司其职、通过结构化数据传递协作。

### 3.1 Agent 角色定义

```
                    DeepResearchAgent (编排器)
                    ═══════════════════════════
                    不直接执行 LLM 调用
                    负责: 生命周期管理、状态协调、线程调度
                           │
          ┌───────────────┼───────────────┬────────────────┐
          │               │               │                │
          ▼               ▼               ▼                ▼
   PlanningService  Summarization   ReportingService  NoteSubAgent
   ┌─────────────┐  Service         ┌──────────────┐  ┌────────────┐
   │ 研究规划专家  │  ┌────────────┐  │ 报告撰写专家   │  │ 笔记工具    │
   │              │  │ 任务总结专家 │  │              │  │ (非 LLM)   │
   │ 输入: topic  │  │ (×N 实例)  │  │ 输入: 全部    │  │            │
   │ 输出:        │  │            │  │ 任务摘要+笔记  │  │ CRUD 操作  │
   │ TodoItem[]  │  │ 输入: 搜索  │  │ 输出:         │  │ 文件系统    │
   │              │  │ 上下文+笔记  │  │ Markdown报告  │  │ 读写       │
   └─────────────┘  │ 输出: 摘要  │  └──────────────┘  └────────────┘
                    └────────────┘
```

### 3.2 Agent 实例化策略

不同 Agent 采用不同的实例化策略，体现了对**上下文隔离**的深度思考：

| Agent | 实例化策略 | 生命周期 | 原因 |
|-------|-----------|---------|------|
| 研究规划专家 | **单例** — `DeepResearchAgent.__init__` 中创建 | 整个 Agent 生命周期 | 规划只调用一次，无上下文污染风险 |
| 任务总结专家 | **工厂模式** — `_summarizer_factory` 每次创建新实例 | 单次摘要调用 | 每个任务有不同上下文，必须隔离 |
| 报告撰写专家 | **单例** | 整个 Agent 生命周期 | 只调用一次 |
| NoteSubAgent | **单例** | 整个 Agent 生命周期 | 无状态工具，线程安全 |

关键代码（[agent.py:53-56](backend/src/agent.py#L53-L56)）：

```python
self._summarizer_factory: Callable[[], ToolAwareSimpleAgent] = lambda: (
    self._create_tool_aware_agent(
        name="任务总结专家",
        system_prompt=task_summarizer_instructions.strip(),
    )
)
```

**为什么 Summarizer 必须用工厂模式？** 因为 `run_stream()` 中多个任务并行执行（多线程）。如果共享同一个 Agent 实例，线程 A 调用 `agent.stream_run(prompt_a)` 的同时线程 B 调用 `agent.stream_run(prompt_b)`，两个 prompt 会互相污染。工厂模式确保每个线程拿到独立的 Agent 实例。

### 3.3 Agent 间通信协议

Agent 之间**不直接通信**——它们通过 `DeepResearchAgent` 编排器和共享 `SummaryState` 进行数据传递：

```
PlanningService ──(TodoItem[])──→ SummaryState.todo_items
                                       │
dispatch_search ──(search_result)──→ SummaryState.web_research_results
                                       │
SummarizationService ──(summary)──→ TodoItem.summary (每个任务)
                                       │
ReportingService ←── 读取 SummaryState 中所有任务摘要 ──→ 生成报告
```

通信格式是**结构化 Python 对象**（dataclass），不依赖自然语言。这比纯文本 Agent 间通信更可靠。

### 3.4 Tool Calling 的设计取舍

项目做了一个刻意的设计决策（[agent.py:102-108](backend/src/agent.py#L102-L108)）：

```python
def _create_tool_aware_agent(self, *, name, system_prompt):
    return ToolAwareSimpleAgent(
        name=name,
        llm=self.llm,
        system_prompt=system_prompt,
        enable_tool_calling=False,   # ← 关闭 Tool Calling
        tool_registry=None,           # ← 不注册工具
    )
```

**所有研究 Agent 的 `enable_tool_calling=False`。** 这意味着 LLM 不会被注入工具定义，模型输出是纯文本（Markdown / JSON），不产生 function call。

**为什么这样做？**

1. **职责分离**：笔记 CRUD 全部由 `NoteSubAgent` 在 Agent 文本流之外处理。模型不需要知道"笔记工具"的存在。
2. **确定性输出**：模型输出纯 Markdown，不会混入 `<function_call>` 标签或 JSON tool call，前端渲染和解析更简单。
3. **减少 Token 消耗**：工具定义的 system prompt 不发送，节省上下文窗口。

**代价**：失去了让模型主动调用搜索/计算工具的能力。搜索是由硬编码编排流程触发的，而非模型决策。

---

## 4. 上下文工程

上下文工程是 LLM 应用的核心挑战。本系统在多个层面做了精细的上下文管理。

### 4.1 Prompt 模板设计

系统使用 **System Prompt + 结构化 User Prompt** 模式：

```
┌──────────────────────────────────────────┐
│  System Prompt (不变)                      │
│  - 角色定义                                │
│  - 行为约束 (GOAL / FORMAT / NOTES)        │
│  - 输出格式要求                             │
├──────────────────────────────────────────┤
│  User Prompt (每次调用动态构造)              │
│  - CONTEXT: 当前日期 + 研究主题              │
│  - 搜索结果 + 来源                          │
│  - 笔记内容 (避免重复)                       │
│  - 已有的任务进展                            │
└──────────────────────────────────────────┘
```

三个 Prompt 模板各有不同的设计策略（[prompts.py](backend/src/prompts.py)）：

| Prompt | 策略 | 关键约束 |
|--------|------|---------|
| `todo_planner_instructions` | JSON 模式 | `{"tasks": [...]}` 严格格式 |
| `task_summarizer_instructions` | 多维拓展 | 原理/应用/优缺点/工程实践/对比/历史演变 |
| `report_writer_instructions` | 五段式模板 | 背景/洞见/证据/风险/来源 |

### 4.2 上下文构造与防重复策略

每次调用 Summarizer 时，上下文是精心构造的（[summarizer.py:120-148](backend/src/summarizer.py#L120-L148)）：

```python
def _build_prompt(self, state, task, context, notes_context):
    note_section = ""
    if notes_context and task.note_id and task.note_id in notes_context:
        note_data = notes_context[task.note_id]
        note_content = note_data.get("content", "")
        if note_content:
            note_section = (
                f"\n任务笔记（ID: {task.note_id}，已由系统自动同步）：\n"
                f"{note_content}\n"
                "请参考以上笔记内容，避免重复已有信息。\n"   # ← 关键指令
            )
    return (
        f"任务主题：{state.research_topic}\n"
        f"任务名称：{task.title}\n"
        f"任务目标：{task.intent}\n"
        f"检索查询：{task.query}\n"
        f"任务上下文：\n{context}\n"
        f"{note_section}"
        "请返回一份面向用户的 Markdown 总结（遵循任务总结模板）。"
    )
```

**上下文防重复机制**：
1. 如果任务的笔记已存在（之前执行过的结果），将笔记内容嵌入 prompt
2. 明确告诉模型"请参考以上笔记内容，避免重复已有信息"
3. 这样后续任务可以看到前面任务的产出，形成**渐进式知识积累**

### 4.3 Thinking Token 剥离

许多模型（如 DeepSeek-R1、Qwen3）在输出中嵌入 `</think>` 标签包裹的推理过程。系统提供全局开关处理（[utils.py:19-26](backend/src/utils.py#L19-L26)）：

```python
def strip_thinking_tokens(text: str) -> str:
    while "<think>" in text and "</think>" in text:
        start = text.find("<think>")
        end = text.find("</think>") + len("</think>")
        text = text[:start] + text[end:]
    return text
```

**流式场景下的剥离更复杂**（[summarizer.py:65-107](backend/src/summarizer.py#L65-L107)）：因为 chunk 逐个到达，`<think>` 可能跨 chunk 边界。系统使用了一个**有限状态自动机**来跟踪缓冲区：

```
状态: NORMAL → 遇到<think> → INSIDE_THINK → 遇到</think> → NORMAL
```

只有在 `NORMAL` 状态下收到的文本才会 yield 出去。`finally` 块确保流结束后清空缓冲区。

### 4.4 Harness 层的上下文压缩

`ContextCompressor` 将完整的研究输出压缩为**两种可复用格式**（[compressor.py](backend/src/harness/compressor.py)）：

```python
{
    "run_summary": {           # 用于展示和回放
        "completed_tasks": [   # 摘要截断到 280 字符
            {"task_id": 1, "title": "...", "summary_excerpt": "...", "sources_excerpt": "..."}
        ],
        "incomplete_tasks": [...],
        "report_excerpt": "..."  # 截断到 1000 字符
    },
    "reasoning_memory": {      # 用于后续研究的上下文注入
        "key_findings": [...],     # 每条截断到 180 字符
        "key_sources": [...],      # 每条截断到 180 字符
        "open_questions": [...]    # 未完成任务标题
    }
}
```

`reasoning_memory` 的设计是**前瞻性的**——虽然当前版本尚未实现多轮研究对话，但数据结构已经为"将上一次研究的发现注入下一次研究的上下文"做好了准备。

---

## 5. 记忆系统

本系统的记忆分为 **三个层次**，形成了完整的记忆金字塔。

### 5.1 记忆架构全景

```
┌────────────────────────────────────────────────────────────────┐
│  L3: 持久化记忆 (Harness Persistence)                            │
│  - 完整运行记录 ({run_id}.json)                                  │
│  - 事件日志 ({run_id}.events.jsonl)                              │
│  - 运行索引 (runs.jsonl)                                        │
│  - 评估结果                                                      │
│  生命周期: 永久                                                   │
├────────────────────────────────────────────────────────────────┤
│  L2: 压缩记忆 (Compressed Context)                               │
│  - run_summary (已完成任务/未完成任务/报告摘要)                     │
│  - reasoning_memory (关键发现/来源/待解决问题)                     │
│  生命周期: 跨运行 (设计为后续研究的上下文注入)                       │
├────────────────────────────────────────────────────────────────┤
│  L1: 工作记忆 (NoteTool + SummaryState)                          │
│  - 任务笔记 (notes/*.md) ── 每个任务的搜索+摘要结果               │
│  - SummaryState ── 运行中的可变状态                               │
│  - 笔记索引 (notes_index.json)                                   │
│  生命周期: 单次运行 (可跨运行恢复)                                  │
└────────────────────────────────────────────────────────────────┘
```

### 5.2 L1: 工作记忆 — NoteTool 笔记系统

**这是系统最核心的记忆机制。** 每个研究任务都有对应的持久化笔记。

**创建** — `NoteSubAgent.create_task_note()`（[note_agent.py:35-61](backend/src/services/note_agent.py#L35-L61)）：

```python
def create_task_note(self, *, task_id, title, content=""):
    tags = ["deep_research", f"task_{task_id}"]
    payload = {
        "action": "create",
        "task_id": task_id,
        "title": f"任务 {task_id}: {title}",
        "note_type": "task_state",
        "tags": tags,
        "content": content,
    }
    response = self._tool.run(payload)
    note_id = self._parse_note_id(response)
    return note_id
```

**更新** — 任务摘要完成后立即更新笔记（[agent.py:415-429](backend/src/agent.py#L415-L429)）：

```python
def _update_task_note(self, task):
    content_parts = [f"任务状态：{task.status}"]
    if task.summary:
        content_parts.append(f"\n任务总结：\n{task.summary}")
    if task.sources_summary:
        content_parts.append(f"\n来源概览：\n{task.sources_summary}")
    self.note_agent.update_note(...)
```

**读取** — 后续任务的 Prompt 构造时读取已有笔记，实现知识传递（[agent.py:329](backend/src/agent.py#L329)）：

```python
notes_context = self._read_task_note(task)  # 读取当前任务笔记
# 传递给 summarizer，让模型看到已有进展
```

**结论笔记** — 最终报告也保存为 `note_type="conclusion"` 的特殊笔记（[agent.py:431-457](backend/src/agent.py#L431-L457)）。

**笔记系统的设计价值**：
- **知识累积**：任务 3 的摘要可以看到任务 1、2 的产出
- **断点续传**：笔记是文件系统持久化的，理论上可以从失败的任务恢复
- **可追溯**：每个结论都有笔记 ID 可回溯来源

### 5.3 L2: 压缩记忆 — ContextCompressor

详细分析见 [4.4 节](#44-harness-层的上下文压缩)。`reasoning_memory` 是本系统记忆体系中最具前瞻性的设计——它从完整的研究输出中提取**结构化关键信息**，为未来的多轮对话式深度研究做准备。

### 5.4 L3: 持久化记忆 — JsonlRunRecorder

`JsonlRunRecorder` 以**三种文件格式**持久化每次运行（[recorder.py:25-66](backend/src/harness/recorder.py#L25-L66)）：

```
output/harness_runs/
├── {run_id}.json           # 完整运行快照 (可审计)
├── {run_id}.events.jsonl   # 逐事件日志 (可回放调试)
└── runs.jsonl              # 追加索引 (可列举)
```

三种格式各有用途：

| 格式 | 内容 | 用途 |
|------|------|------|
| `.json` | 完整记录（输入/输出/评估/策略） | 审计、查询 |
| `.events.jsonl` | 按时间序的事件流 | 回放、调试、性能分析 |
| `runs.jsonl` | 一行一条索引（id/topic/status/score） | 列出所有历史运行 |

---

## 6. 流式事件协议与实时通信

### 6.1 SSE 事件类型定义

系统定义了 **9 种 SSE 事件类型**，构成前后端通信的完整协议：

| 事件类型 | 发出者 | 含义 | 关键字段 |
|---------|--------|------|---------|
| `status` | agent | 状态通知 (含搜索 backend 消息) | `message`, `task_id` |
| `todo_list` | agent | 规划完成，任务列表就绪 | `tasks: TodoItem[]` |
| `task_status` | agent | 任务状态变更 | `task_id`, `status` (in_progress/completed/skipped/failed) |
| `sources` | agent | 搜索完成，来源就绪 | `latest_sources`, `raw_context`, `backend` |
| `task_summary_chunk` | agent | 摘要流式增量 | `content` (部分 Markdown) |
| `tool_call` | agent | 笔记工具操作通知 | `note_id` |
| `final_report` | agent | 最终报告就绪 | `report` (完整 Markdown) |
| `done` | agent | 研究流程结束 | — |
| `error` | harness | 流式过程异常 | `detail` |

### 6.2 并行任务的事件多路复用

当有 3 个并行任务时，每个任务独立产生 `sources` → `task_summary_chunk` → `task_status` 事件序列。前端通过 `task_id` 和 `stream_token` 进行**事件分发**：

```
时间线 →
Task 1: [task_status:in_progress] [sources] [chunk][chunk][chunk] [task_status:completed]
Task 2:     [task_status:in_progress] [sources] [chunk][chunk] [task_status:completed]
Task 3:         [task_status:in_progress] [sources] [chunk][chunk][chunk] [task_status:completed]

所有事件通过单一 Queue → SSE 通道串行传输，前端按 task_id 分发到对应 UI 区域
```

### 6.3 前端 SSE 消费

[api.ts](frontend/src/services/api.ts) 使用原生 `fetch` + `ReadableStream` 解析 SSE：

```typescript
const reader = body.getReader();
const decoder = new TextDecoder("utf-8");
let buffer = "";

while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });

    // 按 \n\n 分割 SSE 事件
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
        const rawEvent = buffer.slice(0, boundary).trim();
        buffer = buffer.slice(boundary + 2);
        if (rawEvent.startsWith("data:")) {
            const event = JSON.parse(rawEvent.slice(5).trim());
            onEvent(event);       // 回调通知 Vue 组件
        }
        boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
}
```

### 6.4 Harness 的流事件摄入

Harness 在流式路径中通过 `_ingest_stream_event()` 从事件流重建 `TodoItem` 状态（[runner.py:179-251](backend/src/harness/runner.py#L179-L251)）：

```
事件流: todo_list → task_status → sources → task_summary_chunk × N → task_status → final_report
                          │            │              │
                          ▼            ▼              ▼
重建逻辑:           创建 TodoItem   更新来源     追加摘要文本片段
```

这个**事件溯源 (Event Sourcing)** 模式使得 Harness 可以在流结束后拥有完整的 `SummaryStateOutput`，用于压缩、评估和持久化。

---

## 7. 治理与安全层 (Harness)

### 7.1 设计哲学

Harness 是"运行时治理层"，不是第二个业务工作流。它的稳定执行形态为：

```
FastAPI → request normalization → HarnessRunner → DeepResearchAgent
                                                      │
                  ┌───────────────────────────────────┘
                  ▼
          compression / evaluation / persistence
```

**核心原则**：
- `DeepResearchAgent` 对研究执行负责
- `HarnessRunner` 对运行治理负责
- 两者互不侵入，通过 `Configuration` 和 `SummaryStateOutput` 进行数据交换

### 7.2 能力型权限模型

`HarnessPolicy` 定义了一套**基于能力的权限模型**（[policy.py](backend/src/harness/policy.py)）：

```python
def required_capabilities(self, request):
    capabilities = ["research:run", "search:web", "report:export"]
    if request.config.search_api == SearchAPI.PERPLEXITY:
        capabilities.append("search:premium")          # 高级搜索需额外权限
    if request.config.enable_notes:
        capabilities.extend(["notes:read", "notes:write"])  # 笔记功能
    return capabilities
```

每种能力返回三种结果之一：

| 结果 | 含义 | 行为 |
|------|------|------|
| `allow` | 许可 | 继续执行 |
| `deny` | 禁止 | 抛出 `PermissionError` → HTTP 403 |
| `ask` | 需审批 | 当前版本等同于 `deny`（未来可接入审批 UI） |

**权限决策示例**：

| 场景 | `search:premium` | `notes:write` (enable_notes=False) |
|------|------------------|-----------------------------------|
| `permission_mode="default"` | `allow` | `deny` |
| `permission_mode="strict"` | `ask` → 阻断 | `deny` |

### 7.3 质量评估引擎

`RuleBasedEvaluator` 采用**扣分制**进行质量评分（[evaluator.py](backend/src/harness/evaluator.py)）：

```
初始分数: 1.0

- 无输出        → 直接归零 (score=0.0)
- 无任务        → -0.2
- 报告为空      → -0.5
- 有未完成任务  → -0.2
- 有任务无摘要  → -0.1
- 无压缩上下文  → -0.1

下限: 0.0
```

评估结果随运行记录一起持久化，可用于**回归测试**和**质量监控**。

### 7.4 事件溯源与可观测性

`InMemoryEventBus` 是 Harness 的神经中枢（[event_bus.py](backend/src/event_bus.py)）：

```python
class InMemoryEventBus:
    def emit(self, context, event_type, **payload) -> HarnessEvent:
        event = HarnessEvent(
            event_type=event_type,
            run_id=context.run_id,
            payload=dict(payload),
            sequence=len(context.events) + 1,   # 自增序列号
        )
        context.events.append(event)
        return event
```

在一次完整运行中触发的事件类型：

```
run_started → policy_checked → research_event × N → run_completed/run_failed
                                                         │
                                               context_compressed
```

每个事件带时间戳和序列号，形成完整的**可审计轨迹**。

---

## 8. 搜索与知识检索

### 8.1 多后端搜索架构

搜索层通过 `SearchTool(backend="hybrid")` 统一抽象多种搜索后端（[search.py:76-86](backend/src/services/search.py#L76-L86)）：

```python
raw_response = _GLOBAL_SEARCH_TOOL.run({
    "input": query,
    "backend": search_api,     # duckduckgo / tavily / perplexity / searxng
    "mode": "structured",
    "fetch_full_page": config.fetch_full_page,
    "max_results": 5,
    "max_tokens_per_source": 2000,
    "loop_count": loop_count,
})
```

### 8.2 搜索缓存策略

缓存位于 `notes_workspace/../cache/search/`，以 MD5 哈希为键：

```python
def _cache_key(query, config):
    content = f"{query}_{search_api}_{config.fetch_full_page}"
    return hashlib.md5(content.encode()).hexdigest()
```

**缓存策略**：
- **什么被缓存**：完整的结构化搜索结果（JSON）
- **缓存时机**：仅 `loop_count == 0` 时（首次搜索）
- **缓存命中**：直接返回，跳过网络请求
- **缓存失效**：手动删除文件

### 8.3 搜索结果的上下文构造

`dispatch_search` 返回四个值（[search.py:59-64](backend/src/services/search.py#L59-L64)）：

```python
return (payload,          # dict: 完整搜索响应 {"results": [...], "answer": "...", ...}
        notices,          # list[str]: 来自搜索后端的通知消息
        answer_text,      # str|None: AI 直接答案 (如 Perplexity 的答案)
        backend_label)    # str: 实际使用的后端标识
```

然后 `prepare_research_context()` 将其格式化为 LLM 可消费的文本（[search.py:134-151](backend/src/services/search.py#L134-L151)）：

```
AI直接答案：
(answer_text)

信息来源: {title}
URL: {url}
信息内容: {content}
详细信息内容限制为 2000 个 token: {raw_content[:8000]}... [truncated]
```

---

## 9. 数据模型与状态管理

### 9.1 领域模型层次

```
SummaryState (运行级可变状态)
├── research_topic: str
├── research_loop_count: int           # 搜索循环计数
├── web_research_results: list[str]    # 累积的搜索上下文
├── sources_gathered: list[str]        # 累积的来源列表
├── todo_items: list[TodoItem]         # 任务列表
├── structured_report: str             # 最终报告
├── report_note_id / report_note_path  # 结论笔记
└── (deprecated) search_query, running_summary

TodoItem (任务级状态)
├── id, title, intent, query           # 不可变 (规划阶段确定)
├── status: pending|in_progress|completed|skipped|failed
├── summary: str                       # LLM 生成的摘要
├── sources_summary: str               # 格式化的来源列表
├── notices: list[str]                 # 搜索后端通知
├── note_id / note_path                # 关联的笔记文件
└── stream_token: str                  # 前端流式路由标识

SummaryStateOutput (不可变输出)
├── running_summary: str              # 向后兼容
├── report_markdown: str              # 最终报告
└── todo_items: list[TodoItem]        # 完整任务列表
```

### 9.2 状态生命周期

```
初始化                      执行中                        完成
  │                          │                            │
  ▼                          ▼                            ▼
SummaryState()          state.todo_items              SummaryStateOutput
  todo_items=[]         state.sources_gathered          (不可变快照)
  loop_count=0          task.status="completed"
                        task.summary="..."
                              │
                              ▼
                        Note (持久化到文件系统)
```

### 9.3 线程安全

`DeepResearchAgent` 使用 `threading.Lock` 保护共享状态（[agent.py:42](backend/src/agent.py#L42)）：

```python
self._state_lock = Lock()

# 使用处 (agent.py:280-283):
with self._state_lock:
    loop_count = state.research_loop_count
    state.research_loop_count += 1
```

受保护的临界区很小——仅 `research_loop_count` 的自增和 `web_research_results` / `sources_gathered` 的追加。每个任务的 `TodoItem` 对象由各自的线程独立修改，不存在竞争。

---

## 10. 设计模式与工程实践

### 10.1 已应用的设计模式

| 模式 | 应用位置 | 说明 |
|------|---------|------|
| **外观 (Facade)** | `HarnessRunner` | 统一封装 policy → agent → compression → evaluation → persistence |
| **工厂方法 (Factory Method)** | `SummarizationService` | `_summarizer_factory` 为每次调用创建新 Agent |
| **策略 (Strategy)** | `RuleBasedEvaluator` / `HarnessPolicy` | 可替换的评估和权限策略 |
| **依赖注入 (DI)** | `create_app(harness_runner)` / 各 Service 构造函数 | 便于测试 mock |
| **模板方法 (Template Method)** | `HarnessRunner.run()` / `.stream()` | 固定生命周期骨架 |
| **事件溯源 (Event Sourcing)** | `InMemoryEventBus` + `_ingest_stream_event` | 从事件流重建聚合状态 |
| **建造者 (Builder)** | `HarnessScenario.build_request()` | 构建 `HarnessRunRequest` |

### 10.2 配置管理

`Configuration` 使用 Pydantic `BaseModel` + 环境变量映射（[config.py:91-107](backend/src/config.py#L91-L107)）：

```python
@classmethod
def from_env(cls, overrides=None):
    raw_values = {}
    for field_name in cls.model_fields.keys():
        env_key = field_name.upper()      # max_web_research_loops → MAX_WEB_RESEARCH_LOOPS
        if env_key in os.environ:
            raw_values[field_name] = os.environ[env_key]
    if overrides:
        raw_values.update(overrides)
    return cls(**raw_values)
```

支持**运行时覆盖**（如 API 请求中指定不同的 `search_api`）。

### 10.3 错误处理策略

系统采用**分层错误处理**：

```
服务层:  捕获具体异常 → 记录日志 → 优雅降级 (任务标记为 skipped)
Agent层: try/except per task → 单个任务失败不影响其他任务
Harness: try/except 包裹全部 → context.status = "failed" → 评估 + 持久化
HTTP层:  按异常类型映射 HTTP 状态码 → ValueError→400, PermissionError→403, else→500
流式层:  捕获异常 → SSE error 事件 (不中断连接)
```

---

## 11. 前端架构

### 11.1 单文件组件设计

前端是单个 `App.vue` 组件（2416 行），通过 `v-if` 在两套布局间切换：

```
状态: idle → 显示居中的输入卡片
状态: running/completed → 显示全屏研究面板
  ├── 左侧栏: 研究信息 + 进度指示
  └── 右侧面板: 任务列表 + 任务详情 + 最终报告
```

### 11.2 XSS 防护

前端对 Markdown 渲染做了 XSS 清洗（去除 `<script>`、`on*` handlers、`<iframe>`、`<object>`、`<embed>`），然后通过 `marked.js` 渲染。

### 11.3 实时任务进度

每个任务根据 `stream_token` 进行前端路由，支持：
- 动画时间线（pending → in_progress → completed）
- 增量 Markdown 渲染（摘要 chunk 逐段显示）
- 来源链接悬停预览
- 工具调用日志展示

---

## 12. 数据流全景

```
┌──────────────┐     HTTP/SSE      ┌──────────────┐     Python      ┌─────────────────┐
│   用户浏览器    │ ◄──────────────► │   FastAPI     │ ◄────────────► │  DeepResearch   │
│  (Vue 3 SPA)  │                  │  (main.py)    │                │  Agent          │
│               │                  │               │                │                 │
│  - 输入topic   │                  │  - 请求标准化   │                │  - 规划          │
│  - 实时看板    │                  │  - SSE 推送    │                │  - 搜索+摘要     │
│  - Markdown   │                  │  - 异常映射    │                │  - 报告生成      │
│    渲染       │                  │               │                │  - 笔记管理      │
└──────────────┘                  └──────┬────────┘                └────────┬────────┘
                                         │                                  │
                                         │ HarnessRunner                    │
                                         │ ┌──────────────┐                │
                                         │ │ Policy       │ ◄── 权限检查    │
                                         │ │ EventBus     │ ◄── 事件记录    │
                                         │ │ Compressor   │ ◄── 上下文压缩   │
                                         │ │ Evaluator    │ ◄── 质量评分    │
                                         │ │ Recorder     │ ◄── 持久化      │
                                         │ └──────────────┘                │
                                         └─────────────────────────────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────┐
                                              │   文件系统        │
                                              │  - notes/*.md   │
                                              │  - harness_runs/ │
                                              │  - cache/search/ │
                                              └─────────────────┘
```

---

## 附录 A: 关键文件索引

| 文件 | 行数 | 职责 |
|------|------|------|
| [agent.py](backend/src/agent.py) | 460 | 核心编排器 |
| [main.py](backend/src/main.py) | 286 | FastAPI 入口 + 路由 |
| [config.py](backend/src/config.py) | 122 | 配置模型 |
| [models.py](backend/src/models.py) | 59 | 领域数据模型 |
| [prompts.py](backend/src/prompts.py) | 94 | LLM Prompt 模板 |
| [utils.py](backend/src/utils.py) | 85 | 工具函数 |
| [services/search.py](backend/src/services/search.py) | 152 | 搜索调度 + 缓存 |
| [services/summarizer.py](backend/src/services/summarizer.py) | 149 | 摘要生成 (同步+流式) |
| [services/planner.py](backend/src/services/planner.py) | 123 | 任务规划 |
| [services/reporter.py](backend/src/services/reporter.py) | 78 | 报告生成 |
| [services/note_agent.py](backend/src/services/note_agent.py) | 156 | 笔记子代理 |
| [harness/runner.py](backend/src/harness/runner.py) | 267 | Harness 编排器 |
| [harness/policy.py](backend/src/harness/policy.py) | 112 | 权限策略 |
| [harness/evaluator.py](backend/src/harness/evaluator.py) | 150 | 质量评估 |
| [harness/compressor.py](backend/src/harness/compressor.py) | 68 | 上下文压缩 |
| [harness/recorder.py](backend/src/harness/recorder.py) | 74 | 持久化记录 |
| [harness/event_bus.py](backend/src/harness/event_bus.py) | 32 | 事件总线 |
| [harness/models.py](backend/src/harness/models.py) | 179 | Harness 数据模型 |
| [frontend/src/App.vue](frontend/src/App.vue) | 2416 | 前端单文件组件 |
| [frontend/src/services/api.ts](frontend/src/services/api.ts) | 97 | SSE 客户端 |

## 附录 B: 关键设计决策速览

| 决策 | 理由 |
|------|------|
| 为什么 Agent 不启用 tool calling？ | 职责分离——笔记 CRUD 由 NoteSubAgent 离线处理 |
| 为什么 Summarizer 使用工厂模式？ | 多线程并行时每个线程需要独立的 Agent 实例 |
| 为什么 Harness 不侵入 Agent？ | 保持研究逻辑独立可测试，Harness 是装饰层 |
| 为什么用文件系统而非数据库？ | 零依赖、可调试性、单用户场景足够 |
| 为什么前端是单文件组件？ | MVP 阶段快速迭代 |
| 为什么缓存键不包含 max_results？ | 当前 max_results 硬编码，未来需补全 |
