# v0.5 数值协议、认证与恢复

适用环境：本地已验证 Darwin 25.6.0 / arm64，隔离 Python 环境 `.venv-emsolver`。

当前版本保留统一后端协议、可选稀疏重排、分项内存估算、策略来源和零空间诊断，新增有界一般稀疏独立谱认证、固定工作位移块算法完整状态恢复，以及通过实际运行门禁的可选 SLEPc/MPI 和 Metal 混合修正路径。默认继续使用 SciPy ARPACK 与精确稀疏 LU；可选后端需要对应解释器、已验证镜像/设备和明确支持的配置组合。

实施与验收顺序：

1. 先加入协议生命周期、重排原索引恢复与预算分项的失败测试，再实现公共接口。
2. 先加入原始输入残差及零空间诊断测试，再实现诊断与产物；所有验证继续针对原问题。
3. 增加真实块位移反演路径，先用独立小矩阵参考、重根、取消、有限迭代失败与禁止稠密化测试；只有验证通过才注册能力。
4. 保留子空间热启动，另为受控固定工作位移块算法实现完整下一轮状态恢复；输入/配置/源码/实际已加载代码/浮点环境不兼容时拒绝。
5. 运行数值与后端完整测试，记录准确范围、未实现条件和复现命令。

协议对象在准备阶段持有原 CSR、工作 CSR 与置换，求解阶段产出候选，监控返回可序列化状态，收尾只返回原始顺序的向量。运行入口继续独立检查、保存数组并重新读盘验证，最后发布清单。

## 已落地的接口与配置

| 接口或配置 | 行为与边界 |
|---|---|
| `create_backend(config)` | 返回符合 `SolverBackend` 协议的对象，按 `prepare(matrix)`、`solve()`、`monitor()`、`finalize()` 调用；错误阶段调用返回 `BACKEND_LIFECYCLE` |
| `algorithm_plan(config, matrix_statistics=None)` | 输出实际支持的后端、精度、线性求解、残差检查频率、锁定方式、检查点能力、完整冻结的回退配置序列及来源；提供已验证的 `is_diagonal=true` 统计时预告解析对角路径 |
| `estimate_resources(matrix_or_statistics, config)` | 接受稀疏矩阵或包含 `n/nnz/dtype/storage_bytes/is_diagonal` 的统计；逐项列出原输入与恢复副本、工作 CSR、CSR/CSC 转换、尺度归一化、置换、子空间、候选、验证与有限日志预算 |
| `problem.reordering` | `none` 或 `reverse_cuthill_mckee`；保存 `permutation.npy`，定义为工作第 j 行对应原始第 `permutation[j]` 行；输出向量恢复原始行序后才进行最终验证 |
| `problem.nullspace` | `basis_indices` 为不超过 512 个唯一非负原始坐标，`interpretation` 为 `unspecified/expected_nullspace`，`source` 为不超过 256 字符的来源说明；仅验证所声明坐标基是否确实处于矩阵零空间，不删除任何模式 |
| `solver.method` | 默认 `arpack`；`auto` 确定选择 `arpack`；新增 `block_shift_invert` 支持标准实对称、复厄密 CPU 最近 k 个模式 |
| `solver.method=jacobi_davidson` | 标准实对称、复厄密 CPU 最近 k 个模式；必须配套 `transform=precond`、`linear.method=gmres`、`linear.preconditioner=none/jacobi`，不能继承精确位移反演配置 |
| `solver.block` | `oversampling=4`、`residual_check_interval=1`、`max_restarts=2`、`stagnation_checks=20`、`checkpoint_interval=0`；`checkpoint_kind` 默认 `subspace`，受控组合可选 `exact_block` |
| `solver.linear.adaptive_accuracy` | `enabled=false`、`rtol_min=1e-10`、`rtol_max=1e-2`、`forcing_factor=0.1`；仅 Jacobi–Davidson 投影修正方程支持，精确逆路径明确拒绝启用 |
| `solver.linear.precision` | 默认 `float64`；`mixed_float32` 只允许 JD 的近似修正方程，必须通过真实 64 位修正方程残差复验，否则提升 64 位重算 |
| `observability.enabled` | 严格布尔值，默认 true；false 只抑制次要观测事件，取消、资源守卫、原问题校验和权威阶段事件继续执行 |
| `solver.fallback_profiles` | 默认空；唯一已验证值为块算法的 `['block_to_arpack_v1']`，最多执行一次 ARPACK 回退，目标、验收阈值与资源预算不变 |
| `solver.adaptive_shift.policy` | 默认 `observed_ritz_gap_v2`；观测精化后的 Ritz 间隙、线性残差和失败码后动态生成下一工作位移；显式 `feedback_bounded_v1` 保留旧实验的尺度序列 |
| `run_solver(..., initial_subspace_path=...)` | 关键词参数，块算法与 Jacobi–Davidson 接受安全加载的子空间快照，用于新任务热启动；工作进程也支持其内部任务参数 |
| `run_solver(..., resume_checkpoint_path=...)` | 与热启动互斥；恢复固定工作位移 SciPy 块算法完整状态，重新分解 LU 并如实计费；不支持 ARPACK/JD 精确状态 |
| `solver.backend=slepc` | 显式本地镜像/Socket 且实际自检通过后，支持标准厄密最近 k、Krylov–Schur、MUMPS 稀疏 LU、实/复数和 MPI 1/2 秩；候选仍由主工作进程独立验证 |
| `resources.device=gpu` | 仅实际可运行的 MLX/Apple Metal JD `mixed_float32` 修正稀疏乘；外层与权威复核仍 CPU64，实际 GPU 调用为0时记录 CPU 执行及原因 |

