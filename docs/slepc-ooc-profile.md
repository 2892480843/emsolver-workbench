# 显式 MUMPS 磁盘外对称不定分解

## 作用与边界

`mumps_ooc_symmetric_v1` 是 SLEPc（稀疏特征值计算库）的一个显式 CPU 求解配置。它让 MUMPS（稀疏直接分解库）把部分因子写入私有磁盘目录，以降低分解工作内存需求。原先默认的内存内 LU 配置保持不变。

该配置使用实对称不定 `LDLᵀ` 分解。不定表示矩阵允许同时存在正、负特征值；`LDLᵀ` 保留这一结构。PETSc 3.18.5 使用的接口名称为 `PCCHOLESKY`，但实矩阵明确设置 `SPD=false` 后，官方 MUMPS 适配器选择 `sym=2`，并没有假设矩阵正定。

**后端只返回候选结果。**宿主仍重新计算原始矩阵上的残差、正交性及独立目标完整性。后端误差估计、MUMPS 浮点惯性数和磁盘外分解成功均不能单独产生 `CERTIFIED`。

## 支持范围与能力门槛

仅支持以下组合：

| 参数 | 要求 |
|---|---|
| `solver.backend` | `slepc` |
| `solver.method` | `krylovschur` |
| `solver.transform` | `shift_invert` |
| `solver.linear.method` | `sparse_lu` |
| `solver.linear.preconditioner` | `mumps_ooc_symmetric_v1` |
| `solver.linear.precision` | `float64` |
| 矩阵 | 实数、标准厄密问题；实际复数数组即使虚部为零也拒绝此配置 |
| 设备、MPI 进程、线程 | CPU、1 个 MPI 进程、1 个线程 |
| 目标 | `nearest_k` |

额外的小规模实不定 OOC 自检必须通过，能力响应才在 `slepc.factor_profiles` 中列出 `mumps_ooc_symmetric_v1`。原有实/复、1/2 个 MPI 进程的四项内存内自检仍执行。如果仅 OOC 自检失败，原有后端仍可用，但不会发布 OOC 能力，计划与实际执行都会拒绝未经验证的 OOC 路线。

工作进程只能复用服务端冻结的完整能力证明；不会在作业中悄悄重新开展全局自检，也不能由前端自行添加能力声明。

## 资源与数据结构

单个作业的内存预算仍按既有策略分为宿主和容器两份。MUMPS 数值工作区按固定公式计算：

```text
workspace_MiB = min(768, floor(container_bytes × 0.35 / 2²⁰))
```

小于 64 MiB 时提前报告 `RESOURCE_EXHAUSTED`。该参数是 MUMPS 的数值工作区控制，**不是容器总内存上界**；输入、符号分析、三元组、特征向量和库开销仍占内存，容器总内存与禁止额外交换的硬限制继续生效。

单进程配置通过 `createAIJWithArrays` 借用规范的 CSR（压缩稀疏行存储）缓冲区，避免逐行 Python 插入大量非零元。数组必须具有正确类型、连续布局、可写权限，且一直存活到 PETSc 矩阵销毁之后。只读映射会先复制；不会把只读内存直接交给可能写入的 C 库。

磁盘外文件固定写到 `/output/ooc`，用户不能传入额外宿主路径。它位于已有私有工作目录中，与快照、日志和候选数组共同受宿主磁盘预算监控；取消、超时和退出沿用已有容器删除与目录清理。磁盘监控是周期采样，不是文件系统硬配额，可能存在采样间隔内的短暂超量，因此不将它描述成严格磁盘隔离。容器依然没有网络、采用只读根目录并使用作业截止守护。

## 最小配置示例

```yaml
solver:
  backend: slepc
  method: krylovschur
  transform: shift_invert
  subspace_max: 16
  linear:
    method: sparse_lu
    precision: float64
    preconditioner: mumps_ooc_symmetric_v1
target:
  mode: nearest_k
  k: 2
  sigma: 0.0
resources:
  device: cpu
  mpi_ranks: 1
  threads: 1
  memory_limit_gib: 4
  disk_limit_gib: 4
  walltime_seconds: 180
```

