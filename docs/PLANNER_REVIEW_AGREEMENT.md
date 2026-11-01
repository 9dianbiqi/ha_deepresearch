# 两份人工复核的一致性与裁决

本工具衡量标签之间是否一致，不判断语义标签是否正确，也无法证明两名复核者真正独立工作。输入必须是已完成的、针对同一来源的两份人工复核包；同一个计划的复核者名称相同（忽略首尾空格和大小写）会被拒绝。

## 当前材料

`backend/evaluation/planner_round3/` 已生成 reviewer_a.json / reviewer_b.json 和对应 Markdown。两份包含相同6个计划，展示顺序不同，所有标签、复核者、日期仍为空。只把各自的JSON/Markdown交给复核者；映射文件和原始标签由协调者保留。当前没有人工一致性结果。

用户选择优先复核现有6个计划。逐项AI复核依据见 [六计划复核草稿](PLANNER_SIX_PLAN_REVIEW.md)，供人工核对，不充当第二名人类复核者。若希望独立标注，先不要向标注者展示这个草稿。

## 运行

从backend目录执行。重新导出时使用新目录，现有目录会被拒绝以保护标注：

```powershell
uv run python -m research.plan_review_agreement prepare --collection evaluation/planner_round2_source.json --output benchmark_runs/two-reviewers
```

完成两份真实人工标注后：

```powershell
uv run python -m research.plan_review_agreement agreement --collection evaluation/planner_round2_source.json --left evaluation/planner_round3/reviewer_a.json --right evaluation/planner_round3/reviewer_b.json --output benchmark_runs/agreement.json
```

生成一致性结果及空白agreement.decisions.json。未填写标签、AI草稿、题目/任务被修改或来源不同的材料都不能生成结果。

## 指标

更新：允许uncertain并隔离Boundary Set。任一复核者对计划任一轴不确定，该计划就不进入本轮Core一致率/κ/正向Jaccard分母；仍报告全部分歧、Boundary数量和不确定比例。uncertain不当作第三种确定类别纳入κ。双方都不确定时可没有标签分歧，但has_unresolved_boundaries仍为true。

| 指标 | 计数单位 |
| --- | --- |
| relevance | 每个任务的0/1/2标签 |
| granularity | 每个任务的粒度类别 |
| coverage_binding | 每个任务×每个必需维度，是否绑定 |
| missing_dimension | 每个计划×每个必需维度，是否遗漏 |
| redundancy | 每个计划的所有无序任务对，是否重复 |

每项输出单位数量、匹配数量、准确一致比例、混淆矩阵和未加权Cohen κ。κ按 `(观察一致率 - 按边际分布预期一致率) / (1 - 预期一致率)` 计算，不加权相关性的0/1/2距离。零单位时一致比例和κ为null；边际分布退化使分母为零时κ为null，不能报告成完美可靠。

冗余还输出被至少一人标为重复的任务对集合的Jaccard比例。没有任何正向重复对时返回null，避免大量“不重复”对使整体比例显得很高。维度绑定同样可能被多数未绑定项主导，应一起看计划维度遗漏和逐项分歧。小样本不给稳定性或语义准确率结论。

任务对[1,5]和[5,1]、相同维度集合的不同排列不会产生假分歧。理由文字不同不直接算标签分歧，但会保留在材料中供复核。分歧清单标明计划ID、任务ID、评分轴、双方标签和理由，不用总分平均掉分歧。

## 人工裁决

agreement.decisions.json中的每项需要填写value和rationale；还要填写实际adjudicator、adjudicated_at，并在实际完成后将annotation_status改为human_reviewed。value格式：

- relevance：0/1/2或uncertain。
- granularity：too_coarse / appropriate / too_fine或uncertain。
- coverage：维度ID数组或uncertain。
- redundancy：关系对象数组，含task_ids、label（redundant/complementary/uncertain）和reason；保持兼容旧的确定冗余对象数组。没有确定冗余时可填[]，但 unresolved对必须明确写uncertain。

裁决值可以不同于双方标签，也可以继续uncertain，不能强迫二选一；通过schema后仍保留Boundary。来源或双方标签改变后旧裁决失效；每个分歧必须记录一次结论及理由。审计区分已处理分歧数量与仍然存在的Boundary计划数。无分歧也需实际签署。

```powershell
uv run python -m research.plan_review_agreement adjudicate --collection evaluation/planner_round2_source.json --left evaluation/planner_round3/reviewer_a.json --right evaluation/planner_round3/reviewer_b.json --decisions benchmark_runs/agreement.decisions.json --output benchmark_runs/adjudicated-review.json
```

结果包含最终复核包及独立audit文件，记录双方指纹、原分歧、裁决者、日期、值和理由。双方一致项采用左侧标签及理由，并由裁决者签署；不自动解决任何冲突。最终包可以进入上一轮import-review流程。真实LLM采样最终包才可进入compare流程，合成样本仍不能证明真实模型收益。