内存区间采用 2 倍至 20 倍稀疏存储的分解填充情景，**不是可证明的上界，也不是实际峰值**。最小常驻缓冲估计明显超预算时先拒绝，数值核心每 50ms 至多采集一次整个进程树内存；取消与时间检查每次执行，父进程继续监督整个作业。原矩阵、重排矩阵与位移矩阵全程保留稀疏形式；受限向量块和小投影矩阵使用稠密存储。

`inspect_matrix` 新增 `normalization` 与最多 64 个柱的 `row_nnz_histogram`。归一化记录原始已存储项数、原始显式零、重复合并、合并后零项移除、三角恢复前后项数及声明格式；NumPy 稠密输入同时记录原始总元素数。非法结构错误定位到 `indices[i]`、`indptr[i]`、`row[i]` 或 `col[i]`，不回显矩阵数值。

SciPy 的对角输入会走解析选择，无论请求的是 ARPACK、auto、block 或 JD，策略均保留 `requested_method`，实际记录为 `actual_method=structural_diagonal`、`implementation.candidate_solver=analytic_diagonal_selection`。实际谱变换为 `none`，线性求解与预条件器为 `not_used`，外/内迭代、分解、线性解次数为真实的 0；位移反馈、混合精度、迭代残差历史及子空间续算均标记未执行。准备阶段基于实际 CSR 重新判定结构，预备事件、返回值与保存的策略一致。SLEPc 显式后端使用其真实求解路径，不借对角快捷路径证明 MPI 工作。不能使用解析对角样例证明块迭代、JD 或预处理性能。

## 块算法与原问题保证

块位移反演属于本项目实现，底层使用 SciPy SuperLU 稀疏分解、NumPy 的两次缩减 QR（将向量块变为正交基）和 Rayleigh–Ritz 小矩阵投影（在候选子空间中更新特征对）。每次完整残差检查重新计算未改变的原问题，只有残差与正交性合格的候选才软锁定。软锁定向量保留在子空间中；投影旋转后仍会复验并可解锁。检查频率大于一次时，未检查旋转后的向量不会沿用过期锁定标记。达到停滞阈值时保留请求的 Ritz 向量，只刷新超采样列，并受最大重启数约束。

工作位移尝试有上限，因子奇异或线性残差不合格时使用尺度相关的正负扰动，始终围绕原请求位移筛选输出。ARPACK 与块位移反演的线性逆作用保持双精度精确稀疏 LU 与独立线性残差检查，拒绝随意放宽内层精度。块算法记录实际外层迭代、向量乘、线性解、软锁定、重启、残差检查次数及成本；ARPACK 不可观测的外层迭代和完整向量乘次数继续为 null 并解释原因。`matrix_vector_products` 明确只覆盖候选迭代及其残差检查，不包含末态独立验证；全流程总次数保留为不可用。

