# v0.9 工业求解推进与交付

本版新增显式磁盘外对称不定分解，使此前因不完全分解零主元而失败的完整公开微波矩阵得到数值合格候选。**严格目标完整性尚未完成，不能宣称五项工业验收全部通过。** 原矩阵、旧版交付及历史失败保留；不会用新结果覆盖旧记录。

## 五项状态与判定口径

| 项目 | 本轮已取得的证据 | 未完成的部分 |
|---|---|---|
| 完整工业输入求解 | 正式系统任务 `j_b29d2f56d06d4e829e128c553dd1b493` 在原始 `Dziekonski/gsm_106857` 的589446阶、21758924非零元上取得2个合格候选；约84秒完成，最大缩放残差2.71×10⁻¹⁷，原始矩阵未变。 | 任务正确保持 `PARTIAL / TARGET_UNVERIFIED`。数值合格不代表目标无遗漏或物理模型认证。 |
| 目标完整性 | 在完整原矩阵上严格包围两个生产候选：半径分别4.73×10⁻¹⁵和6.21×10⁻¹⁵，两个区间互不重叠，证明至少存在两个不同原问题特征值；另完成严格计数部件及原图成本分析。 | 候选附近存在根不证明目标无遗漏。原型尚不能在完整GSM上给出严格计数。默认AMD下单组显式因子需约2.74GiB（64位索引），估计分解运算约1858亿次，尚未包含输入与误差记录；本机没有完成该证明。 |
| 独立数值参考 | 隔离ARPACK外层算法计算4个模式，前2个与正式候选比较：最大特征值差1.77×10⁻¹⁷、子空间夹角正弦3.88×10⁻¹²；各自在原系数上重算残差。完整原坐标参考资产与新档案已登记到工业项目。 | 两者共用PETSc/MUMPS逆算子；参考集合自身没有严格无遗漏证明，不能升级为可信完整性证书。 |
| 工程来源核查 | 核对收藏站、原作者论文、许可与摘要；原GSM确实来自实际微波滤波器；另固定公开TESLA腔参数与MIT实现入口。 | 原作者明确没有发布GSM尺寸，不能补造几何、频率映射或实物签字。TESLA是另一个模型来源，尚未执行该模型。 |
| 工业资源与性能 | 正式任务满足声明的300秒上限；宿主进程树采样峰值约1.57GiB，容器采样峰值约2.26GiB（含文件缓存），宿主可用余量最低约2.12GiB；未触发外部终止。 | 这些是不同域的采样值，不是同一时刻总峰值。当前k2与k4参考试验不构成同条件速度比。严格目标覆盖尚未通过，不登记工业加速成绩。 |

这里的“惯性”指一个对称矩阵正、负、零特征值各有多少，用于核对目标区间是否遗漏模式；“残差”指把结果代回原方程后剩余的误差。磁盘外分解把大型因子临时存入任务私有目录，减少内存压力。

## 数值与架构

- 新配置 `mumps_ooc_symmetric_v1` 使用 SLEPc Krylov–Schur（子空间特征求解方法）和 MUMPS 带主元的对称不定 `LDLᵀ` 分解。PETSc接口名虽为 `cholesky`，此处明确选择 `sym=2`、关闭正定假设；不是把不定问题当正定问题处理。
- 限制为CPU、实数双精度、单MPI进程、单线程；独立OOC运行自检通过后才在界面开放。旧默认LU路径保留，不能根据公开矩阵答案自动切换或调参。
- 一个作业内存预算分给宿主进程树和容器各一半，容器由内核限制内存及交换空间。MUMPS工作区64–768MiB只是其中一部分，不能视为总内存保证。
- 任务私有目录在启动前、运行中、结束后检查磁盘和日志上限，目录扫描容忍临时因子销毁。父级任务监控复用同一计数逻辑；错误不再因删除竞争丢失。
- 求解容器禁网、根文件系统只读、无额外权限，仅挂载原输入快照与私有输出。参考在候选结束后比较，未送入候选容器。主工作进程仍是可信本机进程，不宣称整个宿主被文件沙箱隔离。
- 候选发布后仍由原矩阵验证器重算残差、正交性与目标认证；MUMPS浮点负主元数量只作诊断，明确不是证书。

## 使用入口

