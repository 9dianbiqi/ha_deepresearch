# 证据研究的保存与恢复

证据 Web 和 GitHub kernel 在 `planning_completed`、`research_tasks_progress`
保存任务恢复数据。普通兼容 Web 的入口、证据开关默认值及来源路由保持不变。

保存顺序是：协调器合并任务结果、接受证据绑定、复制运行状态、保存页面
artifact，最后原子提交 checkpoint。页面正文不写入 run JSON 或 SSE。
页面 artifact 按内容散列命名；读取时检查大小、散列和来源身份。
失败抓取与 metadata 降级也会保存，恢复后不自动重抓替换原始证据。

任务阶段恢复使用 checkpoint 的任务列表、质量结果、证据绑定、页面缓存和
预算，重新创建 provider context、锁与取消控制。已经完成的任务不重复规划、
摘要或 Judge；未完成任务继续使用同一套证据契约。已知启动过的检索 attempt
计入三次上限，创建/读取任务笔记不占用检索 attempt。

恢复绑定完整的 profile 策略指纹。缺少恢复数据、profile 改变或 artifact
损坏时明确拒绝任务恢复，不降级到旧搜索路径。没有 ArtifactStore 时，研究
仍可执行，但任务阶段 checkpoint 会标记 `evidence_recovery_unavailable`。
旧兼容 checkpoint 与证据完成后的 report-only 恢复继续使用原有路径。

保证范围是**最后已提交 checkpoint**：预算和已知 attempt 不回补。
进程硬崩之前、保存点之后尚未提交的网络开销不承诺 exactly-once；本次未
引入逐请求 WAL。已知不确定副作用仍由现有操作审计阻止恢复。

针对性测试位于 `backend/tests/test_evidence_recovery_runtime.py` 和
`backend/tests/test_evidence_recovery_integration.py`，覆盖真实磁盘存储与新实例
恢复、共享页面、失败抓取、版本/原文身份、损坏拒绝、预算与重试、写入失败、
报告阶段恢复以及终态输出晚于 checkpoint 的情况。
