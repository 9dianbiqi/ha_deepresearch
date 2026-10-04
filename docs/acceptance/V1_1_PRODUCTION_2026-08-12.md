# v1.1.0 单实例生产验收记录

日期：2026-08-12

范围：Artifact 下载、单实例保护、持久化/清理、部署配置和 GitHub 真实仓库读取。论文检索不在范围内。

## 自动验收结果

| 检查项 | 结果 | 证据 |
|---|---|---|
| Artifact 下载、Run 所属校验、正确 MIME/文件名 | PASS | `backend/tests/test_harness_api.py`：`test_artifact_download_survives_new_app_instance_and_checks_ownership` |
| 新进程/新 App 实例继续下载 Artifact | PASS | 同上；第二个 `TestClient(create_app(...))` 返回相同字节 |
| `/healthz` 公开、`/readyz` 检查持久化目录 | PASS | `test_readiness_probe_checks_durable_and_artifact_directories` |
| Bearer 鉴权、错误不回显密钥 | PASS | `test_configured_bearer_key_protects_routes_but_not_probes` |
| 并发上限和 `429 capacity_exceeded` | PASS | `test_run_capacity_returns_429_without_waiting` |
| 主题/Metadata 限制和稳定错误 | PASS | `test_request_limits_return_stable_errors_without_echoing_payloads` |
| 取消、恢复、真实进程重启 | PASS | `tests/test_fault_injection.py` 的 kill/restart 与 SSE disconnect 验收 |
| 证据不足进入 `report_incomplete` | PASS | `tests/test_research_application.py::test_two_incomplete_reports_persist_report_incomplete_terminal` |
| 后端全量测试 | PASS | `480 passed, 2 skipped` |
| Ruff / mypy | PASS | `All checks passed`；`Success: no issues found in 51 source files` |
| 前端契约测试 / 构建 | PASS | `5 passed`；`npm run build` 成功 |
| Compose 生产配置展开 | PASS | `docker compose --env-file .env.production.example config` |
| Docker 镜像构建 | CI | GitHub Actions `deployment-config` job 在推送后构建 backend/frontend 镜像；本机 Docker daemon 当前未启动 |

## GitHub 真实仓库验收

使用 GitHub REST API 只读访问，未使用 `GITHUB_TOKEN`。同一仓库内的源码证据均检查为单一完整 Commit SHA，并由 schema-v2 生成 `#Lx-Ly` 定位 URL。

| 场景 | 仓库 | 结果摘要 |
|---|---|---|
| 小型 Python | `pallets/markupsafe` | `complete`；SHA `b2e4d9c7687be25695fffbe93a37622302b24fb1`；16 个源码行引用，全部固定到该 SHA |
| 大型 Python | `django/django` | `complete`；SHA `082b3df4067c3899dd4d57e8c2eca5baea9d07bb`；8 个源码行引用，全部固定到该 SHA |
| Vue/TypeScript | `vuejs/core` | `complete`；SHA `a2b40db9a83b36ed9da3a16403cf8f040262d73f`；12 个源码行引用，全部固定到该 SHA |
| 无 License/Release | `octocat/Hello-World` | 首次读取 `complete`，`license=null`、Release 数为 0，未伪造源码引用；重复探测在未认证配额耗尽后正确进入 `github_rate_limited` |
| 404 / 限流降级 | `octocat/helloagents-v1-1-does-not-exist-20260812` | 首次批次返回 `github_not_found`、`partial`、无 Commit SHA；后续未认证请求返回 `github_rate_limited`，两条降级路径均保持 schema-v2 并禁止报告通过 |

真实读取中观察到的行引用示例：

```text
https://github.com/pallets/markupsafe/blob/b2e4d9c7687be25695fffbe93a37622302b24fb1/docs/Makefile#L1-L19
```

限流降级的确定性结果为 `coverage_score=0.4`、`allow_report=false`，因此证据不足不会被包装成完成报告。

## 发布边界

- 后端和前端版本均为 `1.1.0`。
- 生产 Compose 使用 named volume `helloagents_data`，前端通过 nginx `/api` 反向代理透传浏览器输入的 Bearer 密钥，密钥不编译进浏览器 JavaScript。
- 备份、恢复、保留天数和安全清理步骤见 [生产运维说明](../PRODUCTION_OPERATIONS.md)。
- 论文检索、论文全文/PDF、BibTeX 和 citation snowballing 仍明确不属于 v1.1.0。
