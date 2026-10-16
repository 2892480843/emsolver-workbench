# 架构优化独立审查

审查日期：2026-10-16。环境：本机 macOS（`uname -s` 返回 `Darwin`），`zsh`；仅使用临时目录和临时数据库，未通过 `Store` 打开业务数据库，未修改实现代码。

## 当前结论

第一轮及前端追加审查发现的 **2 项重要问题均已修复并独立复测**：重复取消异常、终态并行读取遗漏最后事件。后续恢复模块审查未发现新增的重要缺陷。存储迁移、索引查询、分页及事件日志模块的定向测试 **84 项通过**，前端模型测试 **12 项通过**，接口、恢复等筛选测试 **8 项通过**；这不等于完整浏览器流程已完成验收。

审查依据：`docs/architecture-optimization-plan.md`、`docs/architecture-optimization-contract.md`、当前源码与 `artifacts/architecture-baseline/source.zip`。未使用外部信息作为结论依据。

## 发现

### R1：重复取消导致异常（P2，重要）

- **触发条件**：第一次取消已把 `RUNNING` 任务变为 `CANCELLING`，进程结束前第二次取消同一任务。
- **位置**：`emsolver/service.py` 的 `cancel_job` 在处理终态、排队态后无条件调用 `transition(job, JobStatus.CANCELLING)`；`emsolver/jobs.py` 明确禁止 `CANCELLING -> CANCELLING`。
- **结果**：第二次调用抛出 `ValueError: INVALID_JOB_TRANSITION: CANCELLING -> CANCELLING`。接口未转换该异常时会产生服务端错误；重复点击或网络重试可触发。
- **基线对比**：基线 `cancel_job` 使用 `job.update(status='CANCELLING', stage='cancelling')`，允许重复取消。
- **建议**：在服务层对已经 `CANCELLING` 的任务返回当前结果，保持状态规则严格；补充重复请求回归测试。
- **状态**：主开发增加 `CANCELLING` 直接返回后，独立临时数据复测通过：两次返回相等，仍为 `CANCELLING`，数据库仅有一个 `CANCEL_REQUESTED` 事件。

复现命令（仅临时数据）：

```bash
.venv/bin/python - <<'PY'
import tempfile
from emsolver.service import Service
with tempfile.TemporaryDirectory() as root:
    service = Service(root, start_runner=False)
    service.store.put('jobs', 'j_repeat', {
        'id': 'j_repeat', 'status': 'RUNNING', 'stage': 'solving',
    })
    print(service.cancel_job('j_repeat')['status'])
    try:
        print(service.cancel_job('j_repeat')['status'])
    except Exception as error:
        print(type(error).__name__, str(error))
    service.close()
PY
```

实际输出：

```text
CANCELLING
ValueError INVALID_JOB_TRANSITION: CANCELLING -> CANCELLING
```

### R2：终态并行读取可能提前停止事件轮询（P2，重要）

- **触发条件**：`web/src/models/jobs.ts` 的 `readDetailCycle` 同时请求任务状态和首个事件页；事件查询先读到终态提交之前的空页，任务查询随后读到 `SUCCEEDED`。
- **结果**：`has_more=false`，结果查询成功，函数直接返回 `delay=null`，最后终态事件不会再补读。
- **建议**：确认终态之后再读取事件，确保终态事务的事件已对该次读取可见；保留每轮最多五页的限制。
- **可复现实证**：对已构建模型传入异步模拟请求，任务请求在一次微任务等待后设 `terminalRead=true` 并返回成功，事件请求根据该标记返回空页或 `seq=1` 的终态事件，结果请求返回有效结果。原实现输出：`{"eventReads":1,"eventCount":0,"nextDelay":null}`。
- **状态**：前端改为先请求任务，再请求事件；独立执行 `node --test web/tests/jobs-model.test.mjs`，12 项通过，包含新加的确定性终态并发回归。

## 已验证范围

