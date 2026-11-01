# 本地状态与提交建议

日期：2026-11-01。已刷新origin引用，仅做状态、依赖、冻结指纹与候选文件检查；没有重跑pytest、模型调用，没有提交或推送，没有删除分支/工作树。

## 结论

**可以提交确定性Plan Gate及离线Semantic Gate Lite V1，但不建议把整个工作区一次性提交。** Lite核心冻结指纹一致、依赖可独立整理；当前工作区还混有历史研究框架、辅助报告及临时诊断脚本。

提交不是生产语义阻断验收。状态仍为V1 FROZEN — OFFLINE ACCEPTED — PRODUCTION BLOCKING DISABLED，Shadow尚未接入。

## Git状态

检查开始时共有66个候选文件：3个已跟踪文件有未提交改动、63个未跟踪文件，总约713KB。git status默认会合并显示evaluation目录，因此条目数少于实际文件数。本文是本轮另加的审查记录，不包含在上述66个中。

| 分支 | 当前情况 | 建议 |
| --- | --- | --- |
| codex/evidence-v1-integration | 本地ffa9800；远程同名分支5e6ac62，本地多1个提交 | 保留原现场；不要直接打包所有未提交改动推到旧集成分支 |
| origin/main | 6d81394，旧PR #4已合并；相对当前HEAD各有1个独有提交 | 新功能基于此建立干净分支 |
| 本地main | 71d9e6c，相对origin/main仅落后23个提交 | 另在干净上下文快进同步，不用reset --hard |
| origin/agent/improve-repository-presentation | 7de8185；比origin/main少36个主线提交，有2个未合并文档提交 | 只涉及README，另行检查/同步，不混入Lite提交 |

当前HEAD相对origin/main唯一独有提交是ffa9800（确定性Plan Gate）；origin/main相对HEAD唯一独有提交是6d81394（合并提交）。origin/main的文件树与ffa9800的父提交一致，因此从origin/main重新承接ffa9800有清晰的基线，不需要强推改写旧远程分支。

远程目前只有main、codex/evidence-v1-integration、agent/improve-repository-presentation三个分支。

## 旧工作树/本地分支

下面8个分支的提交都已进入origin/main，工作树的已跟踪/未跟踪状态均干净：

| 分支 | HEAD |
| --- | --- |
| codex/evidence-contracts | bd6b6a4 |
| codex/evidence-frontend | 93590f5 |
| codex/evidence-frontend-race | e1dd0f9 |
| codex/evidence-pipeline-integration | db4fc56 |
| codex/evidence-web-security | fc7a44e |
| codex/paragraph-citations | ea35105 |
| codex/summary-quality | 446eed1 |
| codex/web-paragraph-evidence | 8497e0a |

另有codex/github-research-mode与feature/tests-and-followup也已进入origin/main。它们没有新的待合并提交。旧工作树内可能存在被忽略的.env、运行结果或依赖，干净不表示这些文件不存在；此次不删除。后续盘点保留文件后，可先移除工作树，再安全删除已合并的本地分支，避免-D或强制清理。

## 推荐提交组织

### 主线功能分支

建议新建codex/semantic-gate-lite-v1，基于origin/main；使用新工作树，不在当前脏工作区直接切main、stash或硬重置。原现场保持不变。

提交一：承接现有ffa9800（可cherry-pick），保留其“feat: add deterministic planner plan gate”含义。

提交二：`feat: add offline semantic gate lite v1`，明确文件白名单：

- backend/src/research/semantic_gate.py
- backend/src/research/semantic_gate_eval.py
- backend/tests/test_semantic_gate.py
- backend/evaluation/semantic_gate_lite_dev12.json
- backend/evaluation/semantic_gate_lite_verification12.json
- backend/evaluation/semantic_gate_lite_v1_frozen_manifest.json
- backend/evaluation/semantic_gate_lite_acceptance_2026-10-12.json
- docs/SEMANTIC_GATE_LITE_V1.md
- docs/SEMANTIC_GATE_SHADOW_NEXT.md
- docs/acceptance/SEMANTIC_GATE_LITE_V1_2026-10-12.md

Lite仅依赖已跟踪的research.plan_schema及既有第三方包，不依赖旧的plan_evaluation/多模型评审框架。不得因依赖误判而把旧框架整体带入主线。

README、CHANGELOG、.env.example应在该新工作树整理成Lite专属改动，再与提交二一起提交。当前README/CHANGELOG包含整个研究过程，不能直接完整复制后宣称是轻量范围。

### 历史研究分支

建议另建codex/planner-calibration-archive，保存其余研究工具、对应测试、数据、匿名映射和历史报告，不默认合入Lite主线PR。保存是为防止研究资料丢失，不表示这些旧方向仍是第一版交付要求。

历史工具彼此有依赖：plan_sampling依赖plan_evaluation，plan_feedback_experiment依赖plan_sampling，独立评审/组分校准又依赖前者。归档时应整组保存，不能只提交某个上层脚本或测试。

## 提交前需要收敛的地方

1. **配置模板。** 当前.env.example新增的是双模型实验配置，缺少Lite专属SEMANTIC_GATE_*示例；应给Lite显式配置20B、Groq地址和占位密钥，避免落到历史Feedback默认GLM。同时模板还把主应用默认模型由glm-4.5-air改成glm-4.7-flash；已有历史默认思考模式下2000-token截断记录，建议这项模型默认变更拆出，不与离线Lite混合发布。本地用户.env不要回滚或提交。
2. **文档范围。** 新PR的主入口只突出Lite与验收，历史研究说明放归档。多模型“必须不同”的要求属于独立实验，不能误写成Lite运行时必须双模型。
3. **临时诊断脚本。** backend/tests/_diagnostic_reporter_limitation_repro.py复现报告正文与limitations矛盾，断言历史上失败且文件名不被pytest默认收集。保留本地，单独建修复项；不混入Lite提交，不删除记录掩盖缺陷。
4. **过时仓库说明。** AGENTS.md关于无测试、无README、无CI等事实已过时，建议另一个文档维护提交修正。

## 已核对的证据

- Lite冻结清单5个文件的LF归一化SHA-256均匹配，没有修改冻结代码、提示或数据。
- .env及benchmark_runs目录受忽略规则保护。
- 对66个候选文件做本地现有密钥值匹配与常见token格式扫描，均无命中；扫描未输出密钥内容。这不是全部历史提交的安全保证。
- 历史工程证据为2026-10-10的787 passed、2 skipped及lint/mypy通过，本轮没有重跑。该历史结果覆盖完整工作区，拆分后的新分支仍应由对应CI检查组合完整性，不能直接把旧总测试数当作新分支验收。
- 原始模型调用结果仍在被忽略的benchmark_runs，主线只提交现有脱敏验收摘要及指纹，不强制添加整个运行目录。

## 推送顺序

先在新工作树完成两个清晰提交及范围检查，再推送新功能分支并向main开PR；不直接推main，不强推旧分支。旧历史资料可单独推送归档分支。README展示分支另行处理，避免两批README更新互相覆盖。

本次建议：提交可以推进，优先提交“确定性Gate＋离线Lite”；暂不把所有实验工具合成一个大提交，也不宣称上线生产语义阻断。
