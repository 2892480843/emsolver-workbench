# 数值核心实现与本机验收

2026年10月16日；适用版本：本仓库 `emsolver/numerics/`，macOS 26.6.2 / Darwin 25.6.0，arm64，Python 3.12.13、NumPy 2.5.4、SciPy 1.18.1。

**标准实对称及复厄密稀疏 CPU 求解闭环已实跑。对角及受限三对角结构具备独立目标认证；其余一般稀疏结构仍诚实返回 PARTIAL。** 这里的认证是本产品定义的数值验收，不表示命题方已经认可，也不证明全部任意十万阶问题都可处理。

## 范围与数值路径

- 安全输入支持 SciPy CSR/CSC/COO `.npz`、兼容字段的 NumPy 稀疏数组包以及受导入预算限制的普通数值二维 `.npy`。不允许 pickle 或对象数组。ZIP 字段、未压缩大小、NPY 头声称的分配大小、真实载荷、稀疏索引和 CSR 行指针分配均在构造前检查。
- 输入转换为排序、合并重复坐标、去除显式零的 CSR。用户声明上/下三角时采用共轭恢复，并拒绝声明以外的非零项。主对角的虚部保持到检查阶段，不偷偷取实部或对称化输入。
- 厄密偏差使用相对无穷范数；先按数据尺度归一化，直接对数据数组做除法，防止对极小浮点数取倒数溢出。非有限数和不可靠偏差被拒绝。
- ARPACK（稀疏迭代特征值库）采用稀疏 LU 位移反演。复厄密路径直接调用 `eigs`，在取实部之前检查原始候选虚部。工作位移与请求目标分开记录，奇异分解触发有上限的通用扰动重试。最终按原目标排序。
- 原问题验证重新计算缩放无穷范数残差、绝对二范数残差和 `||XᴴX-I||₂` 正交误差；拒绝零、非有限或维数不符的向量。近重根在小结果块中正交化并作 Rayleigh 投影修正，不因数值相近就删掉向量。
- 对角解析路径仅由结构检测触发，直接从输入主对角构造对应标准基向量；不使用样例名称、哈希、参考答案或隐含预生成谱文件。
- 三对角独立认证先验证完整共轭三对角结构。复数非对角元通过对角酉变换具有与绝对值实三对角相同的谱。用 Sturm 计数（逐行递推估计谱窗中的模式数量）作资源预检，再独立调用 `eigvalsh_tridiagonal(select='v', lapack_driver='stebz')` 只计算所需谱窗的特征值及重数，比较原请求最近 k 个或区间模式。该调用不计算全尺寸特征向量。计数不一致、边界不稳定、区间端点邻近特征值或固定 k 截断重根簇时不认证。
- `verify_run` 重新加载原输入和保存数组、重新计算验证与结构认证、检查产物哈希；不盲信 `validation.json`。结果文件验证完成后，最后以原子重命名发布 `manifest.json`。

## 资源边界与尚未支持的路径

