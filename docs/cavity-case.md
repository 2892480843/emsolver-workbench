# 二维 PEC 矩形区域的 TM_z 教学算例

本算例由 `scripts/generate_cavity_case.py` 在本地生成。矩阵、节点坐标、自由度映射和解析离散谱均为项目自行生成的数据，按 CC0-1.0 提供；生成器源码沿用仓库软件许可。生成过程不联网、不下载几何、不读取第三方数据。

**边界：这是二维标量电磁简化模型，不是三维工业腔体、真实器件网格、制造数据或测量验证。** 它证明输入、授权模型、求解结果、单位与实际坐标之间的数据流程，不能代替赛题工业性能指标的独立验收。

## 模型和单位

PEC（理想导体：忽略导体内部电场和损耗）的侧壁采用 `E_z=0` 边界。TM_z（横磁简化：取沿 z 的电场分量作为标量未知量）在均匀真空、z 不变或横向截止 `β_z=0` 的假设下，使用二维负拉普拉斯特征问题。此类 TM 标量势及零边界的建模依据可见 [INL MOOSE Waveguide Eigenvalue Benchmark，2022 版本](https://mooseframework.inl.gov/releases/moose/2022-06-13/modules/electromagnetics/benchmarks/WaveguideEigenvalue.html)。该网页作为理论参考，不是本算例数据来源。

矩形尺寸为 `a × b` 米，内部网格为 `nx × ny`，间距为 `hx=a/(nx+1)`、`hy=b/(ny+1)`。采用五点有限差分（用相邻节点近似二阶导数），边界零值不进入未知量。

\[
A=I_{ny}\otimes T_x+T_y\otimes I_{nx},\qquad
T_x=h_x^{-2}\operatorname{tridiag}(-1,2,-1).
\]

`T_y` 同理。矩阵单位为 `m^-2`，特征值为离散横向波数平方 `k_c²`。本模型的截止频率/二维特征频率由已明确审阅的映射

\[
f=\frac{299792458\sqrt{\lambda}}{2\pi}\ \mathrm{Hz}
\]

给出。此公式**不解释一般三维腔体频率或任意传播常数的情况**。结果向量使用二范数归一化，场单位为 `normalized E_z (arbitrary amplitude)`；其幅值不是经激励或能量标定的 V/m。

`dof=iy*nx+ix`，x 方向索引变化最快。每个自由度对应真实内部节点 `[(ix+1)hx,(iy+1)hy,0]` 的 z 分量。可选三角形只用于显示内部网格连接关系，数值算子仍是五点有限差分；图中不额外补造边界节点、全矢量分量或三维外壳。单元超过 100000 个时省略连接关系并在来源清单中说明，坐标仍完整保留。

## 生成和独立参考

```bash
python scripts/generate_cavity_case.py --output examples/generated-cavity
```

默认 `8×8=64` 个内部节点，尺寸为 `0.02×0.01 m`。也可生成更小的快速流程算例：

```bash
python scripts/generate_cavity_case.py --output examples/generated-cavity-4x4 --nx 4 --ny 4
```

输出目录必须不存在；脚本拒绝覆盖已有文件。两个网格维度均至少为 2，尺寸限于 `1e-6..1e6 m`。工程侧车的真实节点上限为 100000，因此本生成器有效限制为 `nx*ny≤100000`，同时低于数值矩阵的 200000 上限。

| 文件 | 内容与用途 |
|---|---|
| `matrix.npz` | 原始 CSR 稀疏负拉普拉斯矩阵 |
| `engineering.json` | 原矩阵 SHA-256、真实坐标、z 分量映射、单位、来源、授权与频率公式 |
| `config.json` | CPU 求解配置，冻结 `observed_ritz_gap_v2`；不引用参考目录 |
| `source-manifest.json` | 模型假设、网格参数、生成器版本/相对路径/SHA-256、各数据文件摘要 |
| `reference/eigenvalues.npy` | 全部离散解析特征值，只供求解完成后的外部比较 |
| `DATA-LICENSE.txt` | 本地生成数据的 CC0 声明及许可链接 |

离散解析谱为

\[
\lambda_{p,q}=\frac4{h_x^2}\sin^2\frac{p\pi}{2(nx+1)}+
\frac4{h_y^2}\sin^2\frac{q\pi}{2(ny+1)},
\quad 1\le p\le nx,\quad1\le q\le ny.
\]

它是给定**离散矩阵**的完整谱，保留重根；不是连续模型或实测器件谱。正弦基与 Dirichlet 差分算子的关系可对照 [MIT 18.330 数值分析讲义，Laurent Demanet，2014-04-25 草稿](https://math.mit.edu/icg/resources/teaching/18.330/18.330-Apr-2014.pdf)。测试同时用独立逐行五点模板和小矩阵直接特征分解核对生成结果，而不是仅比较同一公式的两次计算。

候选求解器只接收 `matrix.npz` 和配置。`reference/` 不加入配置、计划或工程侧车；端到端测试在求解前删除参考目录，求解结束后仍用测试持有的解析值核对。

## 工作台流程

1. 在已获授权的项目中导入 `matrix.npz`，记录许可 `CC0-1.0` 和单位 `m^-2`；明确公开需要项目管理员授权。
2. 在矩阵详情的“工程模型映射”上传 `engineering.json`，核对原始矩阵 SHA-256、来源与频率适用范围，明确确认有权使用后保存。
3. 选择该不可变模型版本，再新建配置预览和任务。旧计划与旧任务保留各自冻结的映射，不会随当前选择补绑几何。
4. 求解完成后查看授权频率，场图选择 `z` 分量及 `XY` 投影。显示的是实际坐标对应的归一化 Ez；`XZ`/`YZ` 的重叠是二维几何的真实投影。
5. 需要外部谱比较时，求解完成后单独读取 `reference/eigenvalues.npy`。不要将它加入候选运行的输入或隔离容器。

有效候选仍可能因为完整性认证的时间、精度或内存预算返回 `PARTIAL / UNVERIFIED`。实际默认 64 节点案例曾达到独立原问题残差约 `3.5e-16..4.9e-16`，同时有理惯性计数达到 5 秒预算；该次候选不能据此标为已认证。软件继续保留原判定，界面 Hz/场图也不增加认证。4×4 的完整注册、冻结、独立原问题验证、Hz 与坐标采样路径已由自动化测试实跑。

## 可复现检查

```bash
python -m pytest -q tests/test_cavity_case.py tests/test_engineering.py tests/test_engineering_backup.py
```

测试覆盖独立算子与谱、实际坐标和单位、原矩阵摘要、无参考目录的真实 CPU 求解、冻结映射与频率、错误参数和越界、既有输出拒绝覆盖，以及私有侧车的备份与恢复。生成文件使用 UTF-8 JSON，不记录宿主机绝对路径或令牌。脚本不会宣称完成工业电磁验证。
