# Web 段落证据与摘要质量门禁

`web.evidence.v1` 是显式启用的生产预览路径。普通 Web 请求仍使用原有轻量流程，不会因为升级而自动进入 Evidence 流程。

## 启用方式

1. 在部署环境设置 `ENABLE_EVIDENCE_WEB=true`。
2. 请求中同时选择 `research_mode=web` 和 `research_profile=web.evidence.v1`；只提供其中一个显式 Web 选择也会解析到该 Profile。
3. 首次上线保持默认阈值：语义 `0.72`、事实 `0.75`、引用 `0.85`、综合 `0.78`。

若 `ENABLE_EVIDENCE_WEB=false`，显式 `web.evidence.v1` 请求会兼容回退到 `web.default.v1`，输出不会继续标记为证据模式；运行指标会记录 `compatibility_fallback/feature_disabled`。

## 执行边界

- 搜索结果先经 Web capture 转换为可定位段落 Evidence，再封存 Evidence bundle。
- 网页 snapshot、`structured_summary.json` 与 `quality_assessment.json` 的正文写入 ArtifactStore；Run 和 SSE 只携带描述符或有界结构化字段。
- 摘要先生成 `StructuredSummaryDocument`，再经过语义、事实和引用质量门禁。生产 verifier 使用现有受治理 LLM 边界、确定性温度和严格 JSON；不可用或输出不合法时结果为 `unverified`，不会伪造高分。
- 首次失败只允许重写一次。默认 Web/GitHub 第二次失败会删除不合格事实段并追加 limitation；只有调用方显式选择 `permission_mode=strict` 才会阻止报告。
- 每个事实/分析段落中的每个 claim 都必须有绑定引用；最终引用还必须与 verifier 的正向支持 Evidence 相交，冲突 Evidence 不能作为普通支持。

当前版本没有在摘要质量失败后重新发起 gap query。生命周期不能安全保证补检索只执行一次时，系统选择确定性重写与删段降级，并保留稳定质量告警。

## 输出与事件

Run/API 输出新增：

- `structured_summary`
- `quality_assessment`

Artifact manifest 中对应类型为 `structured_summary`、`quality_assessment`，网页快照类型为 `web_page_snapshot`。

新增 SSE `summary_quality_update` 仅包含综合分、是否通过、段落/claim/阻断计数和 blocker code；不包含网页正文、Evidence excerpt、claim 文本或 verifier support span。`artifact_ready` 仍只包含安全描述符。

## 恢复与回滚

质量完成后检查点为 `summary_quality_completed`。恢复会校验 task ID、文档 hash、Evidence hash、固定阈值键和 verdict/score 自洽性；校验失败会关闭式阻断。严格模式已经失败的质量结果恢复后仍会再次阻断，不会绕过门禁或重复 capture/评分。

回滚只需设置 `ENABLE_EVIDENCE_WEB=false` 并重启服务。普通 Web 旧报告、GitHub 旧字段和旧 SSE 保持兼容。