默认 v2 控制器位于 `shift_policy.py`：首次尝试原位移，后续只使用已经观测到的至多 8 个局部 Ritz 值间隙、数值线性失败与原矩阵尺度，选择有限、互异且受原信赖半径约束的工作位移。它不接收矩阵名称或参考答案。ARPACK 在簇内精化后、有效性筛选前提供反馈；块算法在实际 Ritz 更新后提供反馈。每次尝试保存 `shift_decision`，记录控制器版本、触发原因、观测数、间隙和失败数；精化间隙只是下一尝试的策略输入，不是目标完整性证据。缩放工作位移换回原始单位后若溢出，会在分解前以 `NUMERICAL_SCALE_LIMIT` 拒绝本次尝试。显式旧 v1 忽略新反馈以保留原有冻结实验语义。JD 的 `shift_policy.applies=false`，不能将其没有执行的位移重试开关当作有效消融。

Jacobi–Davidson（在候选向量正交补中求改进方向的特征值迭代）是新增的独立路径，实现在 `emsolver/numerics/correction.py`。令 X 为当前所选正交 Ritz 向量，P 为 `I−XXᴴ`，使用线性算子计算 `P(A−θI)P t + XXᴴt = −r`，不形成 n×n 投影矩阵。以 GMRES（允许求解不定实数与复数线性系统的广义最小残差迭代）近似求修正量，并独立重新计算修正方程的真实残差。内层达到上限或真实残差不合格时停止该尝试，不能因库返回候选就锁定。

启用自适应精度后，线性相对容差取 `clip(forcing_factor × sqrt(outer_residual / initial_outer_residual), rtol_min, rtol_max)`，并与此前容差取最小值，随进展单调收紧。关闭后使用 `linear.rtol_initial` 固定容差。这条策略只作用于修正方程，不改变 ARPACK 的逆算子。GMRES 使用 `callback_type='legacy'`，使配置中的 `max_iterations` 实际限制每个修正方程的内层步数，另由总外层次数、时间与内存守卫控制总预算。复杂谱形不保证收敛或完整最近性。

JD 可选择 `jacobi`：对原尺度归一化矩阵的主对角绝对值设置与矩阵无穷范数、当前浮点精度相关的正下限，取倒数，并对预条件作用再次作 Ritz 子空间正交投影。这样零或负对角不会导致除零，也不把不定问题宣称为正定；该预条件器只供 GMRES 使用。

JD 的 `mixed_float32` 路径会真正以 float32/complex64 稀疏矩阵、投影向量和 GMRES 求近似修正，外层 Ritz、锁定与最终原问题验证始终为 float64/complex128。每次单精度求解后，重新使用原 64 位修正算子计算真实残差；不收敛、非有限或残差不合格时，以已有修正为初值提升到 64 位重算。当强迫容差小于 `64×float32 epsilon` 或右端在单精度中消失时，直接选择 64 位。提升后仍不合格则失败，不能放宽最终验收。记录实际两种精度求解次数、提升数、单精度失败数和预条件应用数；这只是有明确提升边界的近似修正策略，不将单精度包装为精确逆作用。

块算法最多返回受配置控制的候选，不能凭收敛数量证明一般稀疏矩阵最近目标完整。对角与受限三对角结构继续使用原有独立认证；一般非三对角结构新增 [有界精确有理惯性认证](bounded-sparse-certification.md)，上限64阶/4096存储项，并限制填充、位长、更新次数、计数次数与总时间。它独立计数原始 IEEE 矩阵的谱，再以精确残差区间、无其他根的间隔及 Gram 秩证明排除错邻域、漏模式和错向量。超出预算、端点/子空间无法证明时继续为 `PARTIAL/UNVERIFIED`。重排后认证始终使用原始未置换输入。

回退序列在计划阶段冻结为块算法与 ARPACK 的完整合法组合。只有块算法合格数量不足时才执行一次；记录触发原因、每次工作位移、方法、成本与全部尝试。资源、超时、取消与非法配置不触发抢救回退。若回退也不足则保留合格数量较多的一组，目标未认证仍不能显示成功。

## 快照、产物与可观测性

新增 `strategy.json`、`memory-estimate.json`、`diagnostics.json` 与 `permutation.npy`；块算法还写入 `subspace.checkpoint.npz`。返回结果也包含 `strategy`、`memory_estimate`、`diagnostics`。所有新产物受清单哈希保护，发布前再次读取原输入与已保存结果验证。

