# P0 真实链路验收记录

- 时间：2026-08-11（Asia/Shanghai）
- 范围：FastAPI → HarnessRunner → ResearchApplicationService → SSE → FileRunRepository；前端使用本地 Vite 页面复核。
- 说明：生命周期场景使用确定性 coordinator 隔离外部模型波动；另用当前运行中的生产配置完成一次真实研究请求。

## 场景结果

| 场景 | 结果 | 关键证据 |
| --- | --- | --- |
| 正常研究 | PASS | 生产 Run `55452c3df2a244e3913c124bf05e09ca`：SSE `done`、`resumable=true`、持久化 `completed`、报告 303 字符；首版过短后自动重试 1 次并通过。 |
| 截断报告 | PASS | 受控 Run `d131f92c96f544a4ad3b0ce3824c9459`：终端 SSE `error/report_incomplete`，持久化 `report_incomplete`，`retry_count=1`，`failure_reason=report_incomplete`。 |
| A 成功、B 失败后继续 | PASS | A=`4b6ae7f6acfe45cb8348b339586280b8`；B=`5489a3040bf34205984e94641c57097a` 失败且 `last_resumable_parent=A`；C=`263449f654804fe983bfd7f110e3523a` 的 `parent_run_id` 和载入上下文均仍指向 A。 |
| 后端重启 | PASS | 独立端口启动新后端进程（PID 33440）读取同一 Run 目录；A 重新加载为 `completed/resumable=true`，此前的重建链路也确认重启后的续问仍载入 A 上下文。 |
| 前端刷新 | PASS | 真实页面选择历史 Run `77645fcab49f4b608a69326cc02d2971` 后刷新，报告、任务和“继续追问”入口自动恢复；`resumableRunId` 可用。 |
| SSE 中途断开 | PASS | 生产 Run `ca53e287e78f4aa3b5fdac58916beb33` 读取首帧后关闭连接；随后 `/runs/{id}` 可读取，状态为 `cancelled`、`resumable=false`，未出现 `completed`。 |
| 父 Run 错误区分 | PASS | 受控链路分别返回 `parent_not_found` 与 `parent_not_resumable`；失败追问响应保留 `last_resumable_parent`。 |

## 检查结果

- 后端：`392 passed, 2 skipped, 45 warnings, 22 subtests passed`。
- Ruff：`All checks passed!`
- Mypy：`Success: no issues found in 37 source files`。
- 前端：`vue-tsc --noEmit && vite build` 成功，15 modules transformed。
- `git diff --check`：通过。

## 验收期间修正

- 发现前端刷新只加载历史列表、没有恢复最近选中 Run；已在挂载时读取 `helloagents:last-run-id` 并调用历史恢复，随后浏览器刷新复验通过。
- 发现 Legacy SSE 投影器未把 `report_incomplete` 纳入终端错误码白名单，曾降级为 `run_failed`；已修复并加入回归断言。
