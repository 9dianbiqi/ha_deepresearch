# Planner 第二轮：真实采集与人工复核

本轮把固定评估集推进到可操作流程：真实 Planner 采样、隐藏来源的待标材料、完整性校验、复核标签导入，以及初始/最终计划的四轴比较。尚未引入 Embedding 或 Judge。

## 本轮实际状态

最新更新：后端模型配置已改为glm-4.7-flash，接口小请求成功，关闭思考模式的真实Planner试采得到5个合法任务。人工材料位于backend/evaluation/glm47flash_real_review.json及.md，包含该样本的两个前后快照；它们未发生规则修复，内容相同。批量扩展采样遭供应商拒绝，未完成全部题目。

用户给出的固定计划4ee47a78e50c810f的任务1、5关系已改为uncertain并划入Boundary；其余标签未被自动记成人工标注。详见[六计划复核](PLANNER_SIX_PLAN_REVIEW.md)。以下第二/三轮失败记录为历史状态。

- 已生成 `backend/evaluation/planner_round2_review.md` 和配套 JSON：新能源汽车主题的 6 个固定候选，顺序打乱，原始标签与候选类别隐藏，全部标注字段为空。
- 这是**合成样本复核材料**，不能称为真实模型输出。源文件和映射文件用于导入定位，不应交给独立标注者。
- 固定样本曾在仓库中公开，因此只是隐藏已有标签的工作表，不能声称是严格独立盲测。
- 已尝试一个开发主题、一次真实 Planner 逻辑调用，供应商返回 `RateLimitError`，没有采集到有效计划。SDK 内部自动重试过；未继续扩大请求。
- 尚无人工复核结果，未生成语义改善率，未改变现有数据集的 `ai_draft` 状态。

第三轮已补充[六计划AI复核依据](PLANNER_SIX_PLAN_REVIEW.md)和[双人一致性/裁决流程](PLANNER_REVIEW_AGREEMENT.md)。供应商的具体拒绝原因为HTTP429、业务码1113：「余额不足或无可用资源包,请充值。」项目模型为glm-4.5-air。之前的RateLimitError只是SDK异常类型，不能据此判断为请求频率超限。

## 复核当前材料

先阅读题目 scope、必需维度与数量建议。需要调整这些标准时，应建立新数据版本并重新导出；不要看完候选以后悄悄修改 rubric。

在 `planner_round2_review.json` 中逐项填写：

- `reviewer`：实际标注者姓名或稳定标识。
- `reviewed_at`：实际标注日期，格式 `YYYY-MM-DD`。
- `review_notes`：本计划的复核说明。
- 每任务 `relevance`（0/1/2）、`granularity`（too_coarse/appropriate/too_fine）、`covered_dimensions`（维度 ID 数组）、`rationale`（非空理由）。
- 计划 `redundant_pairs`：例如 `[{"task_ids": [1, 5], "rationale": "相同对象与目标，未增加结论维度"}]`；确认没有冗余时填 `[]`，不要保留 null。

全部完成后，才将该复核包 `annotation_status` 改为 `human_reviewed`。这是人工完成声明，不是工具证明了标注可靠；建议两人独立标注并保留裁决记录。单个主题的复核不意味着整个 48 样本数据集已复核。

从 `backend` 目录导入已完成材料：

```powershell
uv run python -m research.plan_sampling import-review --collection evaluation/planner_round2_source.json --review evaluation/planner_round2_review.json --output benchmark_runs/planner-round2-reviewed.json
```

导入会验证源数据身份、任务文字/顺序/ID、题目标准、所有计划是否完整、日期、标注者和理由，并复用已有 schema 验证维度和冗余引用。缺失标注不能默认为合格。导入输出保留审阅者、日期、说明及四轴结果；不会自动把整个原数据集改成已复核。

扩大复核范围时使用新文件名，`--topic-limit 6` 可以导出全部开发主题的 36 个固定候选：