示例总预算 4 GiB 时，容器份额 2 GiB、MUMPS 工作区 716 MiB。磁盘需求依赖填充程度，不能仅凭输入的非零元数量保证成功；计划应保留未知磁盘开销提示，实际守护负责终止超量作业。

## 诊断与失败记录

销毁因子前记录以下数值观测：

- `factor_profile`、`mumps_symmetric_mode`、`factorization`、`assumes_positive_definite`。
- 实际 `mumps_icntl` 的 22、23 项，验证磁盘外模式与工作区确实生效。
- `mumps_infog` 的错误码、负主元数、内存及因子信息；这些值是后端浮点观测。
- `ooc_files_before_factor_destroy` 与 `ooc_bytes_before_factor_destroy`。
- `ooc_inertia_is_certificate=false`，避免将后端惯性观测误作严格完整性证据。

驱动能够返回数值诊断时，写出有大小限制、无原始异常路径文本的 `failure.json`。宿主仅接受白名单字段与整数数据，将已知工作区错误映射为 `RESOURCE_EXHAUSTED`、已知结构/数值零主元映射为 `SLEPC_FACTORIZATION_FAILED`。其他错误保留为后端失败，不猜测其含义。失败证明保存为作业内的 `slepc.failure.json` 后再清理私有容器目录。

若容器被内核终止，可能来不及写出驱动诊断；宿主仍根据容器 OOM 状态、作业超时或磁盘守护报告相应状态，不伪造 MUMPS 错误码。

## 验证记录

实现前红测已观察到 5 项预期失败；完成配置、规范、借用数组及失败协议后，新旧本地合同测试为 **40 项通过，20 项需要真实镜像的测试跳过**。日志为 `artifacts/v090-solver-research/ooc-contract-tests-final.log`。

真实 OOC 集成测试显式设置 `EMSOLVER_TEST_SLEPC_OOC=1` 并使用重建且自检通过的镜像 `emsolver-slepc:bookworm-arm64-v090`，镜像 ID 为 `sha256:586b521134202f7e709ddf9c2353b7f85906dac22cc7e4685686acb0421ef592`。新 OOC、旧实/复 1/2 个 MPI 进程、驱动/宿主协议、Service 取消与外部进程清理等五个测试文件共 **61 项通过、0 跳过、0 失败，14.45 秒**。其中包括小实不定矩阵的原问题残差与独立小规模参考、实际参数和销毁前诊断、精确奇异目标的结构化失败、取消、磁盘预算，以及复矩阵拒绝。记录为 `artifacts/v090-solver-research/ooc-actual-validation.json` 与 `ooc-actual-tests.log`。这些实际小规模测试不代替完整工业输入验收。

## 官方依据

- [PETSc 3.18.5 MUMPS 适配器源码](https://gitlab.com/petsc/petsc/-/raw/v3.18.5/src/mat/impls/aij/mpi/mumps/mumps.c)：`MatGetFactor_aij_mumps` 的 `MAT_FACTOR_CHOLESKY` 分支在实矩阵 `SPD` 非真时选择 `sym=2`；`MatFactorNumeric_MUMPS` 区分数值零主元与工作区错误。
- [PETSc 的 MUMPS 控制项](https://petsc.org/release/manualpages/Mat/MATSOLVERMUMPS/)：磁盘外模式、工作区和暂存目录选项。
- [PETSc 借用 CSR 数组接口](https://petsc.org/release/manualpages/Mat/MatCreateSeqAIJWithArrays/)：单进程、行内有序索引、缓冲区生命周期要求。
- [petsc4py Options 接口](https://petsc.org/release/petsc4py/reference/petsc4py.PETSc.Options.html)：设置、读取与清理参数数据库。
