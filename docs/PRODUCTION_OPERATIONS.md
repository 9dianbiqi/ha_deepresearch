# v1.1 单实例生产运维

## 数据布局

`DATA_DIR` 是唯一持久化根目录。生产 Compose 中它映射到 `/data`，目录结构为：

```text
DATA_DIR/
├── runs/<run_id>.json
├── artifacts/<run_id>/<artifact_id>
├── research-history.db
└── user-memory.db
```

Run JSON 是 canonical 数据；`research-history.db` 是可从 Run JSON 重建的历史索引。`user-memory.db` 保存用户明确确认的记忆，不能从 Run JSON 推导，必须和 Artifact 文件、Run JSON 一起备份。

## 备份

备份前先停止写入，保证 Run JSON、Artifact 和 SQLite 文件处于一致快照：

```powershell
docker compose --env-file .env.production stop backend
docker run --rm `
  -v helloagents-deepresearch_helloagents_data:/source:ro `
  -v ${PWD}/backups:/backup `
  alpine tar -czf /backup/helloagents-data-$(Get-Date -Format yyyyMMddHHmmss).tar.gz -C /source .
docker compose --env-file .env.production start backend
```

如果使用本地目录作为 `DATA_DIR`，在停止后复制整个目录，不要只复制 `runs/` 或 `artifacts/`。

## 恢复

1. 停止 backend，保留现有 Volume 作为回滚副本。
2. 将备份中的整个数据根恢复到同一个 Volume 或 `DATA_DIR`。
3. 启动 backend，确认 `GET /healthz` 返回 `{"status":"ok"}`。
4. 确认 `GET /readyz` 返回 `{"status":"ready"}`，再用已知 `run_id` 下载一个 Artifact。

若历史 SQLite 索引损坏，服务会以 canonical Run JSON 重建；恢复时仍建议恢复整个数据根，以保留用户记忆和索引中的时间信息。

## 保留与清理

先预览，再执行删除。命令只接受包含专用 `runs/` 和 `artifacts/` 子目录的 `DATA_DIR`，拒绝工作区、Git 根目录、用户 Home 和文件系统根目录：

```powershell
cd backend
python -m maintenance cleanup --data-dir ..\data --retention-days 30
python -m maintenance cleanup --data-dir ..\data --retention-days 30 --apply
```

清理只删除超过保留期的终态 Run 及其同名 Artifact 目录，并在删除后重建 `research-history.db`。运行中的 Run、损坏的 Run JSON、符号链接和不在受控目录内的路径都会跳过或使命令失败。

## HTTP 保护

- `APP_API_KEY` 是生产必填配置；除 `/healthz` 和 `/readyz` 外的 HTTP 路由都要求 `Authorization: Bearer <APP_API_KEY>`。未配置时受保护路由 fail closed，仍返回 `401 unauthorized`。
- Compose 中前端使用 `/api` 反向代理，nginx 只透传浏览器输入的 `Authorization` 请求头；密钥不会编译进浏览器 JavaScript。页面首次使用时输入 API Key，直接开发模式仍通过 `VITE_API_BASE_URL` 指向后端。
- `MAX_CONCURRENT_RUNS` 控制单进程活动 Run 数，超限返回 `429 capacity_exceeded`。
- 主题长度上限为 4000 字符，Harness `metadata` 上限为 16 KiB。
- SSE、Run JSON 和日志只保留受控投影，不写入 Token、Prompt 原文或完整模型响应。
