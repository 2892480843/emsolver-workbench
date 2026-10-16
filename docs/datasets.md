# 数据来源与验证边界

> **当前状态索引（2026-10-16）**：本页包含分阶段的历史获取与拒绝记录。GSM 后续已合法取得，完整矩阵已产生数值合格候选，严格目标覆盖和物理频率仍未认证；当前结论以 [v0.9 交付](v090-delivery.md)、[工程来源审计](industrial-source-evidence-v090.md)和[参赛说明](submission/作品方案介绍.md)为准。下文“本轮未获取”“尚无正向结果”等表述仅指其所在的早期阶段，不代表当前状态。

求解器不读取参考特征值。矩阵与参考结果分别管理；`scripts/evaluate.py`仅在求解结束后运行。合成数据与公开研究矩阵的局部数值验证分别登记，均不能替代工业电磁器件验收。

## 可运行的合成套件

`emsolver sample`提供不定三对角、聚集谱、重根、近奇异和复厄密五类数据，保存种子和生成器版本。生成器源码采用MIT许可证，本项目生成的纯数值样例按CC0-1.0开放。`benchmarks/required.yaml`固定用例、重复次数与内存不足反例。十万阶记录和原始指标见`numerical-implementation.md`。

## 公开电磁数据候选

| 数据 | 来源确认 | 用途与限制 |
|---|---|---|
| Dziekonski/gsm_106857 | [官方矩阵页](https://sparse.tamu.edu/Dziekonski/gsm_106857)：589446阶、21758924非零元、实对称、非正定，来源为微波梳状滤波器有限元分析 | 可作为大型真实电磁压力测试。不是十万阶合成数据的替代名称；直接稀疏LU的填充可能超过本机16GiB预算。一般稀疏目标只有残差合格时仍标记未完整认证。 |
| CEMW/vfem | [官方矩阵页](https://sparse.tamu.edu/CEMW/vfem)：93476阶复数电磁问题 | 作为非厄密兼容边界候选，必须经过导入诊断，不能因为来源是电磁问题就跳过厄密性检查。 |
| Lee/fem_filter | [官方矩阵页](https://sparse.tamu.edu/Lee/fem_filter)：74062阶、1731206非零元、500MHz有限元微波带通滤波器 | **已实际取得并验证拒绝**。原始复非对称矩阵的相对无穷范数厄密误差为 `2.33237784e-4`，超过默认 `1e-12`。未编辑系数或强行对称化。 |
| Bai/dwg961b | [官方矩阵页](https://sparse.tamu.edu/Bai/dwg961b)：961阶、10591非零元、色散波导结构 | **已实际取得并验证拒绝**。文件头为 `complex symmetric`，复对称不等于厄密，相对无穷范数厄密误差为 `1.99984013`。未将单独 B 矩阵当作原始广义问题。 |

[集合官方许可说明](https://sparse.tamu.edu/about)声明矩阵采用CC-BY-4.0，要求保留数据和原作者引文。每个矩阵页及集合许可均为一手来源；不是两个独立机构的交叉证据。下载文件后还必须核对原始Matrix Market头部、维度及本地哈希，网页描述不能代替文件验证。

2026-10-16 复查确认，当前矩阵页的 Heroku 下载链接对 `gsm_106857` 返回 `301`，目标为 **HTTP** 的 `sparse-files` 地址；仅允许 HTTPS 的获取会拒绝该降级。直接访问其 HTTPS 等价地址仍连接超时。原官方 [UF 镜像](https://www.cise.ufl.edu/research/sparse/matrices/Lee/fem_filter.html)的 HTTPS 下载可正常校验 TLS；滤波器和小波导原包已由此取得。`gsm_106857` 的镜像响应声明压缩包为 `152679272` 字节，超过本轮 100MiB 下载预算，因此未获取。

程序现有获取脚本仍使用 `sparse-files` 主机；该主机在本机网络中未恢复。以下命令展示指定数据与预算的脚本接口，不表示这条网络路径本轮已下载成功：

```bash
.venv-emsolver/bin/python scripts/fetch_dataset.py gsm_106857 --output data/public/gsm_106857
.venv-emsolver/bin/emsolver inspect --matrix data/public/gsm_106857/gsm_106857.npz --json
.venv-emsolver/bin/emsolver matrix import --project PROJECT_ID --matrix data/public/gsm_106857/gsm_106857.npz --metadata data/public/gsm_106857/import-metadata.json --json
```

脚本仅转换指定常规矩阵成员，不解压归档路径、不进行对称化或裁剪、不生成参考答案。原始压缩包、Matrix Market文件和来源说明均保留。若用户另行提供数据，必须登记来源、用途、单位和公开权限；默认按私有数据处理。

## 已完成的真实微波输入拒绝验证

两份实际下载文件的原始头部、压缩包和原文件 SHA-256、官方链接、许可依据、维度交叉核对与运行配置登记在 [v050-public-datasets.json](evidence/v050-public-datasets.json)。`load_matrix` 与 `run_solver` 均实际返回 `NOT_HERMITIAN`，未发布特征值、向量或成功清单。复厄密检查使用原矩阵的 `||A-Aᴴ||∞ / ||A||∞`，其中 `Aᴴ` 表示共轭转置。

本次转换与两次拒绝合计约 1.75 秒，外部每 100ms 轮询 RSS（进程实际占用内存），观测峰值约 254MB，预算为 45 秒和 1GiB；这是软监视边界。求解配置为单线程、1GiB 内存、30秒墙钟时间。原始档案只复制预期常规文件，不按归档路径解压；仅做 Matrix Market 声明存储的格式重建与 CSR（按行压缩的稀疏存储）转换，没有取实部、平均对称化或裁剪。

小波导的未修改原压缩包与原文件随 [examples/public-em/Bai_dwg961b](../examples/public-em/Bai_dwg961b/README.md) 提供，附完整作者归属、CC-BY-4.0 链接和哈希。约27MB的滤波器原包保留在本地忽略目录 `artifacts/public-dataset-v050/Lee_fem_filter/`，公开登记其来源与哈希。两项证明真实电磁数据的合法来源登记与不支持问题的正确拒绝，**尚无真实厄密微波矩阵的正向原问题残差验收，不能作为工业性能成绩或完整谱认证**。

## 参考评估

```bash
.venv-emsolver/bin/python scripts/evaluate.py --help
```

评估器进行一对一特征值匹配与重根子空间比较；主求解器不导入评估器。`scripts/run_isolated.py` 另提供已实跑的 Linux 禁网、只读文件系统及精确挂载隔离；普通本机进程仍不具备该容器边界。没有独立参考或谱计数证据时，界面和报告保留“未认证”。

## 已完成的公开研究矩阵局部验证

2026-10-16 从 [NIST 官方矩阵页](https://math.nist.gov/MatrixMarket/data/NEP/mhd/mhd1280b.html)及其[集合说明](https://math.nist.gov/MatrixMarket/data/NEP/mhd/mhd.html)定位并实际取得 `mhd1280b.mtx.gz`。两页同属一个机构，不构成独立双来源。压缩文件 SHA-256 为 `286c9b340aa55140f2956f1ed279eb23d26139812263d5429881cfd74959d7b5`；本地 Matrix Market 头部确认为 1280 阶复厄密，三角存储12029项，厄密重建后22778非零元。仅做格式转换与厄密存储重建，没有编辑系数。

这次将广义磁流体问题的 B 矩阵单独作为标准厄密输入求解，并非解原始广义问题，也不是微波器件算例。6个候选在原始输入上的双精度数值验证通过，最大缩放残差约 `7.51e-17`，正交误差约 `8.97e-16`；缺少独立目标计数证据，结果保持 `PARTIAL/TARGET_UNVERIFIED`。局部运行原始证据保存在私有 `artifacts/public-dataset-check/`，不能推广为工业性能或目标谱完整认证。

该文件的再分发许可未充分确认，输入与向量均未纳入公开源码包或离线包；不能套用其他集合的许可。该项只证明真实研究数据经过本地导入、计算和原问题复验的有限流程。