| 项目 | 实现与限制 |
|---|---|
| 子空间及结果块 | 请求模式数最多512、子空间最多1024；内部实数 k<n、复数 k<n-1，调用前守卫以防库内稠密回退 |
| 三对角认证 | n不大于200000，谱窗模式数不大于512；超出时仍保留合格候选但不认证 |
| 一般稀疏目标 | 没有可信计数能力，因此即使得到全部请求数量，也为PARTIAL/UNVERIFIED |
| 内存/磁盘/时间 | 核心有预估与采样检查，CLI和服务通过独立进程对整个进程树实施预算；稀疏LU填充不可由nnz准确预测，不能承诺任意输入都满足预算 |
| 取消 | 在线性解、阶段转换、导出前检查；LU/LAPACK本次调用本身不可协作中断，由父进程强制回收保障上限 |
| 线程 | 配置线程预算并设置进程环境；此macOS运行库未向threadpoolctl报告可识别线程池，实际活跃BLAS线程数未知；ARPACK外层及稀疏LU不承诺并行 |
| 迭代指标 | 分解尝试、线性解次数和移位残差矩阵向量乘实际计数；ARPACK外层迭代及全路径矩阵向量乘次数不可得，填null，不能冒充0 |
| 广义问题 | 尚未验证质量矩阵B路径，明确拒绝；不会把B忽略为单位阵 |
| SLEPc/MPI/GPU/JD/LOBPCG | 当前构建均无经过验收的适配器，明确拒绝；能力探测的已安装不等于已支持 |
| 输出配置 | NumPy基础产物始终保留，可额外输出SciPy稀疏格式；要求保存向量以供独立校验，`eigenvectors=false`明确拒绝 |
| 精确续算与回退 | 无ARPACK精确检查点、无已验证替代算法回退配置；有限工作位移重试已经实现 |
| 禁止稠密化 | 原矩阵及位移矩阵从不转全尺寸稠密；只有受限n×k向量与小型k×k投影/Gram块为稠密 |

## 测试证据

按先红后绿开发。首轮实际红灯为尚未实现的数值导出接口；安全头、极小尺度和COO巨大shape等回归测试均在修复前实际失败。已运行：

```bash
.venv-emsolver/bin/python -m pytest tests/test_numerics.py -q
```

44项测试覆盖安全对象拒绝、错误三角、复共轭恢复、非厄密输入、NaN、伪造数组头、异常索引、巨大COO形状分配前拒绝、极小与次正规浮点尺度、奇异位移真实重试、原目标排序、重根子空间、区间空谱和上限、一般稀疏未认证、三对角独立认证及端点、ARPACK部分收敛、复数虚部候选拒绝、预算和取消、产物篡改、禁止调用原矩阵稠密转换，以及真实十万阶输入。

## 十万阶可复现实跑

硬件本机**物理内存17179869184字节，即16GiB**；32GiB行是同一台16GiB机器上的配置预算测试，**不是物理32GB主机验收**。固定seed=20261016、n=100000、k=12、sigma=0、同一残差和正交阈值，各预算独立子进程执行3次。矩阵生成时间不计入求解。运行当时机器负载、温度和频率未采集；这些数字是本机原型证据，不是正式赛事性能成绩。

- `near_singular`：结构对角，nnz=99999，解析路径，包含零模态。该例不等同于稀疏分解或迭代速度。
- `indefinite`：非对角实对称三对角，nnz=299998，真实稀疏LU加ARPACK产生候选，另由三对角谱窗认证。每次1次分解、101次线性求解；非模拟计算。
- 以下每行均SUCCEEDED、12个模式、目标CERTIFIED；再次读取结果均numerical PASSED且artifact_integrity PASSED。时间取父进程端到端时间，内存取父进程监测的整个作业进程树峰值。

| run_id | 家族 | 预算GiB | 请求线程 | 端到端秒 | 峰值字节 | 最大缩放残差 |
|---|---|---:|---:|---:|---:|---:|
| near_singular-certified-16-0 | near_singular | 16 | 1 | 0.323550 | 107888640 | 0.000e+00 |
| near_singular-certified-16-1 | near_singular | 16 | 1 | 0.359135 | 108642304 | 0.000e+00 |
| near_singular-certified-16-2 | near_singular | 16 | 1 | 0.348107 | 107282432 | 0.000e+00 |
| near_singular-certified-32-0 | near_singular | 32 | 1 | 0.349761 | 107839488 | 0.000e+00 |
| near_singular-certified-32-1 | near_singular | 32 | 1 | 0.344671 | 108691456 | 0.000e+00 |
| near_singular-certified-32-2 | near_singular | 32 | 1 | 0.324108 | 107593728 | 0.000e+00 |
| indefinite-certified-16-0 | indefinite | 16 | 1 | 2.451054 | 251674624 | 1.621e-17 |
| indefinite-certified-16-1 | indefinite | 16 | 1 | 2.475418 | 251363328 | 1.621e-17 |
| indefinite-certified-16-2 | indefinite | 16 | 1 | 2.465208 | 250560512 | 1.621e-17 |
| indefinite-certified-32-0 | indefinite | 32 | 1 | 2.461791 | 254263296 | 1.621e-17 |
| indefinite-certified-32-1 | indefinite | 32 | 1 | 2.739838 | 253493248 | 1.621e-17 |
| indefinite-certified-32-2 | indefinite | 32 | 1 | 2.501326 | 250413056 | 1.621e-17 |