默认子空间快照保存原始行序的有限正交基、原输入/规范化矩阵/配置摘要、NumPy/SciPy/适配器版本和 `exact_resume=false`。块算法与 JD 可周期及末态写入 `subspace.checkpoint.npz`；旧0.4快照只缺新增默认 `checkpoint_kind=subspace` 时保持严格兼容，不允许省略其他策略字段。

新增 [完整块状态恢复](exact-block-checkpoint.md) 使用独立文件 `exact-block.checkpoint.npz`：仅 SciPy CPU 固定工作位移、无回退的块算法，可恢复下一轮实际基、锁、最优候选、停滞、RNG、累计计数与已消耗时间；绑定输入、配置、排列、源码、已加载关键函数代码及 Python/NumPy/SciPy/BLAS 环境。真实进程在保存后突然退出，再由工作进程恢复的测试与未中断结果逐位相同。恢复会新做 LU 并增加实际计数，不重置时间/外层预算。服务预检先核对数组头并流式摘要，工作进程再完整验证。ARPACK/JD/SLEPc 精确状态恢复仍不支持；其余路径不冒称精确续算。

磁盘预算包括完整内部子空间及原子替换时的旧/新副本，不能只按最终接受的 k 个向量估算。快照写入前检查目录已用体积及新 ZIP 上界，写完再按实际文件复核。导出结束和清单临时文件写入后再次核验实际目录体积，超限不发布 `manifest.json`。独立审查指出的 2000 阶、k=1、超采样 128、约 1.5MiB 磁盘预算反例，以及导出回调注入 4MiB 意外文件的反例，均已先红后绿修复。

核心内存同时受 4096 个事件和 8MiB 序列化事件字节限制，预算留出 32MiB Python 事件对象存储情景；启用观测时回调逐条发送事件。关闭观测仅抑制 `linear_progress/block_residual_checked/modes_soft_locked/block_restarted/inner_tolerance_updated/inner_progress` 等次要事件，资源守卫及残差计算照常进行，阶段、位移失败、回退、检查点、独立校验与导出事件保留。结果记录观测开关、抑制数、发出数、保留数和丢弃数，以及内存采样间隔、资源检查与事件发出成本。后端不伪造未提供的 ARPACK 外层进展曲线，也不把阶段事件绘制为数值残差。

块算法与 JD 的真实残差事件还提供当前 `locked_indices`、`stalled_checks` 与 `stagnation_threshold`。锁索引指当前 Ritz 候选排序，不是跨迭代不变的物理模式标识；块算法包含内部超采样候选，JD 为当前所选块。停滞由真实残差未取得 10% 改进的连续检查次数定义；达到配置阈值时才触发有界停滞重启。JD 子空间容量不足的重启另记为 `subspace_capacity`，不会冒称停滞。重启原因同时置于顶层 `restart_reason` 和 `metrics.reason`。

## 验证与剩余边界

先红后绿已观察到：接口缺失、配置拒绝、归一化统计缺失、回退未安装、独立后端 k 边界缺失与监控重复采样均在实现前真实失败。运行命令：

```bash
.venv-emsolver/bin/python -m pytest tests/test_numerics_certification.py tests/test_numerics_exact_resume.py tests/test_backend_protocol.py tests/test_numerics.py tests/test_contracts.py tests/test_recovery.py tests/test_shift_policy.py tests/test_plan_actual_strategy.py tests/test_gpu_metal.py -q
```

0.4基线数值及配置专项曾有138项通过；以下2500阶/JD工作进程记录均是该阶段的历史实测，不能替代本版回归。v0.5 增加一般稀疏认证正反例、完整状态/突然退出/跨进程逐位恢复、流式预检、升级窗口已加载代码绑定和 Metal 预算守卫。当前命令、环境和准确测试数量集中记录于 [本版数值证据](evidence/v050-numerical-summary.json)，Metal 实测范围另见 [Metal profile](metal-profile.md)。小型独立稠密参考只存在于受限测试样本，不进入生产求解路径。

