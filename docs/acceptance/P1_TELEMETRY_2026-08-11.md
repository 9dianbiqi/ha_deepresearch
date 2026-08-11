# P1 telemetry acceptance log

日期：2026-08-11

## 范围

- 为每次报告、规划和摘要 LLM 调用保留 provider、model、request_id、开始时间、耗时、输入/输出/总 token、输出字符数、finish_reason、异常类型、retry_count 和 stream_completed。
- 对流式调用保留 chunk_count、首 chunk 延迟，并兼容不支持 `stream_options.include_usage` 的 OpenAI-compatible 服务。
- 将 LLM telemetry 限量持久化到 Run snapshot 的 `metrics.llm`，同时生成汇总；不保存 prompt 或模型原文。
- SSE 终止事件增加 duration、event_count、bytes_sent、首事件延迟、stream_completed 和 terminal_type；前端进度区显示该摘要。

## 自动化验证

- `396 passed, 2 skipped`（共收集 398 项后端测试）。
- 新增 `backend/tests/test_telemetry.py`：非流式 usage/finish_reason、正常流、provider 中断、Run metrics 限量汇总。
- 扩展 `backend/tests/test_harness_api.py`：SSE 正常结束和错误终止的 transport telemetry。
- `ruff check`：通过。
- `mypy`：本次 telemetry 与 SSE 修改文件通过；全量仍受现有 `hello_agents` 缺少类型声明影响。
- `npm run build`：通过（vue-tsc 与 Vite build）。

## 关键语义

- `stream_completed=true` 只表示 provider iterator 正常耗尽；异常、取消、GeneratorExit 均为 false，并记录 exception_type。
- finish_reason 只采信 provider 返回的 choice/chunk 元数据；没有元数据时保持 null，不推断为 stop。
- 最多保留 64 条调用明细，额外调用通过 `dropped_calls` 计数；summary 只聚合已知数值。
- telemetry 写入失败不会改变研究 Run 的业务结果。

## 未覆盖

- checkpoint 恢复、finish_reason 的长期趋势分析和外部监控接入仍属于后续 P1/P2 工作。