8线程请求配置另按相同输入与阈值独立重复3次；这里的线程数是配置值，实际活跃线程数未观测。以下6行均SUCCEEDED且独立数值、目标与哈希验证通过。

| run_id | 家族 | 预算GiB | 请求线程 | 端到端秒 | 峰值字节 |
|---|---|---:|---:|---:|---:|
| near_singular-threads8-control-0 | near_singular | 16 | 8 | 1.219681 | 109084672 |
| near_singular-threads8-control-1 | near_singular | 16 | 8 | 0.794990 | 110739456 |
| near_singular-threads8-control-2 | near_singular | 16 | 8 | 0.880665 | 109051904 |
| indefinite-threads8-control-0 | indefinite | 16 | 8 | 4.780390 | 218972160 |
| indefinite-threads8-control-1 | indefinite | 16 | 8 | 4.307167 | 215842816 |
| indefinite-threads8-control-2 | indefinite | 16 | 8 | 4.333191 | 230932480 |

| 家族 | 16GiB/1线程中位秒 | 32GiB/1线程中位秒 | 16GiB/8线程中位秒 | 解释 |
|---|---:|---:|---:|---|
| near_singular | 0.348107 | 0.344671 | 0.880665 | 8线程请求配置更慢，未宣称加速 |
| indefinite | 2.465208 | 2.501326 | 4.333191 | 8线程请求配置更慢，未宣称加速 |

原始本机记录位于 `/tmp/emsolver-numerical-evidence/`：`certified-stdout.jsonl`、`certified-records.json`、`thread-control-stdout.jsonl`、`thread-control-records.json`。每个run_id目录含原子清单、输入配置、数组、残差、验证、环境及父进程监控指标。临时目录不会自动进入交付包；上述表格保留了本次全部18个正式记录，早先未加三对角认证的探索运行和受并发测试干扰的线程探索运行没有混入表格。

## 复现入口

将以下内容保存为普通Python脚本，在项目根目录用隔离解释器执行；该脚本仅生成合成输入，不需要外网或参考答案：

```python
from pathlib import Path
from emsolver.contracts import resolve_config
from emsolver.execution import execute_local
from emsolver.numerics import generate_sample, verify_run

if __name__ == '__main__':
    root = Path('/tmp/emsolver-reproduce')
    root.mkdir(exist_ok=True)
    matrix = root / 'matrix.npz'
    generate_sample(matrix, kind='indefinite', n=100000, seed=20261016)
    config = resolve_config({
        'target': {'k': 12, 'sigma': 0.0},
        'resources': {'memory_limit_gib': 16, 'threads': 1, 'walltime_seconds': 180}
    })
    output = root / 'new-run'
    print(execute_local(matrix, config, output))
    print(verify_run(matrix, output))
```

三对角接口语义通过本地SciPy1.18.1公开函数说明与实际调用核对：`eigvalsh_tridiagonal.__doc__`明确`select='v'`为`(min,max]`，实现使用外扩谱窗并单独处理产品`[lower,upper)`端点；通过`eigvalsh_tridiagonal`只取特征值，而不请求n×n向量。

## 原始记录指纹

