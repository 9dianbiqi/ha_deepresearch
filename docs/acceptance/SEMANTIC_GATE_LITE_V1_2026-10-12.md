# 轻量 Semantic Gate 第一版验收收尾

整理日期：2026-10-12。实验及工程回归完成日期：2026-10-10。

**V1 FROZEN — OFFLINE ACCEPTED — PRODUCTION BLOCKING DISABLED**

冻结清单：backend/evaluation/semantic_gate_lite_v1_frozen_manifest.json，保存Lite源码、提示词、模型参数、测试数据和单元测试的指纹。修改这些项应另开版本，不覆盖本基线；当前没有启用Shadow Mode。

本次仅整理既有结果，未重跑已完成的模型调用、pytest、lint或类型检查。范围冻结为覆盖、冗余、相关性，使用单模型openai/gpt-oss-20b，不调用120B、不评分、不判断最优拆分，不接入生产阻断或自动改写。

## 已完成结果

| 运行 | 三项状态与参考标签全一致 | 技术失败 | uncertain | 误拒/漏判 | 其他分项错配 |
| --- | --- | --- | --- | --- | --- |
| 初始提示v1，12开发案例 | 5/12 | 5 | 0 | 0/0 | 2 |
| 修正提示v1.1，同12案例 | 12/12 | 0 | 0 | 0/0 | 0 |
| v1.1重复6个固定案例 | 6/6 | 0 | 0 | 0/0 | 0 |
| v1.1另6个新案例 | 6/6 | 0 | 0 | 0/0 | 0 |

v1.1共24次观察、18个不同案例，不是24个独立样本，也不能宣称生产准确率100%。全部三项状态与开发参考一致；其中只有6个固定案例得到第二次观察，不宣称12例都做了多次稳定性验证。新6例未用于本轮提示修正，不是模型训练未见数据的证明，也不是独立人工金标准。

初始版本的0误拒/0漏判不能理解为全部成功：5个技术失败按不确定放行，另2个结果把偏题同时误定位成覆盖不足，完整诊断只有5例匹配。失败记录保留，未覆盖或删除。

## 修复明确问题

20B初始输出混淆用户原文引用与任务引用：引用topic却填任务ID，或引用任务字段却用null。校验拒绝这些结果，技术失败没有触发修订。

v1.1只补充最小格式示例：topic必须task_id=null、任务必须真实ID、全pass可以不给证据、missing应定位具体未覆盖片段。没有新增评分、投票、研究维度或宽松放行非法引用。

Python继续验证任务编号、原文连续片段、失败定位、重复对两端证据以及整体/分项状态一致性。引用合法不保证语义判断正确。

## 实际流程边界

- pass：形成有效通过观测。
- fail：只有编号、证据和状态都合法，才给出反馈及replan_recommended；本版不调用Planner，不删除任务。
- uncertain：保留计划，记录不确定。
- API错误、截断、JSON/引用/状态错误：记录technical_failure及校验阶段，不给修订指令。
- 全部调用为离线，production_blocked=false；正常研究链路继续使用确定性Plan Gate V1。

覆盖缺失没有对应任务时引用用户明确要求，checked_task_ids记录受检范围，不能编造缺失任务编号。只核对用户显式要求，不擅自补入行业研究维度。

## 已有工程回归（沿用，不重跑）

2026-10-10完成：787 passed、2 skipped、22 subtests passed（83.11秒），Ruff通过，mypy检查71个源文件通过，新增文件格式检查通过。该结果验证工程回归，不替代语义准确性评测。

## 可复核记录

- 初始调用：backend/benchmark_runs/semantic-gate-lite-dev12-2026-10-10.json。
- 修正版：backend/benchmark_runs/semantic-gate-lite-dev12-prompt11-2026-10-10.json。
- 小范围复核：backend/benchmark_runs/semantic-gate-lite-verification-2026-10-10.json。
- 汇总与原记录SHA-256：backend/evaluation/semantic_gate_lite_acceptance_2026-10-12.json。
- 开发案例与复核案例分别保存在backend/evaluation/semantic_gate_lite_dev12.json、semantic_gate_lite_verification12.json。

原始调用被既有Git忽略规则排除；汇总不含密钥。参考标签为AI起草的明显开发案例，不能称为人工金标准。

## 封版与下一步

本轮开发冒烟目标已达到，可冻结这份轻量离线第一版；不代表生产可靠性已验收。正式阻断误拒率/漏判率目标尚未设置或满足，没有足够独立验证。

停止重复已完成测试。下一阶段为[Shadow Evaluation](../SEMANTIC_GATE_SHADOW_NEXT.md)：在真实Planner输出上旁路记录，只积累需复核的Badcase，不影响正常规划；不恢复复杂多模型研究方案，不自动打开阻断。
