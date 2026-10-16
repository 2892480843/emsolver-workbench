# 预算内均衡补偿 ILU v2

`ilu_equilibrated_compensated_v2` 是显式选择的 CPU 双精度预条件策略，用于改善 Jacobi–Davidson（通过修正方程逐步寻找特征对）的内层 GMRES。ILU（不完全 LU 分解，用有限存储近似求逆）只改变迭代的辅助算子；目标特征值、原矩阵和最终原单位残差检查保持原问题定义。旧策略 `ilu_budgeted_v1` 和默认配置均不改变。

## 失败根因与独立复现

**`spilu` 报 `Factor is exactly singular` 不能推出原移位矩阵奇异。** 丢弃小元素后的不完全消元可能出现零主元，而完整消元能够成功。SciPy 的包装直接调用 SuperLU 的 `gsitrf`；其正 `info <= n` 被转换为上述异常。SuperLU 不完全分解内部即使替换零主元，仍可返回正 `info`。这与完整矩阵的最小奇异值是两回事。[SciPy 包装源码](https://github.com/scipy/scipy/blob/v1.18.1/scipy/sparse/linalg/_dsolve/_superluobject.c)、[SuperLU 不完全分解源码](https://github.com/xiaoyeli/superlu/blob/master/SRC/dgsitrf.c)、[SuperLU 主元替换源码](https://github.com/xiaoyeli/superlu/blob/master/SRC/ilu_dpivotL.c)。

独立生成的零对角、尺度不均衡对称矩阵经逐点删除，得到一个 33 阶反例：移位后 193 个非零元素，最小奇异值约 `1.5413e-8`，二范数条件数约 `6.5524e7`；完整稀疏 LU 的原方程相对残差约 `2.0266e-11`，固定 v1 分解仍失败。这是逐点删除意义下不可再缩小的主子矩阵，**不声称全局最小反例**。数据、系数与检查位于 `artifacts/v080-preconditioner-research/minimal-v1-failure-reduced*` 和 `tests/test_robust_preconditioning.py`。构造不读取公开工业矩阵。

`spilu(..., options={'Equil': True})` 在该反例上没有修复失败。包装调用的是低层不完全分解，不能据驱动程序的 `Equil` 选项推断已做均衡；因此 v2 在调用前自行执行可审计的缩放。[SciPy spilu 文档](https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.linalg.spilu.html)、[SuperLU 用户指南](https://portal.nersc.gov/project/sparse/superlu/superlu_ug.pdf)。

## 固定策略与数学契约

| 项目 | 固定规则 |
|---|---|
| 选择入口 | 现有字符串 `solver.linear.preconditioner='ilu_equilibrated_compensated_v2'` |
| 支持范围 | CPU，`float64` 或 `complex128` 原矩阵，双精度 GMRES；GPU 和混合精度明确拒绝 |
| 预条件移位 | 与 v1 相同的目标移位加 `sqrt(eps)` 范数下限；仅用于预条件，不改变谱目标 |
| 均衡 | 4 次对称最大范数缩放；每次 `d_i=clip(1/sqrt(max_j abs(H_ij)),1e-100,1e100)`，零行以下溢安全下限处理 |
| 对角补偿 | `H_ii=sign(H_ii)*max(abs(H_ii),0.01*sum_j abs(H_ij))`；零对角采用正号 |
| 不完全分解 | `drop_tol=1e-3, fill_factor=2, drop_rule='basic,area', permc_spec='COLAMD'`，一次构造，整次求解复用 |
| 约束缓存 | 每个外层候选子空间构造一次，全部修正列复用；不在每次矩阵向量乘法时重新分解 |
| 拒绝条件 | 非有限值、分解失败、非正缩放、非正交候选块、奇异或条件数大于 `1e12` 的小 Schur 矩阵、资源超限或取消 |
| 重试与回退 | 无参数搜索、无填充量递增、无策略隐式替换 |

设原缩放矩阵为 `A`，预条件工作移位为 `s`，均衡后的工作矩阵为 `H=D(A-sI)D+E`，`E` 仅表示上述对角补偿。分解给出的原坐标近似逆为 `B=D ILU(H)^(-1) D`。

设正交候选块为 `Q`，`P=I-QQ*`，缓存 `Z=BQ`、`S=Q*Z`。内层使用固定线性算子

\[
M=P[B-ZS^{-1}Q^*B]P+QQ^*.
\]

该式来自带约束方程 `Kz+Q lambda=r, Q*z=0` 的消元，其中 `B` 近似 `K^-1`。它比简单的 `PBP` 保留了约束与近似逆之间的耦合。Schur（消去大变量后得到的小矩阵）先按最大实虚分量归一化，再判断条件数和 LU 分解，避免极端尺度下复数除法的中间溢出。该归一化不改变方程。推导与 Jacobi–Davidson 的投影预条件结构一致。[作者原始推导 §7.1.1](https://webspace.science.uu.nl/~sleij101/Preprints/923g.pdf)。

GMRES 的停止判据仍检查原修正算子的真实双精度残差。预条件残差小、分解成功或小 Schur 可逆都不能代替此检查。[SciPy GMRES 文档](https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.linalg.gmres.html)。完整性证书继续由独立计数和原矩阵残差、重数及子空间证明产生。

## 资源与审计边界

设 `N` 为阶数，`Z=min(N*N,nnz+N)`，`b` 为每个系数的字节数，`m=min(N,subspace_max)`。v1 既有估计继续保留：移位 CSC 临时副本、L/U 各自按 `ceil(2*Z)` 个元素估计、保守 64 位索引、符号/排列数据、64 列面板场景和求解工作向量。v2 额外预留：

| 分量 | 字节估计 |
|---|---:|
| 均衡稀疏临时数据和非零缩放向量 | `2*(Z*(b+8)+(N+1)*8)+16*Z` |
| 缩放、对角和范数向量 | `12*N*8` |
| 前后两轮 Q/Z 缓存并存 | `4*N*m*b` |
| 小 Schur 与分解工作空间 | `8*m*m*b` |
| 固定四次历史 | `4096` |

构造稀疏副本前执行完整求解预算与当前进程驻留内存检查；缓存构造前再次检查当前内存余量。预算模型使用 `subspace_max`，不能只按请求的 `k` 估计，因为完整簇可能扩展候选块。构造前后、四次均衡之间、约束列之间以及每次应用前后检查取消与资源。**SuperLU 的填充因子不是硬内存上界，必须继续由父进程实际内存和墙钟限制兜住原生库调用。** 原生分解内部不能逐行被 Python 取消。

不导出 `factor.L` 或 `factor.U`；使用 `SuperLU.nnz` 记录实际总非零量，避免为了审计额外复制大因子。计数器分别记录分解次数/时间、原始三角求解次数、两次缩放应用、约束构造与应用次数/时间、最大 Schur 条件数、四次均衡历史和实际对角补偿量。成功应用仍检查有限性。

## 独立合成实验与选择依据

冻结语料包含实不定、复厄密不定、尺度不均衡、零对角、聚簇和鞍点六族，每族 64 阶，训练种子 `0..3`、保留验证种子 `101,102`；加上独立 33 阶反例共 37 矩阵，每个矩阵 2 个原修正方程，共 74 方程。统一 `rtol=1e-6`、`restart=24`、最多 200 内迭代。近收敛诊断仅在这些小矩阵上使用独立稠密特征分解构造加 1% 扰动的候选向量；生产路径不读这些向量。

初始候选诊断中，同样的双投影形式比较得到：无预条件通过 `40/74`；v1 `19/74`（6 个分解失败）；仅四次均衡 `26/74`（2 个分解失败）；均衡加 1% 补偿 `32/74`（无分解失败）；严格对角占优补偿 `25/74`；固定复移位 `16/74`；行和 Jacobi `17/74`。说明**消除零主元并不等于修正方程好用**。选定均衡补偿后再验证约束形式：

| 固定策略 | 初始候选通过方程 | 近收敛候选通过方程 | 初始保留验证集 | 近收敛保留验证集 |
|---|---:|---:|---:|---:|
| 无预条件 | 40/74 | 26/74 | 12/24 | 8/24 |
| v1 双投影 | 19/74 | 43/74 | 7/24 | 12/24 |
| v2 均衡补偿双投影 | 32/74 | 62/74 | — | — |
| v2 均衡补偿约束逆 | 50/74 | 64/74 | 18/24 | 20/24 |

**局限：复不定族近收敛诊断只通过 `2/12`，其余五族各 `12/12`。** v2 不能保证一般不定矩阵收敛，也不能据小矩阵结果推断工业矩阵性能。尚未使用本策略试跑 GSM 公开大矩阵。所有比较保留真实原修正方程残差，未把构造成功计为通过。

固定复移位虽有 Helmholtz 场景的研究基础，但本组通用复移位结果较差，并会使实数因子变复数、增加存储，未采用。[原始论文](https://epubs.siam.org/doi/10.1137/040615195)。正则化正规方程会平方奇异值尺度，显式乘积还可能产生远多于原 `nnz` 的非零元素；本次不将其作为有界稀疏内存的替代实现。

实验脚本、冻结协议、矩阵哈希和逐方程结果：`artifacts/v080-preconditioner-research/{benchmark.py,benchmark_constrained.py,frozen-benchmark.json,frozen-constrained-diagnostic.json,corpus-manifest.json,results.json,constrained-results.json}`。这些结果属于独立合成研究，不是完整外层特征值求解或工业性能验收。

## 回归入口

```sh
.venv-emsolver/bin/python -m pytest tests/test_robust_preconditioning.py tests/test_ilu_preconditioning.py tests/test_backend_protocol.py tests/test_numerics.py -q
```

覆盖 v1 分解失败而 v2 求解同一原修正方程、独立小规模直接解对照、实/复 `1e-300..1e300` 坐标一致性、固定线性约束逆、单次分解缓存、奇异 Schur、无效候选、非有限输出、预算预拒绝、取消和不支持的设备/精度。端到端外层停止条件与独立证明路由由相应集成测试另行验证。
