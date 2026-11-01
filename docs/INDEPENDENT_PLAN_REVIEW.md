# 不同模型的反馈与独立评审

反馈生成与效果评审必须由不同的实际模型承担，不能只给同一个模型换角色提示词。旧实验中Codex提出反馈并复评，仍保留为探索性案例，不升级为独立模型验证。

## 配置与密钥

密钥填在本地 `backend/.env`，这个文件已被Git忽略。`.env.example`只放占位符，不填真实密钥。当前用户指定分工：

| 角色 | 模型 / 服务 | 工作 |
| --- | --- | --- |
| Planner | 智谱glm-4.7-flash | 生成对照与修改后的计划 |
| Feedback | 临时Groq openai/gpt-oss-20b | 根据冻结rubric找缺口并生成反馈 |
| Independent Judge | Groq openai/gpt-oss-120b | 只看匿名计划和冻结标准，独立标注 |

Planner与Feedback可以共用模型，Feedback与Judge必须不同。用户选择的新模型替代方案需要另行明确记录，不会静默切换。

用户在2026-10-09明确同意暂时更换Feedback，当前本地配置已改为Groq20B，Planner的LLM_MODEL_ID仍为GLM；Judge保持120B。下面GLM配置是原始配置，临时覆盖如下：

```dotenv
PLAN_FEEDBACK_MODEL_ID=openai/gpt-oss-20b
PLAN_FEEDBACK_BASE_URL=https://api.groq.com/openai/v1
PLAN_FEEDBACK_API_KEY=${JUDGE_API_KEY}
PLAN_FEEDBACK_THINKING=provider_default
PLAN_FEEDBACK_REASONING_EFFORT=low
PLAN_FEEDBACK_MAX_TOKENS=2000
```

真实密钥只存JUDGE_API_KEY，dotenv在本地解析该引用；不复制密钥到实验记录。恢复GLM时改回模型名，清空Feedback专用BASE_URL/API_KEY以回退到LLM配置，设THINKING=disabled并清空REASONING_EFFORT。此次临时覆盖不改变在线门禁行为。

```dotenv
PLAN_FEEDBACK_MODEL_ID=glm-4.7-flash
PLAN_FEEDBACK_THINKING=disabled
PLAN_FEEDBACK_MAX_TOKENS=2000
JUDGE_MODEL=openai/gpt-oss-120b
JUDGE_BASE_URL=https://api.groq.com/openai/v1
JUDGE_API_KEY=在本地填写真实Groq密钥
JUDGE_REASONING_EFFORT=low
JUDGE_MAX_TOKENS=4096
```

同时支持PLAN_JUDGE_MODEL_ID / BASE_URL / API_KEY等长变量名，非空长变量优先。Feedback可以沿用同一个LLM_BASE_URL及LLM_API_KEY；Judge切换到不同API地址时，禁止自动复用智谱密钥，必须有自己的JUDGE_API_KEY。占位符不会被当作有效密钥。

模型对象在序列化和repr中不包含key及API地址。记录只包含模型名称、采样设置、prompt指纹、实际返回模型、用量与耗时。角色身份写在协调器记录中，不写进Judge计划输入。

