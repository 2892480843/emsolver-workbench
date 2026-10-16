# 事件日志边界验证记录

## 范围与环境

- 按 `docs/architecture-optimization-contract.md` 任务 B 实现。
- 文件范围：`emsolver/events.py`、`tests/test_event_journal.py` 及本报告。
- 本地环境：Darwin/macOS，Python 3.12.13；使用 `.venv-emsolver/bin/python`。
- 测试仅使用临时目录中的真实 SQLite Store，不操作运行中的数据，不执行数值基准。
- 服务调用处的最终补读、事件下载及 API 集成由主开发负责；本模块验证不替代该集成验证。

## 失败基线与实现后验证

| 阶段 | 命令 | 观察结果 |
|---|---|---|
| 先写测试，确认失败 | `.venv-emsolver/bin/python -m pytest -q tests/test_event_journal.py` | 退出码 2，收集阶段因 `ModuleNotFoundError: No module named 'emsolver.events'` 失败；当时尚未添加实现 |
| 添加事件实现，等待存储依赖 | 同上 | 23 passed、6 failed；失败仅为旧 Store 缺少事件查询关键字参数及 `event_max_seq` |
| 不依赖新存储接口的检查 | `.venv-emsolver/bin/python -m pytest -q tests/test_event_journal.py -k 'worker_reader or append or rejects'` | 23 passed、7 deselected，0.16 秒 |
| 语法编译 | `.venv-emsolver/bin/python -m compileall -q emsolver/events.py tests/test_event_journal.py` | 退出码 0 |
| 新 Store 接口到位后的事件与周边回归 | `.venv-emsolver/bin/python -m pytest -q tests/test_event_journal.py tests/test_reporting.py` | **35 passed，0.42 秒**，退出码 0；包含全部 30 个事件用例及 5 个报告用例 |

## 行为验收设计

| 行为 | 可重复验证 |
|---|---|
| 字节游标与半行 | 在中文 UTF-8 字符中间断开写入，第一次读取不消费半行；补全换行后恰好读取一次 |
| 无效输入与字段白名单 | 跳过非法 JSON、非对象、非有限字面量及非法 UTF-8 完整行；只保留与 worker 相同的数值观察字段，去除 `message`、路径和伪造序号 |
| 退出后的最终补读 | 首次读取后写入 `FINAL_WORKER_EVENT`，再次 drain 后按序进入真实 Store；再次读取为空 |
| 单一事实来源与回滚 | 外层事务内 append 后抛错，SQLite 回滚为零事件，且不创建 `runs` 或幽灵 JSONL 文件；下一次成功追加从序号 1 开始 |
| 非有限值拒绝 | append NaN 抛 `ValueError`，数据库及文件均无事件残留 |
| 分页和尾部窗口 | 连续分页无重复；空页保留 after；尾部最近记录按序号正序返回；非法布尔值、负数、非整数及超限参数被拒绝 |
| 最大分页边界 | 1001 条记录时，页面只读取 1001 条并返回 1000 条及 `has_more=True`；尾部只读取最近 1000 条 |
| 有界、固定上界导出 | 1203 条初始记录分 500、500、203 三批读取；导出开始后追加第 1204 条，导出仍只包含 1..1203；每批查询带同一 until 上界 |
| 导出失败保留旧文件 | 第二批读取注入 I/O 异常，旧完整导出保持原文，唯一临时文件被清理，数据库仍保留 501 条事件 |
| 并发导出原子发布 | 两个线程在读取处同步，同时导出同一作业；都成功，发布文件包含完整 40 条序号，无共享临时文件冲突和临时文件残留 |
| 空文件和路径边界 | 无事件时原子发布空 JSONL；拒绝 `../`、绝对路径、目录分隔符和空身份标识 |

## 实现边界

- SQLite 是事件事实来源；`append()` 不进行文件写入，并遵循已有 Store 事务回滚。
- 文件导出冻结开始时的最大事件序号，每次最多读取 500 条，采用唯一临时文件与原子替换发布。
- 导出文件表示该次物化操作的上界，不阻塞后续事件追加；并发操作仍由最后完成的原子替换发布文件。
- 本次仅证明本地事件模块行为；未声称验证 Linux、数值性能或服务生命周期完整集成。
