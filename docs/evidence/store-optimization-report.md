# SQLite 存储优化验证记录

验证环境：macOS 26.6.2 / arm64，zsh，Python 3.12.13，SQLite 3.53.1。所有数据库均由临时目录或 pytest 的 `tmp_path` 创建；未打开或迁移生产 `data/metadata.sqlite3`。未创建 Git 提交。

## 交付范围

- `emsolver/store.py`：schema2 原子迁移、稳定入库序号、查询投影与索引、有界查询和分页、公开一致读取事务、事件读取范围。
- `tests/test_store.py`：真实 SQLite 的迁移、并发、故障回滚、备份、游标、查询计划和事件边界测试。
- 本报告。Service、API、任务状态规则、事件模块和前端由其他开发者负责。

正文仍为原 JSON 文档；`status`、`project_id` 为查询投影，即从正文同步提取、用于加速筛选的辅助列。迁移按旧 `rowid` 升序每批读取 500 行，将原 `rowid` 保留为显式 `seq`，原正文字符串直接复制，不重新编码。`put` 在一条 UPSERT（新增或更新）语句内同步正文与投影，更新保留原序号。

## 测试先行与缺陷复现

| 阶段 | 命令/检查 | 结果 |
|---|---|---|
| 初始红灯 | `.venv-emsolver/bin/python -m pytest tests/test_store.py -q` | 48 failed；原版缺少 schema2、分页/查询/事件接口和参数约束，失败原因符合预期 |
| 首轮绿灯 | 同上 | 48 passed，0.20s |
| 未知旧结构回归红灯 | 增加“列名相同但没有旧复合主键”的数据库测试 | 1 failed、51 passed；原实现误接收非旧版结构 |
| 并发启动缺陷 | 同时打开旧库，运行存储/事件/服务测试 | 真实复现 `PRAGMA journal_mode=WAL` 抛出 `sqlite3.OperationalError: database is locked` |
| 修复后聚焦验证 | `.venv-emsolver/bin/python -m pytest tests/test_store.py tests/test_event_journal.py -q` | 83 passed，0.23s |
| 服务兼容验证 | `.venv-emsolver/bin/python -m pytest tests/test_service_core.py -q -k 'backup or restore or cross_connection or fifo'` | 4 passed、12 deselected，1.39s |
| 并发重复验证 | 临时旧库上重复 30 轮，每轮屏障同时启动两个独立 Store 连接 | 30 轮 / 60 个连接通过；每轮迁移一次、共享实例 ID、任务数及投影一致 |

普通 `.venv/bin/python` 未安装 pytest，因此验证统一使用项目原有 `.venv-emsolver/bin/python`。

并发缺陷的根因是两个连接同时升级 journal 模式时的锁竞争，发生在旧版初始化的迁移事务之前。现在先在 `BEGIN IMMEDIATE` 内检查并迁移，再设置 WAL（预写日志模式）；仅对设置 WAL 时的 `SQLITE_BUSY` 在 30 秒窗口内重试，其他错误直接抛出。迁移仍由数据库写事务串行化。

## 数据和事务验收

- 旧正文含空白、中文及非连续 `rowid` 时，迁移保留原始正文、序号、列表顺序；事件表原记录保留。
- 第二批正文无效时，第一批已经写入的数据也全部回滚；在完成 `DROP` / 表重命名后注入异常，原三列表、记录和 `user_version=0` 仍完整保留。
- 未知未来版本及非旧版表声明拒绝迁移，不改写原结构和记录。构造失败关闭连接、释放数据库锁。
- 两个连接同时迁移得到相同随机实例 ID；重开数据库保留该 ID。
- 原始 SQLite 旧备份可迁移，正文、事件、审计和幂等键完整保留。新的数据库快照继承原实例 ID；通过 `Store.put` 重建的新恢复实例生成新 ID，拒绝原实例分页游标。
- 外层写事务中的正文、投影和事件同步回滚。`read_transaction()` 复用已有事务，嵌套读取不提前提交；异常退出清理自己创建的读取事务。
- 读取页和总数共享一个 SQLite 读取快照；测试在计数与取正文之间用第二个连接修改状态，页内计数与正文仍一致。
- 分页固定首次请求的最大序号；后续新增任务不混入原分页遍历。状态按各请求时的当前值筛选，不承诺跨请求保留历史状态。
- 更新和 `VACUUM`（数据库整理）后序号稳定；分页无重复、无遗漏。游标绑定版本、实例和筛选条件，拒绝多余字段、非有限数值、负数、布尔值冒充整数和超长内容；SQL 参数绑定不拼入筛选值。
- `count` 不解析正文，`first` 只解析一条正文，`select(limit=3)` 只解析三条正文。
- 原无上限事件重放保持兼容；新增 `limit`、`until` 和倒序读取复用原 `(job_id,seq)` 主键。

## 查询计划

真实 `EXPLAIN QUERY PLAN` 输出：

```text
kind:
SEARCH entities USING INDEX entities_kind_seq (kind=?)

kind + status:
SEARCH entities USING INDEX entities_kind_status_seq (kind=? AND status=?)

kind + project_id:
SEARCH entities USING INDEX entities_kind_project_seq (kind=? AND project_id=?)

kind + project_id + status:
SEARCH entities USING INDEX entities_kind_project_status_seq (kind=? AND project_id=? AND status=?)

queued count:
SEARCH entities USING COVERING INDEX entities_kind_status_seq (kind=? AND status=?)

bounded event replay:
SEARCH events USING INDEX sqlite_autoindex_events_1 (job_id=? AND seq>? AND seq<?)
```

这四种实体首条查询均无临时排序。多状态集合筛选仍可能需要合并/排序符合条件的序号；本轮队列首条选取使用单个 `QUEUED` 状态。

## 本机诊断性计时

临时数据库写入 20,000 条任务，正文包含 1,800 字节 payload，最后 20 条为 `QUEUED`；每项运行 7 次，记录中位数：

| 操作 | 中位耗时 |
|---|---:|
| 原调用方式：完整 `list` 后在 Python 找第一条排队任务 | 48.8135 ms |
| 新 `first(status='QUEUED')` | 0.0095 ms |
| 新 `count(status='QUEUED')` | 0.0028 ms |
| 新 `page(limit=50)`，包含总数 | 0.3587 ms |

这些数字仅说明本机合成数据下的差异，不能作为生产性能承诺。分页限制正文读取及解析量，但精确总数仍扫描匹配索引，不是固定时间操作。

## 范围边界和剩余关注点

- 未在真实业务数据库上执行迁移，真实数据迁移的耗时、剩余磁盘空间和停机窗口未验证。原子重建过程中同时保留新旧实体表，并产生事务日志，需要额外磁盘空间。
- 手工写入 `rowid <= 0` 的非正常旧库被安全拒绝；原版 Store 正常分配的入库序号为正数。拒绝时旧库完整保留。
- schema2 无法由旧版裸 `INSERT INTO entities VALUES(?,?,?)` 写入。程序降级应使用原数据库备份，不应直接交给旧版程序继续写入。
- 旧 `list` 和不传 `limit` 的 `events` 有意保持原兼容语义；内部队列及新接口需使用新增有界方法才能获得收益。
- 分页游标是经过严格结构校验的不透明标记，不是权限凭证，也未提供加密签名。新恢复实例通过实例 ID 拒绝旧游标。
- 服务全套及前端端到端验证由主开发在完成集成后执行。本次服务聚焦验证未发现存储兼容问题；联调早期另两项服务测试涉及新增任务状态约束，已向主开发交接。
