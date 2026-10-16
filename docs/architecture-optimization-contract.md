# 架构优化模块契约

## 文件归属

- 存储开发：`emsolver/store.py`、`tests/test_store*.py`。不改Service/API/前端。
- 事件开发：`emsolver/events.py`、`tests/test_event_journal.py`。不改Service/Store/API。
- 前端开发：`web/`。不改Python与覆盖登记。
- 主开发：`emsolver/jobs.py`、Service/API集成、备份恢复、其余测试/文档。

## 存储（任务A）

`Store`保持旧`put/get/list/transaction/audit/close`契约。schema2的`entities`列为`seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,status TEXT,project_id TEXT,UNIQUE(kind,id)`。正文单独存放，status/project_id由正文提取，在同一UPSERT中更新。索引覆盖kind+seq、kind+status+seq、kind+project_id+seq、kind+project_id+status+seq。更新不改变seq。

旧库user_version=0、3列entities需在BEGIN IMMEDIATE内迁移：重新检查版本，建新表，按旧rowid分批复制且把rowid保留为seq，正文字符串不重编码，最后原子替换并设user_version=2。未知未来版本拒绝；失败回滚。启动同时建立`store_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)`中的随机实例ID；新恢复库生成新ID，游标不得跨实例复用。

新增：

```python
count(kind, *, status=None, project_id=None) -> int
status_counts(kind, *, project_id=None) -> dict[str | None, int]
list(kind, *, project_id=None) -> list[dict]  # 旧无范围调用保持，完整列表不截断
first(kind, *, status=None, project_id=None) -> dict | None
select(kind, *, status=None, project_id=None, limit=100) -> list[dict]  # status允许str或非空tuple[str,...]；limit 1..1000
page(kind, *, status=None, project_id=None, limit=50, cursor=None) -> dict
# {items:[...], next_cursor:str|None, total:int}
```

page默认升序seq，limit 1..200。cursor至多1024字符，严格校验版本/实例/筛选条件/有限非负整数，不接受bool充当int。首请求冻结该kind当前最大seq，后续只读seq>after且<=upper，防新插入漂移；每次读页及计数处于一致读取事务。返回正文不塞入内部seq。动态状态为请求时的当前值，不承诺跨请求事务快照。非法参数/游标抛`ValueError`，SQL始终绑定参数。

事件增强：

```python
events(job_id, after=0, *, limit=None, until=None, descending=False) -> list[dict]
event_max_seq(job_id) -> int
```

旧无limit语义保留；有limit为1..1001（支持API多取一行检测has_more）。事件原主键(job_id,seq)足够，不新加重复索引。

验收：迁移保留正文及顺序；同时打开迁移幂等；回滚保留原库；put更新投影/不改变seq；分页固定上界、无重复、绑定筛选/实例、VACUUM后稳定；first/count只解析必要正文；EXPLAIN命中索引；旧事件契约保留。

2026-10-16 增补：`status_counts` 只聚合状态投影，不读取正文；未知字符串和 NULL 均保留，由调用者解释。任务汇总在同一 `read_transaction` 内执行状态聚合、项目计数及矩阵计数，共 3 次 SELECT；任务总数取所有状态计数之和，公开状态仍为固定九种。指标把未知状态合并为 UNKNOWN。`list(project_id=...)` 通过现有项目索引过滤，按 seq 返回完整范围，不以单页上限截断旧列表。

## 事件（任务B）

```python
WorkerEventReader(path: Path)
WorkerEventReader.drain() -> list[dict]
# 维护字节offset，完整换行后才消费，半行等待下次；无效JSON完整行跳过。
# 必须按worker.py同样白名单字段过滤，移除message/敏感字段。
EventJournal(root: Path, store: Store)
EventJournal.append(job_id, event) -> dict  # 仅数据库，不写文件
EventJournal.page(job_id, after=0, limit=200) -> dict
# {items:[...],next_after:int,has_more:bool};limit 1..1000, after>=0；多取1行判断
EventJournal.tail(job_id, limit=1000) -> list[dict] # 按seq正序返回最近limit条
EventJournal.export(job_id) -> Path
# 初始event_max_seq冻结上界，按500条分批读取，写root/runs/jobid/events.jsonl
# 以唯一临时文件+replace原子发布；失败清理临时文件，数据库不变。
```

只信任服务生成的job ID，但export仍拒绝路径穿越；JSONL不允许NaN。已有数据库事务中append跟随回滚；不生成幽灵事件文件。测试真实Store，可先只写测试确认现版本缺少模块；等待Store扩展到位后运行完整测试。

## API/前端（任务C）

旧API返回不变。新增：

- `GET /api/v1/jobs/page?limit=50&cursor=...&project_id=...&status=...`→`{items,next_cursor,total}`；上限200；status使用现有合法状态，不含ALL。无project/status表示全部。
- `GET /api/v1/jobs/{id}/events/page?after=0&limit=200`→`{items,next_after,has_more}`；after>=0，上限1000。
- `GET /api/v1/workspace/summary?project_id=...`→`{projects:int,matrices:int,jobs:int,active_jobs:int,certified_jobs:int,job_status_counts:{QUEUED:int,...}}`。指定项目时须先鉴权，projects 为 1；未指定项目时为可见项目数量。matrices/jobs 遵守实际项目范围，非管理员仍须满足现有项目选择规则。

前端任务列表加载最新? 本轮分页按入库升序，与旧数组顺序一致；新提交后选中详情直接响应对象保证可查看；已加载任务页可刷新，用户通过“加载更多”浏览后续历史。总量取summary。UI状态筛选交给分页接口，避免只筛当前页；项目范围改变重置列表游标。

事件按seq增量合并并去重，视图窗口最多1000条（可见提示及完整下载入口）。has_more时连续读取后续页但每次轮询最多5页，之后短间隔继续，避免无限循环。任务到终态后先排空已发布事件且成功读取结果，再停止详情轮询；失败结果404/409属于正常无结果终态。切换任务中止旧请求和游标。使用AbortController，取消不显示成业务错误。

任务列表轮询只刷新已加载首页/当前筛选，不能反复请求旧`/jobs`；静止且无活跃任务降低频率。数值结果、失败状态、配置编辑与其他工作区行为不变。新增单元测试可使用Node内置test加TypeScript构建产物或现有工具，避免大规模依赖更换；浏览器端到端由主开发集成后验证。

## 2026-10-16 请求和关联查询边界

- 矩阵列表按不同项目在单次调用内复用可见性判断；配置列表按不同矩阵复用可见性布尔值，不额外持有完整矩阵诊断对象。不跨请求保存权限判断，身份切换和成员变更在下次调用重算；孤儿记录仍不可见。
- 导入列表先校验显式项目，再在 SQL 中过滤记录；逐行鉴权、拒绝审计和写事务保留。全量接口与私有字段过滤保持兼容。
- 异步导入接口通过既有 `run_in_threadpool` 等待同步 submit，保留身份上下文和 finally 文件清理。此边界不代表上传落盘完全异步；文件哈希仍持服务锁和写事务。
- 任务表按矩阵 ID 构建 Map（键值索引），矩阵数组未变时复用；重复 ID 仍使用第一条，未知 ID 和空名称仍沿用原回退。
- 性能诊断脚本必须校验实际加载的工作区模块，记录源码 SHA、原始样本、查询数及解码数。旧安装包与工作区代码不得混测。
