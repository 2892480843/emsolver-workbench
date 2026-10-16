# v0.4.0 冻结协议实测

本次仅验证本地小规模功能可行性：非对角 `complex128` 合成 Hermitian 矩阵（矩阵等于其共轭转置），阶数 60；开发集和留出集种子分别为 `20261016`、`20261017`，每个单元重复 2 次。全部输入、配置、源码和环境在执行前冻结；用例名称及路径不选择策略。

运行环境：Darwin arm64，Python 3.12.13，NumPy 2.5.4、SciPy 1.18.1，单 CPU 线程；每次运行限制 2 GiB 内存、30 秒和 1 GiB 输出磁盘。

**规定及执行均为 36 行：8 次成功、22 次部分成功、6 次失败；超时、阻断、未运行均为 0。** 部分成功、未认证和失败均按原状态保留分母。结果不能推导通用加速、工业规模效果或全部 PRD 验收。

| 路线 / 变体 | 规定次数 | 成功 | 部分成功 | 失败 | 超时 | 阻断 / 未运行 | 数值通过 | 目标认证 | 求解秒数中位数（有指标次数） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| arpack / baseline | 4 | 4 | 0 | 0 | 0 | 0 / 0 | 4 | 4 | 0.003684（4） |
| arpack / no-adaptive-shift | 4 | 4 | 0 | 0 | 0 | 0 / 0 | 4 | 4 | 0.003604（4） |
| block / baseline | 4 | 0 | 4 | 0 | 0 | 0 / 0 | 4 | 0 | 0.010016（4） |
| block / no-adaptive-shift | 4 | 0 | 4 | 0 | 0 | 0 / 0 | 4 | 0 | 0.010189（4） |
| block / no-bounded-restart | 4 | 0 | 4 | 0 | 0 | 0 / 0 | 4 | 0 | 0.010029（4） |
| jd / baseline | 4 | 0 | 4 | 0 | 0 | 0 / 0 | 4 | 0 | 0.040669（4） |
| jd / no-jacobi | 4 | 0 | 0 | 4 | 0 | 0 / 0 | 4 | 0 | 0.229258（4） |
| jd / no-mixed-precision | 4 | 0 | 4 | 0 | 0 | 0 / 0 | 4 | 0 | 0.041096（4） |
| jd / no-bounded-restart | 4 | 0 | 2 | 2 | 0 | 0 / 0 | 4 | 0 | 0.035041（4） |

“数值通过”仅为原始验证字段计数，**空输出也可得到该字段的 `PASSED`，不能当作存在有效结果**。本次 6 个失败行均为 `NO_VALID_MODES`、`n_accepted=0`：关闭 Jacobi 的 4 行，以及开发集关闭重启的 2 行；其余 30 行各保留 3 个候选。“目标认证”另行判断是否覆盖请求的最近模式；22 个部分成功行的原始限制均为 `mode_subspace_not_verified`，没有改写成完整成功。表中秒数只展示实际记录，失败后缺失的指标不补零；Python 子进程启动、任务提交与求解阶段的时间也不混为同一口径。没有成功样本筛除或按结果调参。

## 实际适用性与触发

所有输入都经安全加载后确认 `is_diagonal=false`；冻结前算法计划和执行后 `strategy.json` 均核对实际方法，未走 `structural_diagonal`。JD（Jacobi–Davidson，通过近似修正方程改进候选子空间的方法）采用真实 GMRES 修正、Jacobi 预处理和可提升至 FP64 的 FP32 修正；关闭变体仅改相应开关，最终原问题阈值始终为 `1e-8`。

| 路线 / 变体 | 预处理调用总数 | FP32 修正总数 | FP64 修正总数 | 重启总数 | 单次运行最多尝试数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| arpack / baseline | 未记录 | 未记录 | 未记录 | 未记录 | 1 |
| arpack / no-adaptive-shift | 未记录 | 未记录 | 未记录 | 未记录 | 1 |
| block / baseline | 未记录 | 未记录 | 未记录 | 0 | 1 |
| block / no-adaptive-shift | 未记录 | 未记录 | 未记录 | 0 | 1 |
| block / no-bounded-restart | 未记录 | 未记录 | 未记录 | 0 | 1 |
| jd / baseline | 1554 | 96 | 0 | 2 | 1 |
| jd / no-jacobi | 0 | 12 | 4 | 0 | 1 |
| jd / no-mixed-precision | 1554 | 0 | 96 | 2 | 1 |
| jd / no-bounded-restart | 1476 | 90 | 0 | 0 | 1 |

调用数为 0 的分支没有在本组样例中触发，不能据此归因收益。自适应工作位移只适用于 ARPACK / block 路线；JD 的 `sigma` 是候选目标，不包含该消融。`no-bounded-restart` 保留子空间和内存硬上限，禁止理解为解除预算。

## 冻结与复查

- 源码内容树 SHA-256：`98dc55483ec40cb480fb6f0fff520a9b2dae6907c0c64919476f287504c72315`；范围为 Python 实现、前端 TS/TSX/CSS 和依赖清单，文档及证据不计入。
- 源码身份哈希：`e8a3be0a91d0d1312348cefad5d951e912ddaad60414502ce8074fccd396282c`。
- 实际运行 Python 代码哈希：`43b4ae84c277f14f8787e66c767d754767547ecf2388bd901a6a00106c75f5fb`。
- 环境哈希：`f6bec6dfedc945ef493cfa832edf63e98e877e8f54068f68680a3aa719a5c977`。

| 证据 | 协议哈希 | 证据文件 SHA-256 |
| --- | --- | --- |
| [完整 arpack 记录](evidence/v040-protocol-arpack.json) | `130132ad7518e0def6f4114a4b619bef373592d8f34152e676c3949745cca5d4` | `3f6638f580c0fc63b6dd15fc182df3530b896b417b0f3d6c431e25074ff06a72` |
| [完整 block 记录](evidence/v040-protocol-block.json) | `17944ac94e820036d936e69666e5dfe51afd8afee6c2fd5fba4efd0167f3a360` | `605e0a93d1912905f3e47b8a880f268fb5199501a06f8a061b1440df8ffa7994` |
| [完整 jd 记录](evidence/v040-protocol-jd.json) | `21d22a4268a3a2c9da5ab52b7d92c493b0ef10baa303c9013dfc7ec824b3bb60` | `ae5c60611117d4aac5eae57e9c7b9ef648f32e822237b13be7281894b74c6e2c` |

完整记录包含预先声明的规格、输入字节哈希、全部解析配置和配置哈希、每次原始状态／验证／指标、实际算法策略及发布清单哈希。执行结束后再次核对三套源码、运行代码和环境身份一致。生成器来源明确为合成数据；留出内容哈希与开发内容哈希不重叠，不表示外部机构隐藏测试。

复跑时从任一证据文件的 `specification` 提取 JSON 为规格，再使用新目录执行：

```sh
.venv-emsolver/bin/python scripts/run_protocol.py freeze --spec copied-spec.json --output artifacts/new-frozen
.venv-emsolver/bin/python scripts/run_protocol.py execute --plan artifacts/new-frozen/protocol.json --output artifacts/new-results --expected-hash PRINTED_PROTOCOL_HASH
```

未提供隔离的独立参考答案，所有行仍保留 `reference_certification=NOT_AVAILABLE`；原问题残差与结构目标认证不能冒称外部参考认证。小矩阵、两次重复、固定执行顺序及本机环境不支持统计显著的性能结论；Linux 冷启动证据由独立安装／容器验证记录提供。
