# 生产级单实例 MVP 验收清单

版本范围：鉴权、配额、数据保留和 Docker。论文研究、Agent 编排和多用户系统不在本次收口范围。

## 自动化验收

- [ ] Backend lint：`uv run ruff check src/ tests/`
- [ ] Backend 类型检查：`uv run mypy src/`
- [ ] Backend 测试：`uv run pytest -q`
- [ ] Frontend API contract：`npm run test:api-contract`
- [ ] Frontend 构建：`npm run build`
- [ ] Compose 解析：`docker compose --env-file .env.production.example config`
- [ ] 两个镜像构建：`docker compose --env-file .env.production.example build backend frontend`

## HTTP 鉴权与输入边界

- [ ] 无密钥访问受保护接口返回 `401`，`detail.code=unauthorized`
- [ ] 错误密钥返回 `401`，且响应、日志和运行快照不包含密钥
- [ ] 正确密钥访问受保护接口成功
- [ ] `/healthz` 和 `/readyz` 无密钥可访问；CORS `OPTIONS` 不被鉴权拦截
- [ ] 主题超过 4000 字符被拒绝
- [ ] Harness metadata 超过 16 KiB 被拒绝
- [ ] API Key 只在前端 `sessionStorage` 中保存，构建产物不包含 `APP_API_KEY`

## 配额与持久化

- [ ] 并发占满后第二个研究入口立即返回 `429 capacity_exceeded`
- [ ] 同步成功、同步异常、流式正常结束、流式异常和客户端断开后均释放槽位
- [ ] 普通研究、流式研究、恢复和 Harness 入口共用同一个 `MAX_CONCURRENT_RUNS` 槽位
- [ ] `RETENTION_DAYS` 默认 30 天
- [ ] 手动清理命令先预览、再删除过期终态 Run 和 Artifact
- [ ] `readyz` 在持久化目录不可写或依赖不可用时返回 `503`
- [ ] 容器重启后 Run JSON、Artifact、历史和用户记忆仍存在

## Docker 运行

- [ ] Backend 使用 `backend/uv.lock` 的 frozen 安装并以非 root 用户运行
- [ ] Backend 数据目录挂载到 named volume
- [ ] Frontend 使用 Node 构建、Nginx 提供静态文件并代理 `/api`
- [ ] Compose 只发布 frontend 端口，backend 没有宿主机 `ports`
- [ ] Backend/frontend 均有健康检查、启动依赖和自动重启策略
- [ ] 页面可访问，`/api/healthz` 和 `/api/readyz` 健康检查通过

验收完成后，将版本标记为“生产级单实例 MVP”，停止在本范围内继续增加功能。
