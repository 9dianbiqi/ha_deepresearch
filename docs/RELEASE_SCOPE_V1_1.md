# v1.1 单实例生产 MVP 发布边界

## 本阶段交付

- 通用研究生命周期、证据 schema-v2、Provider 路由、质量门禁和产物存储作为冻结内核。
- 单仓库 GitHub 研究由 `ResearchKernel` 负责准备、Provider 检索、证据归一化和最终封存。
- 既有 API、SSE 和 `github_intelligence` 字段继续作为兼容投影，不作为新的内部状态来源。
- Web 研究保持现有 Planner/Worker 行为，作为兼容链路运行。

## v1.1 前明确不做

在 v1.1 单实例生产 MVP 发布前，不开发任何论文功能，包括：

- OpenAlex、Crossref 或其他论文平台的真实 Provider；
- PDF 下载、全文解析、页码证据、BibTeX 和引用雪球；
- 论文检索 UI、论文专用报告模板或论文网络请求；
- 将论文请求静默降级为 Web 研究。

`paper.abstract.v1` 仅作为通用内核的契约占位和隔离测试 seam；默认生产 Provider 注册表不会为论文模式提供实现。论文能力必须在 v1.1 发布后单独立项、评审并发布。
