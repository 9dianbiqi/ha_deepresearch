# 规划校验门（Plan Gate）验收记录

日期：2026-10-07
分支：`codex/evidence-v1-integration`

范围：规划层的确定性解析边界、去重与覆盖率校验、复杂度分级任务规模，以及计划任务数超出检索预算时的截断行为。证据层、报告层、前端和部署配置不在本次范围。

## 自动化验收

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| Backend lint | `uv run ruff check src tests` | All checks passed |
| Backend 类型检查 | `uv run mypy src` | Success: no issues found in 63 source files |
| Backend 测试 | `uv run pytest -q` | 696 passed, 2 skipped, 22 subtests passed（93.94s） |
| Frontend API contract | `npm run test:api-contract` | 6 passed, 0 failed |
| Frontend 构建 | `npm run build` | 通过（vue-tsc + vite，24 modules，3.97s） |

本次改动不涉及 `compose.yaml`、Dockerfile 或生产环境变量，因此部署配置作业不受影响。

## 行为验收

- [x] planner 输出为整段 JSON、围栏代码块或夹带散文的花括号结构时都能解析；三者都失败时回落到单个兜底任务
- [x] 单个非法任务（缺字段、非文本、空串、超长）只丢弃该任务并计数，不中断整次运行
- [x] 任务数超过 16、`title` 超过 128 字符、`intent`/`query` 超过 512 字符时按 schema 边界截断
- [x] query 词元 Jaccard 重叠严格大于阈值的任务被丢弃，保留先出现的任务
- [x] 覆盖率低于阈值时生成 ≤8 条可读反馈并发起带反馈重规划；planner 签名不接受 `validation_feedback` 时只做确定性修复
- [x] 重规划次数受 `PLAN_VALIDATION_RETRIES` 约束；重试耗尽后仍返回已保留任务继续执行（失败开放）
- [x] 计划任务数超过 profile `retrieval_budget.max_tasks` 时截断到上限并继续执行，记录 `plan_budget_exceeded`
- [x] `metrics.plan_quality` 记录 verdict、重试次数、去重数、覆盖率、复杂度分级和原因码，且不含 prompt、LLM 原文或网页正文
- [x] `PLAN_VALIDATION=False` 时跳过校验门，planner 输出直接进入执行

## 变更行为

计划任务数超过 profile 检索预算时，行为由「抛错终止运行」改为「截断到上限并继续执行」。覆盖率按截断前的任务集判定，因此截断后实际覆盖可能低于记录值，这是有意的失败开放取舍。

## 未决事项（不在本次范围）

- `backend/tests/_diagnostic_reporter_limitation_repro.py` 复现了报告正文与治理性 limitations 自相矛盾的问题：正文声明某证据缺失，limitations 段却输出「No additional limitations were recorded.」。该文件是未跟踪的临时诊断脚本，文件名不匹配 pytest 收集规则，断言当前失败。本次仅修正其 import 顺序以满足 lint，缺陷本身未修。
- `AGENTS.md` 与仓库现状不一致：「No tests exist anywhere in the repo」「No README」「No CI/CD」均已过时（现有 696 个后端测试、`README.md` 与 `.github/workflows/ci.yml`）。
- `frontend/tests/evidence-contract.test.mjs` 未被任何 npm script 或 CI 作业执行。
