# Planner 固定评估集 V1

这是规划质量评估的起点：固定题目、固定候选计划、可复核的参考标注，以及可重复的 Plan Gate V1 离线基线。它不增加运行时模型调用，不改变现有规划流程。

## 交付与标注状态

- 数据集：`backend/evaluation/planner_v1.json`，版本 `planner-seed-v1.1.0`。
- 标注结构：`backend/evaluation/planner_dataset.schema.json`。
- 规则基线：`backend/evaluation/planner_v1_baseline.json`。
- 执行器：`backend/src/research/plan_evaluation.py`。
- 可读结果：`docs/acceptance/PLANNER_BASELINE_2026-10-08.md`。

**样本为 Codex 构造的 synthetic seed，多数标签仍为 `ai_draft`。** 用户明确给出新能源汽车品牌/集团关系的uncertain判断，已纳入v1.1.0；这不代表整套标签完成了人工复核。数据不代表生产请求分布，不得将草稿数字作为线上准确率。

共 8 个领域主题、48 个候选计划：新能源汽车市场、框架选型、版本新特性、企业 RAG 架构、数据库职责、隐私研究、本地模型部署、英文电池生命周期。每个主题都有合格计划、缺失维度、同义重复、原样重复控制和完全偏题控制；另配置无关额外任务、过粗或过细的粒度样本。

按整个主题划分：6 个 development 主题 / 36 个候选，2 个 holdout 主题 / 12 个候选。同一题目的所有候选必须在同一分组，避免同题变体泄漏。英文题目目前在 holdout，因此 development 基线不证明英文效果。holdout 是预留分组，并非未被作者见过的盲测集；同样的合成方法也限制了它的独立性。

## 四个标注轴

允许显式不确定：相关性、粒度和covered_dimensions均可填字符串uncertain；任务对使用pair_relations中的redundant / complementary / uncertain及reason。null仍表示未标注，会被拒绝。不确定不是第三种确定真值。

任务对例子：`{"task_ids":[1,5],"label":"uncertain","reason":"未明确品牌与集团的分析口径，无法确认独立结论"}`。不要同时把该任务对写入确定的redundant_pairs；矛盾标签会被拒绝。

Core / Boundary与development / holdout是不同分组轴。任何一项显式uncertain都把整个计划路由到Boundary；Core按确定标签计算主要指标，Boundary单独保留诊断与不确定比例，不进入误放行、召回、修复率等主要分母。当前development的36个候选中35个Core、1个Boundary。即使规则删除了模糊任务，本轮也不自动把该计划升级为确定合格。

旧格式redundant_pairs保持兼容：按标注者声明的完整重复对列表统计，未列出的对按非重复处理；新格式可显式记录互补或不确定。工具不能替人工判断哪些未列任务对实际有歧义。

| 轴 | 字段与取值 | 规则 |
| --- | --- | --- |
| 任务相关性 | 每任务 `relevance`：0 / 1 / 2 | 0 完全偏题；1 只部分相关或只是操作步骤；2 直接服务于问题要求 |
| 语义冗余 | 计划 `redundant_pairs`：任务 ID 对及理由 | 目标、对象、范围和预期结论实质重复，即使措辞不同；共享关键词不自动代表重复 |
| 关键维度遗漏 | 题目 `required_dimensions`，任务 `covered_dimensions` | 先确定题目要求，再绑定任务覆盖维度；所有任务覆盖的并集之外就是缺失维度 |
| 拆解粒度 | 每任务 `granularity`：`too_coarse` / `appropriate` / `too_fine`，题目 `task_count_range` | 多个独立问题揉成一个任务为过粗；只打开网页等步骤为过细；合理任务可独立检索并形成结论 |

每个题目必须有 `scope`，每个任务和冗余对必须写 `rationale`。维度只描述研究要求，不在评估集中断言行业事实。例如新能源汽车题目明确要求份额、价格竞争、政策、增长驱动，不自动把电池或充电设施判为必须维度。

`covered_dimensions` 是依据计划目标给出的覆盖绑定，不证明执行后得到证据或正确结论。不能因任务只写“全面研究”就绑定全部维度。复合维度可以由多个互补任务共同完成，但标注者必须记录各组分由哪些任务承担；没有其余组分时，单个部分目标不能算整个维度已完整覆盖。相关性2表示直接服务题目，不等于完整覆盖。需要拆分维度时应先修改rubric并升级版本，不能看到结果后改标准。

Core中“合格”是当前rubric的严格合取：全部任务相关性为2、无确定冗余、覆盖全部必需维度、粒度合理且任务数在建议范围内。Boundary的acceptable为null；不把不确定折算为合格或不合格。覆盖不确定时dimension_coverage为null，并单独记录unresolved_dimensions。

结构验证会拒绝重复 ID、悬空冗余对、未知维度、缺少必填标签、标签与候选类型冲突以及没有复核者却声称 `human_reviewed` 的数据。结构验证不能验证标签本身是否正确。

