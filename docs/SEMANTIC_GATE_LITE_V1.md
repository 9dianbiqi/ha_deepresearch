# 轻量 Semantic Gate 第一版（当前范围）

日期：2026-10-10。当前为离线开发模块，不接入生产阻断。

2026-10-12正式状态：**V1 FROZEN — OFFLINE ACCEPTED — PRODUCTION BLOCKING DISABLED**。修正版12个开发案例及6个重复/6个新案例检查已完成，无需重跑。完整结果见[离线第一版验收](acceptance/SEMANTIC_GATE_LITE_V1_2026-10-12.md)，指纹保存于backend/evaluation/semantic_gate_lite_v1_frozen_manifest.json。

24次观察对应18个不同案例，不是24个独立样本，不宣称准确率100%。当前没有接入Shadow；下一阶段为[真实规划旁路观测](SEMANTIC_GATE_SHADOW_NEXT.md)，不继续扩展语义检查范围。

## 功能冻结

只检查三项：用户明确要求的语义覆盖、任务目标的语义冗余、任务与需求的相关性。不做质量总分、最优分解、粒度评分、多模型投票或自动改写整份计划。使用单模型openai/gpt-oss-20b，当前实现不调用120B。

确定性Plan Gate V1继续负责已有解析、格式、任务数量、预算及词法去重。Semantic Gate是它之后的可选实验边界，尚未接入agent.py或正常研究请求。

## 简单接口

输入只有topic与id/title/intent/query任务列表。输出为coverage、redundancy、relevance、overall、feedback，以及支持fail的evidence。

- 任一检查fail，overall必须fail；否则有uncertain则overall为uncertain；其余pass。
- coverage.missing只记录用户明确要求的原文片段，不擅自增加行业维度。完全遗漏没有对应任务，引用topic且task_id=null；checked_task_ids列出受检范围，不编造任务编号。
- redundancy.duplicate_pairs必须是两个不同的已有ID，且双方各有对应原文证据。
- relevance.irrelevant_tasks必须是已有ID，每个偏题任务有原文证据。
- fail必须有可执行feedback；pass/uncertain不能声明确定的问题列表。
- 所有引文必须是其指定topic或任务字段的连续原文。引用真实性不证明语义判断正确。

有效fail只返回replan_recommended，供未来受控修订使用；模块不删除任务，不调用Planner。uncertain、API错误、截断、JSON/状态/引用校验失败都不返回拒绝或修订指令，记录原因。全部结果标mode=offline、production_blocked=false。

## 文件

- `backend/src/research/semantic_gate.py`：单次语义判断、结果解析、编号/引文/整体状态校验。
- `backend/src/research/semantic_gate_eval.py`：简单离线开发测试执行器。
- `backend/evaluation/semantic_gate_lite_dev12.json`：12个明显案例，每项检查2个通过和2个失败。
- `backend/tests/test_semantic_gate.py`：解析、证据、失败开放及指标分类测试。

案例参考标签由AI按明显目标差异起草，用户允许先标这些明确例子；它们不是复杂行业人工金标准，更不是生产可靠性证明。

## 模型配置

当前继承本地PLAN_FEEDBACK_MODEL_ID/BASE_URL/API_KEY，即Groq20B及用户已填密钥，不读取或调用Judge模型。也可单独设置：

```dotenv
SEMANTIC_GATE_MODEL=openai/gpt-oss-20b
SEMANTIC_GATE_BASE_URL=https://api.groq.com/openai/v1
SEMANTIC_GATE_API_KEY=在本地填写对应服务密钥
SEMANTIC_GATE_REASONING_EFFORT=low
```

不同API地址不能静默复用另一个服务的密钥。temperature=0，输出上限2048，30秒超时，SDK请求级自动重试关闭。原始输出、实际模型、用量与校验阶段保存在本地测试记录，不保存密钥。

## 测试

在backend目录执行，输出使用新路径保留旧结果：

```powershell
uv run pytest -q tests/test_semantic_gate.py
uv run python -m research.semantic_gate_eval --output benchmark_runs/semantic-lite-new.json
```

默认每样本1次，间隔20秒，支持--repetitions 2或3。分类记录三项状态都匹配、误拒、漏判、其他分项错配、uncertain和技术失败，分母不偷偷排除失败。

本轮开发冒烟目标是12个明显案例全部给出正确三项状态及有效定位。这个目标不是生产验收阈值。正式阻断的误拒/漏判标准尚未设置或满足，不能以一次开发集全通过就启用阻断。

## 范围替代与保留

此前24样本金标、双模型评审和组件校准保留为历史实验，不作为第一版的交付前置条件，不继续增加复杂层。当前只收敛本模块、解决明显拒判/JSON/定位问题，随后做少量稳定性与未见案例检查。

所有生产语义判断仍未启用；继续保留确定性V1，不让20B或120B直接阻断正常规划。
