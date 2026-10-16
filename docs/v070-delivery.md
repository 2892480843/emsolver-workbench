# v0.7 数值能力与系统交付

已补齐固定CPU双精度ILU预处理、分离谱独立计数、配置界面和有界结构检查，当前本机服务为`http://127.0.0.1:8000/`。**完整真实微波矩阵仍未通过正向数值验收，因此不能声称全部工业要求完成。** PRD继续为65项已实现、F48一项有条件，原条款与验收阈值保留。

## 本轮实现

- `BudgetedILU`独立承担预检、固定分解、复用、有限值检查和成本事件；最终原问题仍使用双精度验证。默认配置及旧冻结哈希不改变。
- 大于有界精确消元范围的输入，增加严格分离条件下的独立圆盘计数，检查候选分组重数、局部及整体子空间；无法证明时返回未认证。覆盖实数／复数、零根、半开端点与重复根反例。
- 独立审查发现并修复重复报值、病态基整体角度和CSR缓存标志绕过；320组小规模对抗核对未见错误认证，详见[审查证据](evidence/v070-independent-review.json)。这是有限实验结论。
- 结构分类采用65536项分块索引检查，避免为全部非零项扩展行索引。业务数据库仍为schema2；无结构迁移。
- 前端按后端能力启用ILU，自动双精度、拒绝非法组合、切换GPU清除不兼容值；分解失败记录阶段与原因，CLI返回数值失败码3。

算法、内存分项、数据结构及引用见[架构说明](v070-numerical-architecture.md)，数学条件见[独立目标计数](independent-target-counts.md)。

## 原始工业输入的实际结果

完整`Dziekonski/gsm_106857`为589446阶、21758924非零元，原数据来源、许可、归属与哈希沿用[原始证据](evidence/v060-public-industrial-dataset.json)，本轮没有裁剪矩阵或修改系数。固定通用预处理策略在试跑前冻结，不因输入名称调整分解参数。

| 实验 | 预算与目标 | 已观察结果 |
|---|---|---|
| 首次ILU预检 | k2、sigma0、子空间12、6GiB／180秒 | 最低规划情景约4.683GiB，超过6GiB的70%安全门，分解前拒绝。 |
| 增加请求预算 | 同一策略与目标，8GiB／180秒 | 进入分解后失败，20.48秒、采样进程树约1.40GiB，无合格模式。 |
| 最终0.7版本重跑 | 同上；另加外部2.5GiB进程树及768MiB宿主余量保护 | 15.66秒，CLI采样峰值1611235328字节；`PRECONDITIONER_FAILED`，结构化原因`singular_pivot`，一次尝试、0模式。外部监督未触发终止。 |
| 独立计数条件探测 | 完整原矩阵、原目标，无候选或参考；2GiB／30秒外层保护 | 返回`UNVERIFIED / too_many_disks_cover_target`，说明该目标不满足新证明方法的分离条件。 |

三次不可变协议、原始结果与额外监督峰值见[全部实验记录](evidence/v070-industrial-experiments.json)。本轮k2与v0.6的k4不同，均没有工业通过结论，不能据耗时计算性能提升。100003阶合成证明只验证对应数学路径，不能代替此真实微波矩阵。

## 验证

| 范围 | 结果 | 证据 |
|---|---|---|
| macOS ARM64全量 | 738通过、18跳过、0失败；16项SLEPc需显式启用，2项扩展精度本机不具备 | [全量日志](evidence/v070-macos-tests.log) |
| 新数值及认证专项 | 82通过，包括原精确认证、ILU、分离谱与实际导出复验 | [专项日志](evidence/v070-numerical-tests.log) |
| 真实可选后端 | 显式SLEPc／MPI 33通过，Metal 9通过；与全量有重叠，不合计为独立总数 | [SLEPc](evidence/v070-optional-backend-tests.log)、[Metal](evidence/v070-metal-tests.log) |
| 前端 | 63通过，类型检查与生产构建通过 | [测试](evidence/v070-web-tests.log)、[构建](evidence/v070-web-build.log) |
| macOS新安装 | 无索引安装新wheel；服务、原输入复验、导出、备份恢复与新任务重跑通过 | [冷安装证据](evidence/v070-macos-cold-instance.json) |
| Linux安装包 | 212通过、0跳过；禁网、只读、去能力、2GiB／2CPU／128进程，冷启动与保存加载通过 | [Linux证据](evidence/v070-linux-delivery.json) |

Linux amd64在ARM主机翻译执行，仅证明运行兼容性。68个Python模块在源码、wheel及Linux安装目录逐文件一致；新版本包不覆盖历史包。

## 浏览器与历史数据

实际浏览器验证ILU选择、CPU双精度、GPU切换清除和完整提交链路；500阶合成任务失败、16阶教学任务在1e-8阈值下为PARTIAL，复制为更严格的1e-14新任务后为SUCCEEDED，原输入有界独立复验为CERTIFIED。完整失败／部分／通过三份历史都保留；不是工业性能证据，见[界面记录](evidence/v070-ui-validation.json)与[结果截图](evidence/v070-ui-result.png)。

本轮前98实体、328事件、83审计、35幂等记录的冻结业务内容保持。新增任务使两个派生存储用量计数更新，原始逐行严格比对和按字段审查均保留；未将这些正常计数变化写成“全部行未变”。见[数据保留检查](evidence/v070-original-data-preserved.json)。

## 分发

- 源码及构建前端：`artifacts/emsolver-source-0.7.0.zip`。
- macOS ARM64/Python3.12 CPU离线包：`artifacts/emsolver-offline-macos-arm64-py312-0.7.0.zip`，解压后运行`./install.sh`。
- Linux amd64：`artifacts/emsolver-linux-amd64-0.7.0.docker.tar.gz`，加载后镜像`emsolver-runtime:0.7.0`。
- 原公开工业数据包继续使用`artifacts/emsolver-public-industrial-gsm-106857-v1.zip`，不重复改包或省略署名。
- 所有摘要与明确范围见`artifacts/RELEASE-0.7.0.json`；赛事报名和正式提交未据此执行。

剩余关键验收是**完整工业原矩阵的合格目标模式与独立完整性证据**，以及形成器件频率结论所需的合法模型及独立审查。新预处理在当前公开输入上的零主元失败不能通过修改“已完成”状态消除；需继续发展在独立训练／测试集合上验证的通用求解方法，保留公开输入为未调参验收集。
