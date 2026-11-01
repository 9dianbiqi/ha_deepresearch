# GLM-4.7-Flash 与不确定标注验收

日期：2026-10-08。

## 模型切换与真实接口测试

- 本地backend/.env的模型已切换为glm-4.7-flash，示例配置同步更新；API key保持原值，没有进入版本控制。
- 第一次小请求：HTTP429、业务码1305，原文「该模型当前访问量过大，请您稍后再试」。与此前glm-4.5-air的1113额度错误不同。
- 稍后复测：返回OK、finish_reason=stop、总用量11 tokens；关闭thinking，输出上限256、SDK重试0。
- 生产默认请求模式的单主题试采：2000输出tokens、finish_reason=length，JSON截断，解析为unparsed。该记录不是有效计划，也不是语义质量失败结论。
- 关闭thinking的同主题试采：成功生成5个任务，fenced JSON解析成功，词法门禁pass，词法覆盖0.6579。该模式差异明确记录，不冒充生产默认设置。
- 扩大到开发主题批量采样时再次遇到RateLimitError，作业在首个供应商错误后停止，未完成全量采样。

接口通过不代表整个生产研究链路已验证。运行中的后端需重启以加载新的.env；离线采样支持thinking参数，生产推理模式尚未新增专门配置。

## 人工评测的实际进度

已生成 `backend/evaluation/glm47flash_real_review.json` 与Markdown，包含成功样本的初始/最终两个相同快照。

用户明确回复：任务1（规模、销量与份额）和任务5（战略、产品节奏与差异化）为complementary，理由是「能产生不同结论」。这条关系已记录在两个同内容快照中，作为部分人工判断；其余任务相关性、粒度、维度绑定和其他任务关系仍待填写。整体包保持unreviewed，未输出整条计划的人工准确率。

固定样本4ee47a78e50c810f中，任务1、5的品牌/集团口径不清，按照用户给出的具体理由改为uncertain。这个判断与真实计划中更明确的战略目标区分记录。

## 不确定标注机制

- task relevance、granularity、covered_dimensions支持显式字符串uncertain；null仍表示未标注。
- pair_relations支持redundant / complementary / uncertain及reason，兼容旧redundant_pairs；同一对的矛盾标签、重复关系或悬空ID被拒绝。
- 任何显式不确定将整个计划路由至Boundary。acceptable为null，不强行判好坏。
- Core的误干预、召回、修复指标排除Boundary，单独报告不确定比例及边界记录。
- 两人一致性中，任一方不确定的计划不进入Coreκ/一致率分母；保留分歧及边界情况。
- 裁决可以保持uncertain，审计分别报告已处理分歧数与仍未确定的Boundary计划数。
- 新版本planner-seed-v1.1.0：开发分组36个候选，35个Core、1个Boundary；Core为6个参考合格、29个参考有问题，规则干预13个、未干预16个。召回13/29仅描述草稿诊断集；与旧13/30的变化来自标注/分母调整，不是规则算法提升。
- 原六计划子集：5个Core（1合格、4有问题）、1个Boundary，不再把模糊任务对当作一次确定漏检。

在线Plan Gate保持V1；高置信度删除、互补保留、不确定澄清仍属于未来V2实验建议，本次没有宣称实现语义门禁。

## 验证

- 后端全量测试：741 passed、2 skipped、22 subtests passed（93.51秒）。
- 新增10个不确定性用例：边界排除、未知不算好坏、明确的三种关系、空白与uncertain区分、冲突/悬空引用、两人一致性及保留不确定的裁决。
- Ruff通过；mypy检查66个源文件通过。
- 基线JSON与标注schema重新生成；已有黄金文件测试通过。
- 实际GLM接口和Planner试采证据保留在已忽略的backend/benchmark_runs目录，不含API key。

完整不确定规范见 [评估说明](../PLANNER_EVALUATION.md)，真实人工材料见 [工作表](../../backend/evaluation/glm47flash_real_review.md)。
