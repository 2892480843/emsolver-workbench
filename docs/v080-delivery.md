# v0.8 工业求解迭代与系统交付

本轮完成均衡补偿 ILU（不完全分解，用稀疏近似逆辅助迭代）、外层精度继续迭代及精确带权独立计数的集成、界面和安装验证。**完整真实微波矩阵仍未得到合格目标模式及独立完整性证据，F48继续为有条件，不能声明全部工业验收完成。** 其余65项的软件支持范围、原始66条要求及限制保留在[逐项登记](prd-acceptance.md)。

## 本轮实现与架构

| 改动 | 实际行为 | 边界 |
|---|---|---|
| 显式CPU双精度ILU v2 | 固定四次对称均衡、1%行和对角补偿；一次分解，每外层缓存约束逆供全部修正列复用 | 原问题不变；无参数搜索、隐式回退或一般收敛保证；病态小矩阵明确失败 |
| 外层精度继续迭代 | 全部目标候选数值合格后做一次有界认证探测；仅值包围过宽时继续提高精度 | 提示不是证书；几何/预算不适用不会继续无效追逐，最终重新独立认证 |
| 候选保留 | 迭代或重启次数耗尽时保留已有数值有效候选，输出PARTIAL | 内存/墙钟强杀不保证完整产物 |
| 精确带权圆盘 | 原矩阵决定二次幂权重，以保谱相似变换严格计算计数，并复验原矩阵残差、重数、秩及整体子空间 | 只适用于严格分离情形；默认500万运算预扫描预算，超限提前拒绝 |
| 配置与资源 | 按后端能力显示v2；自动双精度，GPU切换清除不兼容ILU；预算包含稀疏临时副本、缩放向量及前后约束缓存并存 | 填充因子不是实际内存上界，原生库仍受父进程监督 |

模块职责、调用链与内存结构见[数值架构](v080-numerical-architecture.md)。固定策略、独立训练/保留集对照及复不定族的失败范围见[预处理v2](preconditioning-v2.md)；严格证明见[独立目标计数](independent-target-counts.md)。数据库仍为schema2，无迁移。

## 真实工业原矩阵

沿用官方 `Dziekonski/gsm_106857` 的完整原数据：589446阶、21758924非零，NPZ SHA-256为 `b56c5bac7fde872951153508eb0641b17e0f52876445d59d4ec8d3ff7687f268`。没有裁剪、改系数或使用参考答案。数据来源、许可和归属见[原档案](evidence/v060-public-industrial-dataset.json)。

通用v2在独立合成集合选定并冻结后，只进行一次公开输入试验：k2、sigma0、子空间12、内层阈值0.001，双精度、2线程、180秒。v2最低规划工作集6393177724字节，8GiB请求无法通过原70%安全门，因此在执行前固定12GiB请求预算；本机另外施加2.5GiB进程树及768MiB宿主可用内存保护，不保证可使用全部请求预算。

**结果为分解阶段 `PRECONDITIONER_FAILED / singular_pivot`，一次尝试、0合格模式。** 墙钟16.66秒；外部采样进程树峰值2011299840字节，CLI采样峰值1979400192字节；额外监督未触发终止，源码在执行前后相同。两种采样峰值单独保留，不互相替代。完整协议、配置、原始事件/结果及摘要见[实验记录](evidence/v080-industrial-experiment.json)和源码包内 `artifacts/industrial-v080-final-v2/`。

该输入还超出带权方法的默认预扫描运算预算，新增证明不能解决其一般完整性问题。原始头部给的是固定频率有限元线性系统；缺少质量矩阵及经审查的频率映射时，不将其标准矩阵特征值冒称器件谐振Hz。旧Jacobi及v1失败继续保留；不同资源/策略、失败试验不能计算加速比。

## 新鲜验证

| 范围 | 结果 | 证据 |
|---|---|---|
| macOS ARM64全量 | 807通过、18跳过、0失败；16项SLEPc需显式启用，2项本机扩展精度不支持 | [日志](evidence/v080-macos-tests.log) |
| 独立代码复核 | 131项定向回归通过，无阻塞发现；与全量有重叠 | [复核](evidence/v080-independent-review.json) |
| 真实可选后端 | 显式SLEPc/MPI实复单秩/双秩33通过；Metal9通过 | [SLEPc](evidence/v080-optional-backend-tests.log)、[Metal](evidence/v080-metal-tests.log) |
| 前端 | 64通过，TypeScript类型检查及生产构建通过 | [测试](evidence/v080-web-tests.log)、[构建](evidence/v080-web-build.log) |
| Mac冷安装 | 新环境无索引安装；69个源码/wheel/安装模块摘要相同；原输入复验、报告、公开包、备份恢复及新任务重跑通过 | [模块](evidence/v080-macos-installed-modules.json)、[冷实例](evidence/v080-macos-cold-instance.json) |
| Linux安装包 | 281通过、0跳过；禁网、只读根、去能力、2GiB/2CPU/128进程、无额外swap；保存加载和冷实例业务链通过 | [Linux](evidence/v080-linux-delivery.json) |

Linux amd64在ARM宿主翻译执行，仅证明功能兼容性。Mac使用 `--no-index` 安装，验证只发回环HTTP请求，未声称宿主操作系统断网。最初全量检查发现环境中仍装旧0.7 wheel导致冷实例版本不符，安装当前冻结wheel后重跑全量通过；初始日志保留。Docker未运行时的拒绝及测试开关未全开的记录也保留；恢复已有Docker并显式启用后，以上33项全部通过。

## 浏览器与旧数据

实际浏览器验证v2选项、自动双精度、GPU切换清除和冻结计划。恢复Docker改变能力后，原计划提交被拒绝，重新冻结后成功提交；旧计划保留。教学模型任务 `j_3f476fa3668e4b989bdc8b2418ccbe2b` 在原1e-8验收阈值下得到2模式、最大缩放残差1.35e-14、正交性误差2.31e-16；8个外层、一次分解、7次约束缓存构造，一次精度继续迭代，原输入新鲜独立复验CERTIFIED。详见[界面证据](evidence/v080-ui-validation.json)与[截图](evidence/v080-ui-result.png)。这是16阶教学模型，不是工业性能结果。

本轮冻结的既有业务实体、事件、审计和幂等记录逐行核对；新增任务只允许更新派生存储计数，具体变化另见[数据保留记录](evidence/v080-original-data-preserved.json)。历史发布包逐摘要核对保留，未覆盖0.7及之前归档。

## 使用与分发

服务地址为 `http://127.0.0.1:8000/`。源码包 `artifacts/emsolver-source-0.8.0.zip`；Mac ARM64/Python3.12 CPU离线包 `artifacts/emsolver-offline-macos-arm64-py312-0.8.0.zip`；Linux amd64镜像包 `artifacts/emsolver-linux-amd64-0.8.0.docker.tar.gz`，镜像名 `emsolver-runtime:0.8.0`。摘要及范围统一在 `artifacts/RELEASE-0.8.0.json`。

公开工业输入包继续使用 `artifacts/emsolver-public-industrial-gsm-106857-v1.zip`，保留原许可和作者归属。源码包带独立合成研究语料、冻结协议与完整失败证据；不包含本机业务数据库或私有输入。材料检查通过只表示所列材料和有限语义齐全，`functional_acceptance=NOT_CERTIFIED`不变；未执行赛事正式提交。
