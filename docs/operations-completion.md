# 产物保留与观测运维

本文记录 F41（观测开销与有界降级）及 F64（存储与回收）的新增模块。两者均不改变数值状态或原问题验收标准。

## 生命周期接口

`LifecycleManager(gateway)` 依赖应用网关的 `root`、`store`、`_mutex`、`_project`、`_authorize` 及 `governance.add_bytes`；不依赖 API 或全局服务实例。

```python
manager = LifecycleManager(gateway)
plan = manager.plan(now=None)
preview = manager.run(dry_run=True, now=None)
execution = manager.run(dry_run=False, now=None)
```

`now` 可省略，也可传入带时区的 `datetime` 或 ISO 日期字符串。时区不明确的输入被拒绝。`plan` 仅规划，返回：

```json
{
  "items": [{"job_id": "j_example", "project_id": "p_example", "bytes": 100,
             "files": ["runs/j_example/artifacts/eigenvectors.npy"],
             "reason": "explicitly unretained terminal output older than 30 days"}],
  "total_bytes": 100,
  "policy": "Only expired, explicitly unretained result files; metadata, manifests, configuration and canonical events remain."
}
```

`run` 在该结果上增加 `dry_run`、`expired`（本次过期任务 ID）、`removed_bytes`、`disk_reclaimed_bytes` 和 `cleanup_pending`。

- `removed_bytes` 表示本次从运行产物目录移出的结果文件字节数，项目完成输出计数只扣除这部分，最低为零。
- `disk_reclaimed_bytes` 只在隔离目录物理删除成功时报告本批字节数；删除失败时为零，不声称已经腾出磁盘空间。
- `cleanup_pending` 为尚待恢复或清理的隔离批次 ID。项目运行输出与工作区临时隔离占用分别描述，不能把前者的减少当成实际磁盘可用量增加。

筛选要求同时满足：`retained is False`、终态、有效 `finished_at`，且超过项目 `retention_days`。缺少 `retained` 的旧任务默认受保护。保留运行及活动运行的父任务链也受保护；不删除输入矩阵、配置、事件、元数据与清单。

当前可删除范围为 `runs/<job_id>/artifacts` 中明确生成的结果文件：`eigenvalues.npy`、`eigenvectors.npy`、`residuals.npy`、`eigenvalues.csv`、`eigenvectors.npz`、`eigenvalues.scipy.npz`、`eigenvectors.scipy.npz` 和 `subspace.checkpoint.npz`。保留所有 JSON、YAML、JSONL、日志、行映射 `permutation.npy`、输入副本及未知文件；不会仅凭扩展名就认定为可回收结果。包含符号链接的输出树整项跳过。执行要求对应项目管理员权限，查看计划仅要求项目读权限。

## 事务与故障恢复

执行始终按应用 `_mutex` 再数据库事务的顺序获取锁。文件先重命名到私有 `.retention-trash/<batch>`，其中保存 `0600` 的有界恢复记录，目录为 `0700`。随后写入任务 `artifacts_state='EXPIRED'`、`artifacts_expired_at` 和批次 ID，更新项目输出计数并记录审计。事务提交后才物理删除隔离文件。

任意重命名、数据库、计数更新或审计失败会回滚事务并恢复已移出的文件。若恢复本身失败则返回 `RETENTION_ROLLBACK_PENDING` 并保留恢复记录。进程中断后，下次非预览执行会恢复未提交的隔离文件，或清理已经提交的隔离批次；恢复同样检查项目管理员权限。清理不会改写 `status`、`result.validation`、输入、配置或规范事件。

产物过期后，清单仍保留原始哈希及执行历史；其存在不能被解读为二进制结果仍可下载。下载与报告应显示 `artifacts_state`。保留计划不会以删除原清单来掩盖缺失产物。

## 次要观测缓冲

`SecondaryTelemetry(exporter=None, capacity=256, start_worker=True)` 提供：