| 项目 | 证据 | 结果与边界 |
|---|---|---|
| 存储、事件、状态规则回归 | `.venv-emsolver/bin/python -m pytest -q tests/test_store.py tests/test_event_journal.py tests/test_job_queries.py` | `84 passed in 0.23s` |
| 双连接同时打开旧库 | 100 个独立临时旧库，每轮使用线程屏障同时运行两个 `Store(path)` | 100 轮零异常；有限压力样本 |
| 迁移原子性 | 代码审查及迁移测试 | 写事务内重查版本、按旧 `rowid` 拷贝、保留原 JSON 文本，异常回滚，包括已复制批次和换表后的注入故障 |
| 分页边界 | 代码审查及游标测试 | 游标（记录下一页位置的令牌）绑定实例和筛选；拒绝布尔整数、非有限数、超限值、重复字段及额外字段；固定序号上界 |
| 投影与顺序 | 代码审查及更新、VACUUM 测试 | 同一写入更新正文及查询字段，更新不改变稳定序号；分页不承诺跨请求状态快照 |
| 事务复用 | 代码审查及读事务测试 | 读事务复用已有写事务，保持页内计数与数据一致；外层事务异常回滚 |
| 事件一致性 | 代码审查及事件测试 | 追加只写数据库；外层回滚不产生文件；导出以固定最大序号分批写临时文件并原子替换 |
| worker 最后补读 | 服务调用链审查 | 进程循环结束后再读取完整事件行，再写终态事件；未完成换行的半行不作为完整事件导入 |
| 前端模型 | `node --test web/tests/jobs-model.test.mjs` | 12 项通过，含终态事件读取顺序、最多五页、事件去重和1000条窗口 |
| 前端请求边界 | `useJobs`、`useJobDetail`、`useWorkspaceData` 代码审查 | 范围变化、手动刷新与卸载会中止旧请求；发布结果前检查取消标记；提交响应可直接展示新任务。已加载尾页仅由手动刷新更新是当前约定边界 |

首次尝试 `.venv/bin/python -m pytest ...` 时该环境没有 `pytest`，随后切换项目 `.venv-emsolver` 环境，未安装依赖。

## 恢复模块补充审查

审查 `emsolver/backup.py` 及稳定后的 `Service.restore_backup`，未发现新增的重要或严重缺陷。

| 项目 | 实证与结论 |
|---|---|
| 旧格式与顺序 | 创建旧三列结构、501 个项目且标识按倒序插入，执行 `rebuild_metadata`：保留源入库顺序，目标版本为2，返回501条 |
| 延迟故障回滚 | 在以上源库添加非对象的审计记录，实体全部复制后恢复报错；检查目标实体数为0，证明前501条实体共同回滚 |
| 分批与关联校验 | `_rows` 使用每批500行读取；实体引用通过源数据库查询校验，无全量实体字典；实体、事件、审计与幂等记录在一个目标写事务内复制 |
| 源与目标隔离 | 源文件使用独立名称，以只读、不可变模式打开；目标使用 `Store` 重建受控结构，不复制源触发器、视图或实例标识 |
| 新结构恢复 | 现有专项测试确认队列顺序、索引投影、1006条事件、全新实例标识；禁止源查询使用 `fetchall` 的测试通过 |
| 事件导出符号链接 | 临时实验确认指向数据根外的目录链接在创建文件前被拒绝；最终文件若为符号链接，原子替换仅替换链接本身，链接目标内容不变 |
| 报告读取上限 | `create_report` 和复现包构造改用 `journal.tail`，数据库读取最多1000条；完整事件下载继续走分批 `journal.export` |
| 接口与兼容 | `.venv-emsolver/bin/python -m pytest -q tests/test_architecture_service.py tests/test_service_core.py -k 'restore or cancel or paged_api or worker_final'`：8项通过，15项未选择；1条已有 Starlette/httpx 弃用提示 |

读事务仅用于页面计数/数据和工作区统计的一致读取；当前服务调用链没有在该读事务内升级为写事务的用法，未发现应阻止交付的事务嵌套问题。导出固定上界依赖事件只追加的当前契约；未测试同一操作系统用户恶意并发替换目录等超出应用数据边界的场景。

## 剩余验证边界

未重跑全套测试，未执行生产数据迁移，未操控浏览器。浏览器实际翻页、切换任务、增量事件与下载的端到端验收由主开发执行。当前已加载尾页在自动轮询时保留较早状态、通过手动刷新更新，属于明确约定的首页轮询范围。