- `certified-stdout.jsonl` SHA256：`a2eb107b21ee6b6eb6ca85ac6c6a270e0faad0205a59411037f58f27275c138d`
- `certified-records.json` SHA256：`19635b8215c5e98a3019500f28f6ad7743981f643f29336a64b56ad302dc2ef3`
- `thread-control-stdout.jsonl` SHA256：`7062960adfc25cc31ed78208d3c7b95a217c4bcc514013eb8a472a9fe830a6b2`
- `thread-control-records.json` SHA256：`2a080aab32395c286ae1a1d71af4533754f311ec956159330bfca5047d70125f`

源码指纹（独立审查修复后的最终验收代码）：

- `emsolver/numerics/__init__.py` SHA256：`e75408f6a2e7bd90ed1c1812256c43a0b6cc9b801694f653dbf874333ac35808`
- `emsolver/numerics/core.py` SHA256：`4ec0dc5ca2f0319278de738120b30d716a1fbc3ec1e44222200f2737ea48ad89`
- `tests/test_numerics.py` SHA256：`782dcf08b8becf23e85fad84f62c830705c127b01d7b8fc35da3d5b135282881`


## 独立审查后的远目标排序修复

独立审查复现：`diag([0,1,2,3])`、`k=1`、`sigma=1e20` 原实现通过归一化差值排序，将不同距离舍入为相等，错误选择0，且认证共用同一排序导致自证。修复前新增测试真实出现3项失败，修复后定向测试7项通过；随后包含跨二进制尺度与次正规浮点数的精确对照测试在内，完整数值套件31项通过。

最终实现采用谱外单调排序、谱内原始距离排序；浮点舍入相同或溢出的距离使用 `Fraction`（基于二进制浮点精确整数比值的有理数）比较，并保留距离相同时按特征值升序的规则。不使用在arm64可能等同float64的`longdouble`补救。结构认证另用独立的完整精确有理数排序；对角候选必须准确匹配输入结构特征值，并落在其精确特征子空间。即使测试强行破坏求解排序，独立认证也会拒绝。

前述18行是审查前同目标sigma=0的历史实跑记录，不将其耗时冒充最终代码性能。修复后同机16GiB/1线程再次独立执行如下7次，全部SUCCEEDED、目标CERTIFIED，重新读取数值及产物哈希均PASSED：

| run_id | 家族 | sigma | 端到端秒 | 峰值字节 | 最大缩放残差 |
|---|---|---:|---:|---:|---:|
| near_singular-postreview-0.0-0 | near_singular | 0 | 0.733460 | 132595712 | 0.000e+00 |
| near_singular-postreview-0.0-1 | near_singular | 0 | 0.735847 | 133316608 | 0.000e+00 |
| near_singular-postreview-0.0-2 | near_singular | 0 | 0.701909 | 133382144 | 0.000e+00 |
| indefinite-postreview-0.0-0 | indefinite | 0 | 2.408149 | 248397824 | 1.621e-17 |
| indefinite-postreview-0.0-1 | indefinite | 0 | 2.368086 | 250511360 | 1.621e-17 |
| indefinite-postreview-0.0-2 | indefinite | 0 | 2.376463 | 250036224 | 1.621e-17 |
| near_singular-postreview-1e+20-0 | near_singular | 1e+20 | 0.631206 | 133726208 | 0.000e+00 |

修复后`near_singular`中位时间为0.733460秒。
修复后`indefinite`中位时间为2.376463秒。

新增十万阶远位移sigma=1e20案例首个特征值正确为5.0。对角路径增加独立精确有理数排序的CPU成本；一般三对角真实LU/ARPACK路径仍通过认证。32GiB预算和8线程历史数据未在此次排序修复后全量重测，不对其最终代码耗时作外推。

- `postreview-stdout.jsonl` SHA256：`b47c75552b3aa2113de594f067b57094dd27e9d030e59ef9cf8af0f388d0d0ff`

- `postreview-records.json` SHA256：`f8caf4b3f32e1dcf692845efc805dada5cfaf3621a276c37e75f280d4ffdb59b`


