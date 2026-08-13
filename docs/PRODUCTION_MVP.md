# 生产级单实例 MVP 部署与备份恢复

本版本的生产边界只包含 HTTP 鉴权、单进程研究配额、数据保留和 Docker 运行方式。它不是用户级多租户、计费或复杂限流系统。

## 部署

1. 复制配置模板并生成一段随机的 `APP_API_KEY`：

   ```powershell
   Copy-Item .env.production.example .env.production
   # 编辑 .env.production，至少替换 APP_API_KEY、LLM_MODEL_ID、LLM_API_KEY、LLM_BASE_URL
   ```

   `.env.production` 已被 Git 忽略。不要把真实密钥写入仓库、前端源码或镜像构建参数。

2. 构建并启动单实例 Compose：

   ```powershell
   docker compose --env-file .env.production up -d --build
   docker compose --env-file .env.production ps
   ```

   只有前端端口（默认 `5174`）发布到宿主机；backend 只通过 Compose 内部网络被 nginx 访问。frontend 的 `/api/*` 代理会透传浏览器输入的 `Authorization` 请求头。

3. 验证服务：

   ```powershell
   Invoke-RestMethod http://localhost:5174/api/healthz
   Invoke-RestMethod http://localhost:5174/api/readyz
   ```

   页面首次使用时输入与 backend 相同的 `APP_API_KEY`。浏览器只把它放在当前标签页的 `sessionStorage`，并在受保护请求中发送 `Authorization: Bearer <key>`。

## 鉴权、配额和保留

- `/healthz`、`/readyz` 和 CORS `OPTIONS` 不需要密钥；其他 HTTP API 缺少或使用错误 Bearer Key 时返回 `401`，错误码为 `unauthorized`。
- `MAX_CONCURRENT_RUNS` 默认 `1`。普通研究、流式研究、恢复和 Harness 入口共用同一个非阻塞槽位；槽位占满时立即返回 `429 capacity_exceeded`，不排队。
- 主题最多 4000 个字符，Harness `metadata` 序列化后最多 16 KiB。
- `RETENTION_DAYS` 默认 `30`。清理只处理超过保留期的终态 Run 及其同名 Artifact 目录，不删除运行中的 Run 或无法验证的记录。

预览清理目标：

```powershell
docker compose --env-file .env.production exec backend python -m maintenance cleanup --data-dir /data --retention-days 30
```

确认无误后执行删除：

```powershell
docker compose --env-file .env.production exec backend python -m maintenance cleanup --data-dir /data --retention-days 30 --apply
```

## 备份

备份前停止 backend，保证 Run JSON、Artifact 文件和 SQLite 索引处于一致快照：

```powershell
docker compose --env-file .env.production stop backend
New-Item -ItemType Directory -Force backups | Out-Null
docker run --rm `
  -v helloagents-deepresearch_helloagents_data:/source:ro `
  -v ${PWD}/backups:/backup `
  alpine tar -czf /backup/helloagents-data-$(Get-Date -Format yyyyMMddHHmmss).tar.gz -C /source .
docker compose --env-file .env.production start backend
```

生产环境应把生成的压缩包复制到独立存储，并定期验证可以解压。备份包含：

```text
/data/
├── runs/
├── artifacts/
├── research-history.db
└── user-memory.db
```

## 恢复

1. 停止 backend 和 frontend，保留原 volume 作为回滚副本。
2. 将备份解压到同一个 `helloagents_data` volume（或挂载到 `DATA_DIR` 指向的目录）。
3. 启动 Compose，并确认 `/api/healthz`、`/api/readyz` 均正常。
4. 用已知 `run_id` 请求受保护的运行记录或 Artifact，确认数据仍可读。

示例恢复命令：

```powershell
docker compose --env-file .env.production down
docker run --rm `
  -v helloagents-deepresearch_helloagents_data:/target `
  -v ${PWD}/backups:/backup `
  alpine sh -c "rm -rf /target/* /target/.[!.]* /target/..?* 2>/dev/null || true; tar -xzf /backup/helloagents-data-YYYYMMDDHHMMSS.tar.gz -C /target"
docker compose --env-file .env.production up -d
```

如历史 SQLite 索引损坏，Run JSON 仍是 canonical 数据源；先恢复完整数据根目录，再用维护命令或服务启动流程重建索引。

## 已知限制

- 配额是单进程内存信号量；不要把多个 backend 副本当作共享全局限流器。
- 没有用户级套餐、计费、Token 余额、复杂限流或跨实例协调。
- `APP_API_KEY` 是共享单实例密钥；轮换密钥需要更新 `.env.production` 并重启 backend，浏览器标签页需重新输入。