## 运行

从 `backend` 目录执行（使用仓库源码与已有 uv 环境）：

```powershell
uv run python -m research.plan_evaluation --output evaluation/planner_v1_baseline.json
uv run pytest -q tests/test_plan_evaluation.py
```

默认只跑 development。阈值实验输出到本地运行目录，避免覆盖固定基线：

```powershell
uv run python -m research.plan_evaluation --overlap-threshold 0.65 --coverage-threshold 0.6 --output benchmark_runs/planner-thresholds.json
```

方案与阈值冻结后才运行预留分组：

```powershell
uv run python -m research.plan_evaluation --split holdout --output benchmark_runs/planner-holdout.json
```

也支持 `--dataset` 和 `--split all`。结果保存数据版本、规范化数据 SHA-256、门禁实现 SHA-256、评估器版本和阈值。不写时间戳，保证相同输入得到相同输出。门禁实现指纹包含 `plan_quality.py` 和提供分词的 `history.py`；历史基线变化时应说明是标签、规则还是阈值变化。

## 指标如何解读

本次只调用一次真实 `PlanValidator.validate()`。**不调用 LLM 重规划，不模拟失败开放后的实际检索，不执行 profile 任务预算截断。** `retry` 只表示门禁发出反馈信号，不能据此声称计划已修复或流程阻止了坏计划执行。

- `gate_action`：verdict 为 `pass_with_repairs` 或 `retry`，表示有干预信号。
- `true_alerts` / `missed_bad_plans`：参考标签有问题的计划中，有干预 / 无干预的数量。
- `false_alerts`：参考标签合格却被规则干预的计划数。
- `alert_precision` / `alert_recall`：只衡量通用干预信号与参考标签的对应，不代表每个问题被正确定位。
- `bad_plans_repaired_to_acceptable`：按保留下来的任务 ID 重投影参考标签后，坏计划确实变合格的数量。
- `reference_bad_retained_without_retry`：修复后仍存在标注问题且门禁没有请求重规划的数量，包括 `pass_with_repairs` 残留问题。
- `repairs_losing_dimensions`：去重导致新增遗漏维度的计划数，帮助发现误删互补任务。
- `issues`：四轴各自的问题计划数、无干预数、修复后残留数；同一计划可在多个轴计数。

没有分母的比率返回 `null`，不假装是 0 或 100%。每候选结果包含修复前后四轴、缺失维度、冗余对、保留任务 ID 和规则指标，支持人工追查。这里的“没有请求重规划”不同于整个生产系统的最终放行结论。

## 人工复核与真实 Planner 评估

已提供可执行的 [第二轮采集与复核流程](PLANNER_REVIEW_WORKFLOW.md)，包含真实采集 CLI、空白复核工作表、标注导入与计划前后比较。当前真实试采返回 RateLimitError，人工复核尚未完成。

1. 先审题目 `scope`、必需维度、任务数量建议，再审候选计划，避免看见模型结果后临时改变要求。
2. 建议两人独立标注，隐藏候选 `kind`、模型名称与门禁结果；记录各轴分歧，由复核者裁决并保留修改理由。`kind` 仅用于样本组织，不应作为答案提示。
3. 调整 `rationale`、相关性、冗余对、粒度或维度绑定；保持同题分组。只有完成真实复核才更新 `annotation_status=human_reviewed`，填写 `reviewers`，升级数据版本并重跑基线。建议单独保存复核记录，记录日期、标注者、分歧和裁决；字段本身不构成复核证明。
4. 在相同题目上采集真实 Planner 输出，再按固定 rubric 标注新候选。记录 prompt/模型版本、温度、运行次数、耗时和 token 用量；不能自动把合成候选的标签转移给新输出。
5. 对同一批输入对比初始计划和反馈重规划后的计划，记录四轴改善、退化、成本与任务截断损失。随机模型输出应多次采样。不要仅凭门禁 `pass` 判定变好。

当前没有成功采集真实计划或完成人工复核。评估器接受的是已标注候选数据；它不能自动为新模型输出生成可信语义标签。空计划也可进入标注，以明确记录缺失维度。

## 决定 Embedding 或 Judge 的条件

先在人工复核的 development 上确认哪些错误频繁发生，再做最小候选实验：

- 同义重复为主要问题时，试验 Embedding 作为冗余候选信号，重点检查互补任务的误删和覆盖损失。
- 关键维度遗漏或粒度不当为主要问题时，试验带题目 rubric 的 LLM Judge，要求返回任务/维度引用和理由，不接受只给总分。
- 任务相关性可同时比较规则、Embedding 和 Judge；相似度低不自动等于无关。

在实现前写定可接受的误干预、退化、延迟和调用成本范围，在冻结后的 holdout 上检查收益，再扩展真实题目集。这个小规模合成集是诊断集，不足以直接给出上线阈值或统计显著结论。