Groq官方列出[openai/gpt-oss-120b](https://console.groq.com/docs/model/openai/gpt-oss-120b)；其[API文档](https://console.groq.com/docs/api-reference)支持JSON Object模式及该模型的low/medium/high reasoning设置。本工具使用low与JSON Object模式，再用本地schema严格验证内容，不把有效JSON等同于有效判断。

## 独立与匿名输入约束

1. 配置阶段归一化模型ID，忽略大小写、常见连接符和供应商命名空间；相同模型的别名不算独立。
2. 每次API调用记录requested_model_id和actual_model_id。Feedback与Judge实际返回同一模型，或返回unknown/default等无法辨认的身份时，实验不能标为分离验证完成。
3. Judge只收到plan_id、题目、scope、必需维度、任务数建议，以及任务的id/title/intent/query。
4. 不发送Feedback文本、模型身份、条件arm、规则分数、旧AI标签、初始/修改版标识或协调器映射。
5. 输入按固定种子打乱。每份计划使用一个新的无历史SDK请求，不把前一份评审结果带入下一份。
6. 映射与角色记录另存；只做到结构上的匿名化，不能保证评审模型无法从任务文字推测版本。

实际API报告不同ID是模型分离的证据，不是权重的密码学证明，也不保证两模型错误独立。结果仍不是人工金标准。

## 判断格式

Feedback输出action（replan/keep/uncertain）与有任务/维度引用的findings。只有replan且存在有效finding才发起修改；keep或uncertain不会强迫修改。

Judge逐任务评价相关性和粒度，逐必需维度评价covered/partial/missing/uncertain，并对全部无序任务对标注redundant/complementary/uncertain。引用必须来自输入，不能省略任务、维度或任务对，不能输出一个无法解释的总分。

本地schema位于 `backend/evaluation/independent_review_schemas.json`。任何不确定进入Boundary，acceptable为null；维度状态不确定时覆盖比为null，另存已确认覆盖下界。缺失标签、格式错误、截断和API失败不能算pass。

## 执行

从backend目录执行，使用新目录保护历史记录：

```powershell
uv run python -m research.independent_plan_review --baseline benchmark_runs/glm47flash-planner-no-thinking-pilot-2026-10-08.json --output-directory benchmark_runs/independent-new-run
```

先用Feedback模型真实生成反馈，再以相同旧计划上下文、冻结基础prompt、模型和参数生成新对照/干预计划，随后让Judge匿名评审。使用原记录的基础prompt日期，不能因为跨日就改变对照输入。

- `protocol.json`：实际角色、Feedback输出、Judge原始结果和严格校验状态。
- `planner.json`：对照/干预调用与原始生成结果。
- `judge_inputs.json`：实际匿名评审载荷。
- `coordinator_mapping.json`：只有协调者使用的计划ID映射。

Feedback供应商失败时停止，尚未发出的Judge请求不造结果。Planner失败时可评审原计划，验证反馈发现的缺口，但状态明确为independent_review_complete_replan_incomplete；没有配对改善结论。流程只影响离线实验，未接入生产门禁。

## 当前真实状态（2026-10-09）

- 用户已在本地填入Groq密钥。
- Groq小请求测试成功，实际返回openai/gpt-oss-120b、stop及有效JSON。
- 前两次完整实验的GLM Feedback请求返回429/1305；未发起修改或Judge评审。
- Feedback输出预算从4096降到2000后，一次请求约49秒超时；这也没有产生可用反馈。
- 以上失败均有独立目录和记录，不能称为正式分离模型实验成功。
- 旧Codex反馈+复评案例仍保持原标记，不把配置模型名称追填成它的实际作者。

以上是切换临时模型前的故障记录。用户随后明确同意更换Feedback，结果如下。

### 临时模型实测已完成

实际返回Feedback=openai/gpt-oss-20b，Judge=openai/gpt-oss-120b，模型分离和匿名输入检查通过。Feedback输出keep、findings=[]，因此未调用Planner；Judge覆盖判4/4，但标任务5为too_coarse。

这是决策分歧，不是证明Feedback错误或Judge正确。协调层划入Boundary，保留原始Judge的Core标签。后续检查发现Judge把任务5描述为研究价格、政策、增长，而原任务只明确战略、产品节奏和差异化；结构schema无法验证这种语义外推。因此不能把4/4作为可靠真值。

记录见 [独立模型实测](acceptance/INDEPENDENT_MODELS_2026-10-09.md)。当前分离链路可运行，语义判断仍需校准；没有修改前后收益结论。

后续已完成[组分与原文证据校准V2](acceptance/GROUNDED_CALIBRATION_V2_2026-10-09.md)：120B能区分两个候选的覆盖，小模型在正向候选上全部拒判，两例仍保留Boundary，尚未支持默认上线。

正式状态：**CALIBRATION COMPLETED — SEMANTIC GATE NOT READY**。下一阶段限定为[拒判诊断及24样本人工金标小实验](SEMANTIC_CALIBRATION_NEXT_STAGE.md)，暂不扩展在线语义门禁。
