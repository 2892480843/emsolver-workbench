# 独立工作进程身份核验与接管

`emsolver.recovery.verified_worker(gateway, job)` 只进行读取与身份核验，成功返回 `RecoveredWorker`，不能确认时返回 `None`；该函数从不发送终止信号。`gateway` 需要 `.root` 和 `.store.get(kind, id)`，不依赖 HTTP 或当前用户请求上下文。

核验要求：

- 作业处于 RUNNING、VERIFYING 或 CANCELLING，作业 id 合法，PID 为正整数，进程创建时间与已登记值完全相同。
- 进程归当前系统用户所有；解释器与当前解释器为同一文件，命令严格为 `python -m emsolver.worker <受控绝对spec路径>`，没有额外参数。`PYTHONPATH` 必须指向当前 EMSolver 包根目录，与服务启动工作进程时的设置相同。
- `runs/<job_id>/worker.spec.json` 及路径各层无符号链接、不允许其他用户写入；文件为本用户普通文件，JSON 不超过 1MiB，拒绝重复字段、非有限值、未知字段与歧义内容。
- spec 配置与冻结作业配置采用严格规范 JSON 比较；不会为旧配置补默认值再篡改其含义。输入路径必须等于登记的 matrices 资产路径，资产不允许任何写权限，真实 SHA256 同时匹配资产与作业快照。
- 输出路径精确为 `runs/<job_id>/artifacts`，各层无符号链接；若工作进程尚未创建该末级目录，可接受其暂不存在。
- 若存在热启动，源快照路径必须来自冻结的 resumption 元数据中的受控源作业，且真实哈希与冻结快照相同；不从外部传入任意路径。

适配器提供 `.pid`、`.args`、`.poll()`、`.wait(timeout)`、`.kill()`、`.terminate_tree(grace_seconds=.5)`，供服务重新接入已有资源监控循环。它不能取得无亲缘进程的可靠操作系统退出码：进程消失或 PID 被复用后，`poll/wait` 返回 `UNKNOWN_RETURN_CODE=255`，并标记 `exit_status_known=False`。该值表示进程已结束或不再是原身份，**不表示求解成功**；业务终态仍需检查工作结果、独立原问题验证和产物清单。

发信号前再次核对 PID 创建时间、命令、解释器、用户、模块路径与 spec/输入文件指纹。目录或 spec 变化、输入变为可写、解释器被替换或 PID 复用均拒绝发信号。终止进程树时只采集已核验父进程的真实后代，并对每个后代固定 PID 与创建时间；发信号前再次核验，不能因为一个旧 PID 就终止后来复用该号码的进程。

服务应先检测适配器的 `terminate_tree` 并调用它，不能重新用裸 PID 绕过其身份保护。接管时仍需恢复事件读取偏移、继承原始运行时间预算，并披露服务停机窗口中未采样的峰值；这些业务整合由服务负责。本模块不修改数据库、不决定任务终态，也不将子空间热启动称作精确断点续算。

验证命令：

```bash
.venv-emsolver/bin/python -m pytest tests/test_recovery.py -q
```

测试先观察到缺失模块与“接管后解释器改变仍可发送 kill”的红灯，再实现并修复。覆盖正确接管、创建时间/模块/额外命令/配置/路径/只读权限不符、符号链接、缺失创建时间、歧义与超大 JSON、接管后篡改、后代 PID 复用、等待超时和安全终止。真实子进程测试启动实际 `emsolver.worker`，让启动父进程退出，等待数值事件产生，核验新服务可接管该无亲缘工作进程，并在 finally 中回收；不会遗留测试孤儿。当前已实测 macOS Darwin arm64；不推断 Windows 支持。

Apple Silicon 上运行 amd64 Linux 容器时，已观察到 Rosetta（跨架构程序翻译器）将工作进程的实际可执行文件报告为 `/run/rosetta/rosetta`，且容器内无法访问该文件。生产校验仍拒绝接管。真实接管测试仅在实际观察到这一精确路径、文件不存在且再次确认 `verified_worker` 返回 `None` 时跳过，普通 Linux/macOS 断言保持不变。该跳过不代表 Linux 接管验收通过；原生 Linux 硬件验证仍为外部验收边界，见 [平台证据](evidence/v040-recovery-platform.json)。