界面在SLEPc后端选择“分解因子存储 → 磁盘因子”，或使用 [完整配置示例](../examples/slepc-ooc.yaml)。配置只有在相应实数OOC自检通过后才可提交；复数、多线程/多秩及混合精度组合会被拒绝。

```bash
export EMSOLVER_SLEPC_IMAGE=emsolver-slepc:bookworm-arm64-v090
export EMSOLVER_DOCKER_SOCKET=/absolute/owned/docker.sock
emsolver solve --matrix INPUT.npz --config examples/slepc-ooc.yaml --output NEW_RUN
```

镜像需单独准备，普通CPU离线包不自动包含Docker或SLEPc镜像。当前实测SLEPc依赖为Linux ARM64、PETSc3.18.5 / SLEPc3.18.2 / MUMPS5.5.1。不得把本机ARM结果解释为比赛指定硬件的成绩。

## 可复核证据

- [正式工业实验](evidence/v090-industrial-experiment.json)、[数值参考与比较](../artifacts/v090-independent-solver/independent-comparison.json)。
- [OOC配置与实际测试](slepc-ooc-profile.md)、[独立生产审查](evidence/v090-ooc-independent-review.json)。审查者是开发子代理，不是外部机构。
- [工程来源](industrial-source-evidence-v090.md)、[原始赛题提取](../artifacts/v090-source-audit/competition-source-extracted.txt)。
- [严格计数路线](v090-count-reference-options.md)、[记录式分解界与范围](v090-recorded-ldl-bound.md)、[独立数学审查](evidence/v090-recorded-ldl-independent-review.json)。研究原型没有启用到生产认证。
- [完整工业候选严格包围与实际成本](v090-spectral-proof-progress.md)、[二范数部件独立审查](evidence/v090-residual-norm-independent-review.json)。完整候选包围实验约0.96秒，观察到进程树峰值约298MiB；512MiB仅为20ms采样监督软边界，不冒充内核硬限。目标认证仍为未验证。

## 软件与安装验证

当前macOS ARM64完整回归 **831通过、23跳过**；显式实际SLEPc/OOC回归 **67通过**，实际Metal回归 **9通过**；前端 **65通过**且类型检查/构建成功。各套件有重叠，不相加为独立测试总数。未选择的可选环境和本机不支持的精度组合在完整回归中明确跳过。

新建源码目录外的macOS ARM64 Python3.12环境，使用本地wheels和 `--no-index` 冷安装；69个生产模块的源码、wheel及安装文件摘要一致。真实HTTP流程覆盖界面、求解、原问题复验、报告、公开包、备份恢复及恢复后新任务，临时服务退出。该证据证明本机离线安装材料可用，不宣称操作系统阻断了网络。

详细记录：[验证汇总](evidence/v090-verification.json)、[Mac冷安装](evidence/v090-macos-delivery.json)、[业务与66项原始条款保留](evidence/v090-data-preservation.json)。

同一wheel还在受限Linux AMD64容器中完成310项测试（5项明确未选），69模块摘要一致；镜像导出后重新载入，7项冷业务流程通过。约束实际核对为2GiB内存、无额外交换、2核、禁网、只读根与移除额外权限。主机为macOS ARM64，AMD64为翻译执行，仅支持功能与分发证据。见[Linux交付](evidence/v090-linux-delivery.json)。

磁盘外后端单独提供 `artifacts/emsolver-slepc-bookworm-arm64-v090.docker.tar.gz`，约336MB；实际重新载入后镜像摘要与67项后端测试、完整原矩阵作业相同，保留278份Debian包版权文件。它是Linux ARM64依赖，普通Mac CPU包和Linux AMD64 CPU镜像不包含它。见[独立后端镜像交付](evidence/v090-slepc-image-delivery.json)。

```bash
docker load --input artifacts/emsolver-slepc-bookworm-arm64-v090.docker.tar.gz
```

原赛题要求数值精度、目标覆盖、稳定性、资源效率及复现证据，没有要求第三方签字或本系统的HMAC回执（用于检查某个审查声明是否由配置的签名者发出）。已有严格审签策略保持可选增强用途，不能将其误写成赛事强制项，也不能用自行签字代替数值与物理证据。比赛报名和官方提交尚未由本地软件执行。