## 最终独立审查：邻居模式归属与稳定范数

2026年10月16日最终数值状态：完整套件 **44 passed in 9.18s**。在前述远目标排序修复之外，第二轮独立审查的错误邻居回归按先红后绿修复：

- 参考根间隙很小时，固定匹配容差原先可能把2.5e-13、3e-13或4e-13邻居当成目标0。现保留完整独立谱窗；任一选中根的不同邻居间隙不大于4倍基础匹配容差时，标记`unresolved_neighboring_modes`且不认证。其他情况下匹配容差小于局部间隙的四分之一。正确但无法区分的近根也保守不认证。
- 特征值数值正确不代表向量属于相同模式。认证现实际接收向量，用原问题残差的稳定2范数除以谱分离下界，估计每模式的子空间夹角上界；分离下界同时包含独立参考邻居和已计数谱窗的两端，并扣除参考及候选误差。上界大于1e-4时不认证。`lambda=0`但向量来自1e-10、1e-9、1e-8邻居的三个反例全部被拒绝，真零模式仍可认证。独立审查者另行复核确认上述真/假两组模式，未发现新的阻断性数值问题。
- 区间缺少重根重数、区间端点邻近参考根，以及距离排序在参考误差范围内可能翻转的不同模式，均不认证。参考缩放如令非零结构数据下溢为零，也不认证。
- 绝对二范数残差改在原始单位重新计算，再按最大分量先缩放后求范数，防平方下溢。`diag([1e-200,1])`、候选lambda=0、第一标准基向量的真实绝对残差正确为1e-200；当绝对阈值为1e-210时判数值失败。1e308动态范围与复相位也已覆盖。
- 复数次正规非零向量按实部、虚部分别除尺度归一化，避免复数除法溢出产生NaN或导致SVD异常。

下表为以上全部修复后的最终源码实跑；前述其他计时行仅保留为历史阶段证据。固定本机16GiB物理内存、16GiB预算、1线程请求，两个100000阶家族各3次，远位移另1次。全部SUCCEEDED且独立重新读取后的数值、目标及哈希验证通过：

| run_id | 家族 | sigma | 端到端秒 | 峰值字节 | 最大缩放残差 | 模式夹角上界 |
|---|---|---:|---:|---:|---:|---:|
| near_singular-final-validated-0.0-0 | near_singular | 0 | 1.265265 | 132497408 | 0.000e+00 | 解析对角 |
| near_singular-final-validated-0.0-1 | near_singular | 0 | 0.885918 | 140312576 | 0.000e+00 | 解析对角 |
| near_singular-final-validated-0.0-2 | near_singular | 0 | 0.881136 | 133808128 | 0.000e+00 | 解析对角 |
| indefinite-final-validated-0.0-0 | indefinite | 0 | 3.218458 | 232374272 | 1.621e-17 | 1.565e-06 |
| indefinite-final-validated-0.0-1 | indefinite | 0 | 4.168560 | 223657984 | 1.621e-17 | 1.565e-06 |
| indefinite-final-validated-0.0-2 | indefinite | 0 | 2.748143 | 251297792 | 1.621e-17 | 1.565e-06 |
| near_singular-final-validated-1e+20-0 | near_singular | 1e+20 | 0.720197 | 138641408 | 0.000e+00 | 解析对角 |

最终`near_singular`三次中位端到端时间：0.885918秒。
最终`indefinite`三次中位端到端时间：3.218458秒。

线程活跃数、外部机器负载及正式比赛环境仍未观测；不把这些时间外推为比赛成绩或其他硬件保证。

- `final-validated-stdout.jsonl` SHA256：`fa62e5dbbfbe664dc2c58bdcf549ea9a2efd91fe71552ca46a2922bc98824b0e`

- `final-validated-records.json` SHA256：`8108608f03a70c890abdbb170f5154f71e1842d22c9dde3f10a9d9ef232b5146`