冻结前的五项边界回归先观测失败再最小修复：计划使用已验证且包含结构信息的矩阵诊断估算；`auto` 仅映射到已支持的 ARPACK 能力门禁；计划与执行共用 70% 安全内存余量；标准问题直接求解入口拒绝未使用的 `matrix_b_id`；对角解析路径不预留未使用的 JD 内层、混精度或预条件缓冲。相关本机服务集成专项 103 项通过；Linux amd64 翻译容器专项 79 项通过，1 项因 Rosetta 的不可核验解释器身份明确跳过，安全拒绝和原生 Linux 待验边界记录于 [恢复平台证据](evidence/v040-recovery-platform.json)。

独立工作进程另以 2500 阶复厄密三对角输入、k=6、原位移 0、1 线程、RCM 重排执行首次与热启动两次；两次工作进程退出码均为 0，原输入残差验证与产物完整性均 `PASSED`。首次 55 次外层迭代，最大缩放残差 `8.715741507818015e-09`，55 个实际残差事件；热启动 1 次外层迭代，最大缩放残差 `4.933724651120147e-09`，1 个实际残差事件。工作位移与原位移字段均穿过工作进程边界。两次均诚实保持 `PARTIAL/UNVERIFIED`：现有独立认证的模式子空间夹角上界未通过，不能把默认残差阈值已通过误称为完整目标认证。

原始进程记录保存在本机临时目录 `/var/folders/rh/36crg_tn3r3gnv0s_w9376_80000gn/T/emsolver-numerical-completion-ltnc2txz/records.json`，并非跨主机交付证据。上述迭代数只适用于这一次可复现样例，不构成其他矩阵的性能或收敛保证。

第二轮另以独立工作进程实跑 60 阶复厄密 Jacobi–Davidson，k=2，原位移 0.15，1 线程，残差验收仍为默认值：8 次外层迭代、1549 次内层步数（每个修正方程上限 500）、4 次周期快照，实际内层容差由 0.01 收紧至 0.00020275619651339497。数值及产物完整性为 `PASSED`，目标完整性仍保守为 `UNVERIFIED/PARTIAL`。原始记录在 `/var/folders/rh/36crg_tn3r3gnv0s_w9376_80000gn/T/emsolver-jd-worker-mki_w45c/record.json`。

第三轮独立工作进程实跑 40 阶复厄密 JD/Jacobi/mixed_float32：8 次外层、169 次内层迭代、14 次实际单精度修正、200 次预条件应用，本例不需要提升64位；原问题最大缩放残差 `5.468884258424717e-11`，数值与哈希均通过，目标仍为 `PARTIAL`。该样本与前一阶段阶数不同，不用于直接性能比较。原记录在 `/var/folders/rh/36crg_tn3r3gnv0s_w9376_80000gn/T/emsolver-jd-mixed-worker-pf7r7_bv/record.json`。

方法与接口依据：[Jacobi–Davidson 原作者论文](https://webspace.science.uu.nl/~sleij101/Preprints/856r.pdf)、[SciPy GMRES 官方说明](https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.linalg.gmres.html)，并与已安装 SciPy 的 `gmres.__doc__` 及真实内层限步回归交叉验证。自适应容差公式为本项目明确记录的有界策略，不声称该公式对所有谱形都保证收敛。

| 尚未完成或未验证 | 准确边界 |
|---|---|
| 广义问题、LOBPCG及未验证扩展 | 仍明确拒绝，安装探测不等于可运行支持；SLEPc/MPI/Metal仅开放已自检的特定组合 |
| 一般稀疏完整目标认证 | 新增64阶以内有界精确路径；更大或预算/角度/端点证明不闭合时维持未认证 |
| 扩展混合精度范围 | 仅 JD 近似修正支持 float32/complex64 并有64位复核与提升；ARPACK/精确逆及其他精度组合仍拒绝 |
| 硬锁定及完整簇自动扩容 | 当前为残差复验软锁定；候选预算之外的完整重根簇无法承诺收集，最终完整性检查仍拒绝缺失 |
| 任意已知零空间或物理伪模态识别 | 目前只接受有限坐标基声明，不接收任意大型基矩阵，也不自动推断物理真伪 |
| 精确续算 | 仅固定工作位移、无回退的 SciPy 块算法完整外层状态；ARPACK/JD/SLEPc不支持精确状态恢复 |
| 外层全流程向量乘总次数 | 内部验证与认证未统一计数；报告已观测计数范围与不可用总数 |
| 任意规模与性能保证 | 本轮主要证明正确性与边界；不把小样本、结构对角或三对角证据外推为所有十万阶输入的性能 |
