# 规划校验门（Plan Gate）

规划校验门是规划层与执行层之间的确定性边界：它把 planner 的自由文本输出转成有界、可校验的任务集合，并在进入检索前做一轮词法层面的质量校验。与证据层不同，这一层**失败开放**（fail-open）——任何无法修复的问题都只记录告警并继续执行，不会中断整次研究。

## 启用与配置

默认启用，无需额外操作。相关环境变量见 `backend/.env.example`：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `PLAN_VALIDATION` | `True` | 总开关；设为 `False` 时跳过校验门，planner 输出直接进入执行 |
| `PLAN_OVERLAP_THRESHOLD` | `0.75` | query 词元 Jaccard 重叠**严格大于**该值的任务被判为重复并丢弃 |
| `PLAN_COVERAGE_THRESHOLD` | `0.4` | 计划任务文本对主题显著词元的最小覆盖率 |
| `PLAN_VALIDATION_RETRIES` | `1` | 覆盖率不足时允许的带反馈重规划次数上限（0~2） |

关闭只需设置 `PLAN_VALIDATION=False` 并重启。生产 Compose 未显式映射这些变量，使用上述默认值即可，不需要改部署配置。

## 解析边界

`research/plan_schema.py` 把一次 planner 响应转成 `ResearchPlan`：

- 三档降级解析：整段 JSON（`direct`）→ 围栏代码块（`fenced`）→ 花括号/方括号截取（`brace`）；都失败则为 `unparsed`。空响应为 `empty`。
- 单个任务字段非法（缺字段、非文本、空串、超长）只丢弃该任务并计数，不影响同批其它任务。
- 有界：任务数上限 16，`title` 128 字符，`intent` / `query` 512 字符；超出任务数上限的部分被截断并单独计数。

「显式空计划」（`{"tasks": []}`，`is_explicit_empty`）与「不可解析」是两种可区分的结果，但当前只用于观测与日志：两者在协调器都会回落到单个兜底任务。

## 校验与修复

`research/plan_quality.py` 的 `PlanValidator` 只做确定性词法校验，不调用 LLM：

- **去重**：按 query 词元 Jaccard 重叠判定近似重复，保留先出现的任务、丢弃后出现的。只在 query 维度判重，不做意图或语义判重。
- **覆盖率**：用显著词元（剔除中英文停用词与「分析/调研/如何」等泛化指令词）计算任务文本对原主题的覆盖率；不足阈值时判定为 `RETRY`，并生成 ≤8 条可读反馈。相邻未覆盖的中文 bigram 会合并成完整概念片段（如「竞争」+「争者」→「竞争者」）。
- **有界重试**：仅当 planner 的可调用签名接受 `validation_feedback` 时才重规划，最多重试 `PLAN_VALIDATION_RETRIES` 次；注入的旧签名 planner 只接受确定性修复，不触发重试。
- 结论分 `pass` / `pass_with_repairs` / `retry` 三档。即使最终仍为 `retry`，也会返回已保留的任务继续执行。

## 复杂度分级

`estimate_complexity` 依据主题中的 ASCII 词、中文连续片段、并列连接符（`、`/`，`/`以及`/`vs`）与「对比/趋势/战略」等标记给出 `simple` / `medium` / `complex` 三档，对应建议任务数 1~2 / 3~5 / 5~8。该分级只作为 planner prompt 的建议，硬上限仍由 schema 与 profile 检索预算决定。

## 与检索预算的交互

计划任务数超过 profile 的 `retrieval_budget.max_tasks` 时，系统**截断**到上限并继续执行（此前会直接抛错终止运行）。覆盖率按截断**之前**的任务集判定，因此截断后实际覆盖可能低于记录值——这是有意的失败开放取舍。截断会记录 `trimmed` / `planned_count` / `max_tasks` / `reason_code=plan_budget_exceeded`。

## 观测

校验结果写入运行指标 `metrics.plan_quality`：

- `task_count`：最终进入执行的任务数
- `fallback`：是否使用了兜底单任务
- `validation_verdict`：`pass` / `pass_with_repairs` / `retry`
- `validation_retries`：实际重规划次数
- `dropped_duplicate_count` / `max_pairwise_overlap` / `coverage_ratio`：去重与覆盖率指标；`max_pairwise_overlap` 只在保留任务之间测量，因此不会超过阈值
- `complexity_tier`：复杂度分级
- `reason_codes`：`plan_duplicate_tasks_dropped`、`plan_coverage_below_threshold`、`plan_retry_unavailable`、`plan_retry_exhausted`、`plan_budget_exceeded`、`no_planned_tasks`

这些指标随 run 的 `metrics` 一并返回，不含 prompt、LLM 原文或网页正文。

## 恢复与回滚

恢复路径不会重新规划，因此校验门不影响已完成任务的恢复。回滚只需设置 `PLAN_VALIDATION=False` 并重启，planner 输出回到未校验路径，其余流程不变。