```powershell
uv run python -m research.plan_sampling seed-review --topic-limit 6 --collection benchmark_runs/seed-source.json --output benchmark_runs/seed-review.json
```

## 真实 Planner 采集

接口恢复后，先用一个主题验证可用性：

```powershell
uv run python -m research.plan_sampling collect --iterations 1 --topic-limit 1 --output benchmark_runs/planner-live-pilot.json
```

再进行默认开发分组的多次采样：

```powershell
uv run python -m research.plan_sampling collect --iterations 2 --output benchmark_runs/planner-live-round.json
```

配置从已有 `.env` / 环境变量读取。使用与生产相同的 `PlanningService`、`SimpleAgent`、系统提示词、解析器和 PlanValidator；温度 0，逻辑调用超时设为 45 秒，任务计划的反馈重试上限使用 `PLAN_VALIDATION_RETRIES`。SDK 可能有额外的请求级重试，45 秒不等于整个采样作业的墙钟上限。

GLM-4.7-Flash默认思考模式在一次2000-token样本中达到length终止，JSON截断；关闭思考后同主题正常解析。采样CLI新增`--thinking disabled`和`--max-output-tokens`，只影响该实验请求，参数会写入结果；不能把关闭思考的结果直接宣称为生产默认模式质量。本地.env仅切换模型，运行中的后端需重启加载；生产思考模式尚未专门适配。

```powershell
uv run python -m research.plan_sampling collect --iterations 1 --topic-limit 1 --thinking disabled --output benchmark_runs/glm47flash-planner-new-pilot.json
```

默认只使用 development 的 6 个题目；iterations 支持 1–5，不触碰 holdout。每个样本保存初始输出、每次反馈后的输出、每轮去重后保留的任务、prompt 与指纹、模型配置、解析策略、耗时及已有 LLM telemetry 的 token/finish reason。没有拿到 token 用量时保留未知，不填成零。采集不保存 API key、base URL 或异常正文，只记录异常类型；数据包含题目与模型输出，存于已忽略的 `benchmark_runs/`。

只采集规划，不执行检索、笔记、报告、profile 任务预算截断或空计划兜底。无法解析的空计划保留供审阅；它与网络/供应商失败不同。实际生产最终报告质量不能由这个规划实验推断。

每个逻辑调用后增量保存，供应商调用发生错误时停止整个采样并记录失败；CLI 返回非零状态。已存在的采样输出不会被覆盖，恢复后使用新路径。不要把失败样本排除后仍声称所有请求成功。

## 导出真实计划的待标包

```powershell
uv run python -m research.plan_sampling export-review --collection benchmark_runs/planner-live-round.json --output benchmark_runs/planner-live-review.json
```

输出 JSON、Markdown 工作表和单独的 `.mapping.json`。复核包只含匿名计划 ID、题目 rubric、任务文字和空标注槽，不含模型、门禁 verdict、原始类别或 initial/final 阶段。初始/最终计划分别打乱展示，映射仅供协调者使用。采集失败的样本不进入语义标注，全部失败时拒绝导出空包。

## 完成标注后比较

```powershell
uv run python -m research.plan_sampling compare --collection benchmark_runs/planner-live-round.json --review benchmark_runs/planner-live-review.json --output benchmark_runs/planner-live-comparison.json
```

对每个真实样本比较初始计划与最后一次门禁保留计划：四轴明细、维度覆盖、从不合格变合格、从合格退化，以及每轴问题的解决/新增。保留调用耗时、已知 token 总量和被排除的采集失败数。token 缺失时总量为 null，不用 token 数直接假装货币成本。

该比较需要每个真实计划均完成标注，拒绝未经复核的包；合成复核包不能用于宣称真实 Planner 改善。接口错误、解析错误、规则修复、LLM 重规划收益分别记录。即使最终规则 verdict 是 pass，也不能跳过人工语义标注。

与原 [评估规范](PLANNER_EVALUATION.md) 一起使用。先复核题目和标签，再采集/标注真实计划，最后根据错误分布和成本决定是否做 Embedding 或 Judge 实验。