- `emit(sample) -> bool`：只提取有限的数值字段及受限事件标识，立即尝试入队；不调用导出器，不等待队列容量。
- `drain(limit=None)`：取出本地缓冲的样本，供可信离线消费者使用。
- `counters()`：返回 `accepted`、`exported`、`dropped`、`exporter_failures`、`ignored_critical`、`in_flight`、`queued`、`capacity`、`closed`。
- `close(timeout=.1)`：停止接收并至多等待指定时间；上限五秒，不因导出器卡住无限等待。

有导出器时，仅单个守护线程调用它。导出器失败计数后继续；导出器永久阻塞时，数值路径仍只做非阻塞入队，缓冲满则丢弃次要采样并计数。线程和队列数量固定，不能通过失败重试无限创建线程。默认无导出器时样本保留在本地缓冲，规范事件仍由原有数据库与文件机制保存。

样本白名单排除输入路径、项目名称、身份、矩阵值、特征向量及自由格式错误文本。终态与独立验证事件不进入易丢失缓冲，计为 `ignored_critical`；调用方必须先使用原有规范事件接口记录这些事件。该缓冲不替代审计、状态或验收证据。

## OpenMetrics 指标

`render_metrics(store, root, telemetry=None) -> str` 输出 OpenMetrics（供指标采集器读取的文本格式）。任务状态计数直接读取 SQLite 的已索引状态列，不解码任务正文。指标标签只有预定义状态，未知状态统一为 `UNKNOWN`；不输出任务 ID、项目、用户、输入名或路径。

指标包括 `emsolver_jobs{status=...}`、`emsolver_free_disk_bytes`、`emsolver_disk_available` 和次要缓冲计数。磁盘查询失败时只给出可用性为零，不用零字节冒充实际测量。输出以 `# EOF` 结束。HTTP 路由由服务层控制访问权限。

## 开关观测的实测脚本

```sh
.venv-emsolver/bin/python scripts/benchmark_observability.py --n 1000 --repeats 7 --output /tmp/observability.json
```

也可传入 `--matrix` 与 `--config`。脚本在同一输入、种子及数值配置下，逐对切换 `observability.enabled`，交替采用“关闭—开启”和“开启—关闭”的次序。每次都调用真实 `run_solver`，使用新的输出目录，并在计时之外执行原问题独立复验；不生成模拟耗时。

输出保存每次实际秒数、状态、错误码、配置哈希、事件数、缓冲计数、数值与目标认证、最大残差，以及每对的数值等价性。统计保留全部运行，报告中位数、开启／关闭耗时比、差异百分比和配对比值；失败不从分母删除。数值等价按明确容差判断，不要求不同环境逐位相同。

该计时包含数值求解、JSONL 回调、有界次要样本入队与数值导出；不包含独立复验、HTTP、界面或外部观测服务。比值仅描述本次本地实验，单次或小样本波动不能证明普遍开销或性能收益。缺少真实开关配置契约时脚本拒绝运行，不以其他语义字段代替。

## 验证范围

```sh
.venv-emsolver/bin/python -m pytest tests/test_retention.py tests/test_telemetry.py -q
```

本地 macOS arm64 测试覆盖保留默认值、期限、活动任务、父链、符号链接、只规划、权限、输出计数下界、部分重命名失败、数据库／审计／计数故障回滚、隔离中断恢复、物理删除失败，以及导出器阻塞／失败和真实配对数值求解。未将这些本地证据外推为全新 Linux 部署、外部采集器或任意文件系统均已验证。

2026 年 10 月 16 日本机实跑 `n=1000`、七对共 14 次；全部 `SUCCEEDED/PASSED/CERTIFIED`，七对数值按容差等价，输入哈希未变。开启与关闭中位数分别约 35.37 和 33.17 毫秒，比值约 1.066；配对比值中位数约 1.039。这组短耗时本地实验仅供复跑比较，不据此宣称普遍开销。原始 JSON 由上述命令输出，完整包含各次耗时与状态。
