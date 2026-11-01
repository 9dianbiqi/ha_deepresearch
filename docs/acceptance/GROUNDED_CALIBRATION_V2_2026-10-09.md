# 组分与原文证据校准 V2

日期：2026-10-09。反馈侧openai/gpt-oss-20b，独立评审侧openai/gpt-oss-120b。

**CALIBRATION COMPLETED — SEMANTIC GATE NOT READY**

本轮工程回归通过，不代表语义准确性验收。保留确定性Plan Gate V1，语义评审仅为独立离线实验；20B或120B的结论不直接阻断正常规划。下一阶段范围冻结为[小规模人工金标校准计划](../SEMANTIC_CALIBRATION_NEXT_STAGE.md)，不继续扩展在线语义门禁功能。

## 结果

本轮冻结6个研究组分，要求每项covered都引用任务原字段的连续原文。使用一个主题的两份真实捕获计划：原始5任务计划、前次反馈实验生成的6任务补充候选；没有改写任务文字，没有重新调用Planner。

| 计划 | 20B覆盖检查 | 120B覆盖检查 | 协调结果 |
| --- | --- | --- | --- |
| 原计划 | 4组分covered；盈利压力、未来不确定性为uncertain；4条原文引用 | 同4组分covered；另外2项missing；4条原文引用；完整维度2/4 | Boundary，保留missing/uncertain分歧 |
| 补充候选 | 6组分全部uncertain，0条引用 | 6组分covered，6条原文引用；完整维度4/4 | Boundary，保留全部组分分歧 |

120B不再沿用V1中对原计划4/4的笼统覆盖，也没有借用任务5去声称价格/政策目标。原来的原始评分保留，不回写成新评分。

20B的正向结果不是通过：虽然JSON和标签结构合法，且uncertain允许空引用，但它没有识别输入中已经明确写出的价格/利润、政策影响、未来情景与风险目标。其理由反复称没有提供相关数据，疑似混淆了任务承诺和研究产出；这是事后解释，原因尚未验证，不能仅凭一次响应归因于模型大小。

四次API调用成功，总用量6898 tokens。协调层2/2样本均为Boundary，没有主要准确率、召回率或“两个模型全部评审通过”的结论。

## 冻结组分

| 原维度 | 组分 |
| --- | --- |
| share | 品牌份额/竞争位置；明确份额可支持集中度计算 |
| pricing | 价格竞争/定价；利润、毛利、成本或盈利压力 |
| policy | 政策/监管对市场或竞争的影响 |
| growth | 增长驱动；明确未来预测/情景、下行风险或不确定性 |

维度只有全部组分covered才为covered，部分covered加missing为partial，含uncertain则维度uncertain。多个任务可以共同贡献，不要求一个任务包揽整个复合维度。

组件在API请求前写入frozen_packet.json。两模型看到相同匿名任务和组分，不看到对方结果、模型身份、旧评分或改版标签。原计划和补充候选的ID由任务内容生成，来源关系保留在协调资料中。

这属于开发集校准：V2改变了标准解释的细度、检查任务和输出格式，不是严格同提示词A/B，也不是独立持出验证。不能把120B评分从4/4变2/4直接说成准确率提升。

## 原文校验边界

每条引用包含task_id、field（title/intent/query）和quote。covered至少有一条；task与字段必须存在，quote必须是对应字段中原样连续片段，不能翻译、改写、引用别的任务或凭空编造。

实际四份结果共14条引用均通过逐字校验；其中20B补充候选为零引用。零引用的全uncertain判断只是合法拒判，不代表存在支持证据。

**逐字匹配只验证引用身份，不验证引用语义是否足以支持组分。** 例如引用真实的「价格竞争」仍不能证明计划承诺研究盈利压力。程序明确记录semantic_quote_support_verified=false，测试也锁定这个能力边界。

20B原计划主要引用短标题，部分引用的解释力弱于120B完整intent引用，仍需检查。自动校验不是语义真值证明，模型一致也不等于正确。

## 参数与限制

两角色temperature=0、reasoning_effort=low；20B输出预算2000、120B输出预算4096，响应均stop、未截断。通用请求器只对Judge设置JSON Object模式，反馈侧依赖文本指令。参数与格式差异保留，不把全部差异归因于模型尺寸。

两个模型同属GPT-OSS家族，错误可能相关。单主题的两个相关计划也不足以检验泛化。本轮只校准覆盖，不重评相关性、冗余、粒度，也没有生成新反馈建议或触发在线门禁。

## 文件与复现

- `backend/src/research/grounded_plan_calibration.py`：V2冻结组分、引用校验与离线采样。
- `backend/evaluation/grounded_calibration_v2_2026-10-09.json`：两个样本的完整原始标签、引用、参数、token及协调状态。
- `backend/benchmark_runs/grounded-calibration-v2-2026-10-09/`：原计划调用和冻结输入。
- `backend/benchmark_runs/grounded-calibration-v2-positive-2026-10-09/`：补充候选调用和冻结输入。
- 补充候选适配文件保存原实验指纹及feedback_1来源，仅调整记录封装，不修改任务内容。

从backend目录执行，用新输出目录保留历史：

```powershell
uv run python -m research.grounded_plan_calibration --baseline benchmark_runs/glm47flash-planner-no-thinking-pilot-2026-10-08.json --output-directory benchmark_runs/calibration-new-run
```

不要为了获得满意分数反复覆盖原始结果。API失败、格式/引用失败、合理不确定和无信息拒判应分别记录。

## 下一步

V2让覆盖结论更可审查，但当前反馈侧还没验收。下一轮应先针对20B做输入/输出设置的单因素检查，或在明确记录模型变更后比较另一反馈模型；同时在更多预先定义的正负样本上检验拒判率和错误覆盖，不能只调到这一个主题得分满意。

保持离线实验，保留Boundary，不默认启用语义门禁。

## 验证

- 后端全量775 passed、2 skipped、22 subtests passed（83.50秒）。
- 新增11个校验用例覆盖原文真实性、任务/字段错配、翻译或编造引用、组分缺失/重复、covered无证据、uncertain分流、无组分维度和未知模型身份。
- 测试明确验证：真实但不相关的引文仍可能通过逐字检查，程序不得把它宣称为语义已验证。
- Ruff通过；mypy检查69个源文件通过；新增文件格式检查通过。
- 四份实际输出重新校验，与冻结输入指纹和原始模型记录一致；旧V1评分未覆盖。

全组分拒判与研究任务自身的合理模糊边界应分别分析。20B补充候选的全uncertain是无信息拒判信号，暂存Boundary只是避免过度下结论，不证明该计划本身存在合理歧义。
