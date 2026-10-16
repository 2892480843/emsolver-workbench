# 参数实验与应用架构迭代

用户已授权继续完善系统架构和功能。本轮保持模块化单体及 schema2，数值内核不变，增加可检查、可冻结、可重放的参数实验。

## 实施步骤与验收

- [x] 独立实验模块：严格规格、去重、组合展开、总预算预检；无效组合整批回滚。
- [x] 冻结实验计划与原子提交；重复提交返回原批次；提交时重新检查输入、能力和队列容量。
- [x] 组合与重复编号、全部运行及认证成功耗时分母、批次取消、摘要分页；兼容旧基准记录与接口。
- [x] 独立前端实验工作区：组合设计、预算预览、冻结确认、分组结果、配置差异及报告。
- [x] 后端规则建议保护：仅允许白名单求解参数，保护输入、目标、验收和全部资源限制；返回逐字段差异。
- [x] 更新备份引用验证、架构与覆盖登记，完成自动测试、浏览器真实求解和独立审查。

## 模块契约

`emsolver.experiments.ExperimentService(gateway)` 使用窄协议访问既有应用服务，遵循 `_mutex → store.transaction()` 锁顺序。对外由 `Service` 代理；实验模块不依赖 API、前端或数值内核。

`plan(spec)`：规范化严格规格后，在一个写事务内冻结每个组合的现有求解计划和配置，存为 `experiment_plans`。预算失败或任一组合无效不留下任何实体。冻结操作会保存计划，但不提交任务。

规格字段：`project_id, matrix_ids, base_config, k_values=[6], sigma_values=[0], threads_values=[1], memory_limit_gib_values=[16], adaptive_shift_values=[true], repeats=1, budget`。轴各自有界、保序去重；组合最多100、总运行最多100。仅支持最近k目标；矩阵及storage由选定资产绑定。`budget={max_runs:100,max_total_walltime_seconds:7200,max_total_disk_gib:200}`。累计时间/磁盘为逐次运行硬限额之和，非预测耗时；单工作进程内存取最大配置值，不声称CPU线程实际利用率。

计划响应：`id, schema_version, project_id, definition, hash, cells, budget, warnings, created_at`。`cells` 每项为 `id,matrix_id,matrix_hash,plan_id,plan_hash,config,parameters,configuration_diff`；参数含 `k,sigma,threads,memory_limit_gib,adaptive_shift`。差异项为 `{path,before,after}`，路径使用点分字段名，按字典序排列。每个矩阵的首个组合为差异基线。

`submit(experiment_plan_id)`：批次ID由计划ID确定；已存在时返回原批次。首次在同一事务内重新预检总队列容量，调用既有提交能力并写入 `experiment={batch_id,cell_id,repeat}`（重复编号从1开始）。批次仍存于 `benchmarks`，增加 `experiment_plan_id, experiment_hash, definition, cells, budget`；旧字段 `jobs,matrix_ids,repeats,plan_hashes` 保留。所有实体、事件及审计共同提交/回滚。

`summarize_batch(batch,jobs)`：返回 `{summary,groups}`；旧 summary 字段与含义保留，新增认证成功耗时统计。每组包含 `cell_id,parameters,matrix_id,job_ids,summary,configuration_diff`；失败、取消、超时与部分结果均保留在总数和分母。组间资源或目标不一致时不自动计算加速比。

## API

- `POST /api/v1/experiments/plans` 接收上述规格，返回冻结计划。
- `POST /api/v1/experiments/submit` 接收 `{experiment_plan_id}`，返回含全部运行的批次。
- `GET /api/v1/benchmarks/page?project_id=&limit=20&cursor=` 返回 `{items,next_cursor,total}`；仅摘要及分组，不返回全部runs。
- `GET /api/v1/benchmarks/{id}` 保留旧字段，新增groups。
- `POST /api/v1/benchmarks/{id}/cancel` 幂等取消未结束子任务；终态保持原值。
- `POST /api/v1/configs/advice` 接收 `{job_id?,config?,changes}`，二选一指定原配置，返回新配置快照和差异。保护字段由服务器校验。

## 限制

不引入外部队列或数据库。不把自适应工作位移开关扩展为尚未实现的算法。泛型工业矩阵目标完整性、MPI/GPU、多用户权限及Linux实机验收仍按覆盖登记明确标记。
