# v0.3独立审查记录

范围：实验领域模块、应用与API适配、建议策略、备份恢复、报告、前端模型及组件；以`artifacts/experiment-baseline/source.zip`为改动前本地快照。仓库尚无提交，不虚构提交ID。

| 发现 | 修复 | 回归证据 |
|---|---|---|
| 普通任务可占用实验幂等键，任务没有组合归属 | 实验成员关系纳入提交请求摘要；冲突整批回滚 | `test_regular_job_cannot_capture_an_experiment_idempotency_key`，先复现失败再修复通过 |
| 恢复接受同批次内错绑、重复编号或缺失反向归属 | 校验计划/配置/矩阵/项目、完整cell×repeat唯一覆盖、正反向关联 | `test_restore_rejects_corrupt_experiment_links_even_with_valid_archive_checksum` |
| 重算方案哈希后，定义预算可能与实际配置不一致 | 冻结与重放共用配置构造，提交及恢复共用严格元数据校验 | 四类重新计算哈希的定义/配置差异回归；`validate_frozen_plan` |
| Python字典比较将布尔True与整数1视为相等 | 配置和快照以规范化内容哈希比较，重复编号显式整数 | `boolean_threads`恢复拒绝回归，先失败后通过 |
| 提交响应可能覆盖用户后来选择的批次 | 选择版本计数器随请求快照，返回时核对未发生新选择 | 前端代码独立复核；当前TypeScript源检查通过 |
| 报告通用脱敏删除配置字段path | 报告将配置字段名称明确改为field，继续脱敏和HTML转义 | 报告中字段名称及脚本转义回归 |

独立审查最终结论：未发现剩余Critical/Important问题；定向70项测试通过，当前TypeScript源检查通过。主开发随后完整测试268项通过，前端30项通过，浏览器验证真实运行、重复提交、预览撤销、取消与报告生成。自动测试不等同于工业数据验收。

浏览器报告操作完成服务端POST201和GET200，报告字节已核对；浏览器工具的下载事件捕获超时，未核验其最终本地保存位置。服务端生成的报告另保留在`artifacts/experiment-report-v030.html`供直接查看。
