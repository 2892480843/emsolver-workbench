# 异步矩阵导入

`emsolver.imports.ImportManager` 将数值读取与结构检查放入独立进程。API 层负责流式接收上传；本模块接收已经写入应用私有 `inbox` 的普通临时文件并接管其生命周期。无需外部服务。

## 集成接口

```python
manager = ImportManager(gateway, start_worker=False)
record = manager.submit(project_id, path, filename, name=None, storage='full', metadata=None)
record = manager.get(record['id'])
records = manager.list(project_id=None)
record = manager.cancel(record['id'])
processed = manager.run_once()
manager.close()
```

`start_worker=True` 才启动守护线程并进行启动恢复；通常传入应用是否持有运行器锁的状态。默认不启动后台线程，`run_once()` 可确定性地处理一个排队项；处理了排队项返回真，无任务或同一管理器已有处理过程时返回假。`close()` 停止线程并终止自身启动的子进程；不关闭应用的数据库。

网关提供 `root`、`store`、可重入 `_mutex`，以及 `_project`、`_get`、`_public`、`_validated_metadata`、`_kill_tree`。最终登记回调为：

```python
gateway._register_import_result(project_id, path, name, storage, metadata, diagnostic)
```

该回调返回包含 `id` 的公开矩阵记录，负责沿用现有哈希去重、只读落盘、配额和登记规则。传入的 `path` 为通过独立进程校验的原始数值文件，`diagnostic` 是结构检查结果；无需在主服务进程再次加载矩阵。回调在原提交身份下、网关互斥锁与数据库事务中执行。回调自身的数据库修改必须复用该事务；发生异常时不能捕获并静默提交半成品。

## 记录和状态

唯一新增存储种类为 `imports`，ID 格式为 `i_` 加 32 位 UUID。公开记录包含：

- `id`、`project_id`、`filename`、`name`、`storage`、`metadata`。
- `status`：`QUEUED`、`RUNNING`、`CANCELLING`、`SUCCEEDED`、`FAILED`、`CANCELLED`。
- `stage`：`queued`、`validating`、`cancelling`、`succeeded`、`failed`、`cancelled`。
- `bytes_received` 与 `total_bytes`：API 完成接收后实际文件字节数，二者相等；不伪造校验进度。
- `matrix_id`：成功后关联的矩阵 ID，其他状态为空。
- `error`：失败时为仅包含稳定错误码的对象，避免泄露路径、矩阵值和自由格式异常文本。
- `created_at`，开始后的 `started_at`，终态后的 `finished_at`，进程启动后的 `worker_pid`，成功后的 `sha256`。

私有字段包括受管暂存路径、冻结内容哈希、提交者的 `subject/global_admin/mode` 及进程创建时间；公开接口去掉以下划线开头的字段。不保存令牌或凭据。

提交和取消要求项目写权限；查询要求读权限。列表过滤无权访问的项目。最终登记再次在原身份下检查权限，因此撤销提交者权限后，正在排队或校验的导入不能借后台线程身份完成写入。

## 进程与文件约束

父进程执行：

```sh
python -m emsolver.import_worker --spec SPEC_FILE --result RESULT_FILE
```

实际调用使用当前 Python 解释器、不使用 shell；标准输出与错误不进入用户响应。子进程使用 `load_matrix` 与 `inspect_matrix`，保留 `allow_pickle=False` 及已有文件、压缩、尺寸、数值和厄密结构校验。

默认上限为原始输入 1 GiB、校验 120 秒、进程树常驻内存 2 GiB，每 100 毫秒检查取消、时间和内存。线程库限制为一线程。结果文件不超过 1 MiB；父进程拒绝重复 JSON 字段、非有限数、未知主字段、超预算预览和不符合结构的诊断。子进程读取前、读取后以及父进程登记前均核对冻结输入哈希。

暂存文件改为管理器生成的名称，输入、说明文件及结果文件权限为 `0600`，`inbox` 权限为 `0700`。拒绝指向其他目录或符号链接的输入；失败与取消只清理管理器接管的文件，不能删除外部文件。输入文件、进程说明和结果在终态清理；有效排队文件在普通关闭时保留，以便同一数据目录重启继续处理。

## 取消、恢复与备份

所有数据库状态修改遵守 `_mutex` 先于 `store.transaction()` 的锁顺序。排队取消直接进入 `CANCELLED`；运行取消先记为 `CANCELLING`，再终止子进程树。最终登记和取消竞争同一事务边界：取消先获得边界时禁止登记；登记已提交时取消返回已有成功记录。

持有运行器所有权的实例启动时调用 `recover()`。历史 `RUNNING/CANCELLING` 统一变为 `FAILED/WORKER_LOST`，不会声称恢复旧进程或续算。仅当进程命令、受管说明文件路径和创建时间都匹配时才终止遗留进程，避免仅凭 PID 误杀其他进程。缺失暂存文件的 `QUEUED` 同样变为 `FAILED/WORKER_LOST`。普通只读或次要服务实例使用 `start_worker=False`，不会改写另一个运行器的活动状态。

备份若不包含 `inbox`，恢复侧必须保留记录并将未完成导入标记失败；不能将其显示为成功或重新制造输入。原有已完成矩阵由矩阵备份策略保护。

## 验证

在本地 macOS arm64 环境执行：

```sh
.venv-emsolver/bin/python -m pytest tests/test_imports.py -q
```

测试使用真实 Python 子进程验证合法输入、损坏文件、对象数组拒绝、输入变更、权限撤销、配额错误清理、运行与排队取消、登记竞争、丢失进程恢复、实际超时与内存监控、后台关闭和异常后继续队列。无需网络或外部计算后端；全新 Linux 环境的进程与文件行为仍需部署侧验证。
