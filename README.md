# EMSolver 电磁模式求解工作台

## 参赛交付入口

- [作品方案介绍 PDF](docs/submission/EMSolver-project-introduction.pdf)：背景、架构、场景、创新、运行方法与验收边界。
- [介绍文档 Markdown](docs/submission/作品方案介绍.md) 与[功能完成度声明](docs/submission/功能完成度与未完成项.md)。
- [全部 66 项原始要求与代码证据](docs/submission/requirements-status.json)：本次按保守闭环口径登记 62 项范围内闭环、4 项部分实现／条件未闭环，分别占 93.94% 与 6.06%。这不是代码覆盖率或工业验收通过率。
- [交付说明与复验记录](docs/submission/README.md)。源码包含所有核心实现、前后端构建、测试、配置模板和合法小样例；无需 API 密钥即可完成最小 CPU 流程。

本仓库发布当前源码与参赛材料，不包含真实业务库、凭据、模型权重、大型输入及旧版本离线归档。下文历史 `artifacts/` 路径用于保留实验证据索引，不代表这些本机归档已上传；当前公开证据以 `docs/submission/` 和仓库内 `docs/evidence/` 为准。

当前开发版本为 **v0.9.0**：新增显式磁盘外对称不定分解，完整58.9万阶真实微波矩阵已获得数值合格候选，并与隔离ARPACK参考核对；目标无遗漏证明仍未完成。见[磁盘分解配置](docs/slepc-ooc-profile.md)和[本轮交付](docs/v090-delivery.md)。旧版本与失败试验继续保留。

面向中国商飞06赛题的本地开源软件：安全导入稀疏矩阵、冻结求解配置、监控独立计算进程、验证特征对、对比实验并导出报告和复现包。图形界面、命令行和API共用配置协议与数值核心。

核心输入是满足厄密条件的标准特征值问题 `Ax = λx`，允许负特征值与零模态。默认不依赖外网、GPU或大模型服务。当前经过验证的路径包括SciPy ARPACK、块位移反演和JD修正方程；可选后端包括已登记组合的Linux SLEPc实/复单秩与双秩、macOS ARM64 Metal低精度修正并由CPU双精度复验。广义问题、非厄密问题及其他硬件组合仍不在支持范围。

当前版本`0.9.0`将项目角色、配额与优先级、异步导入、可替换数值后端、块迭代与JD修正、自适应精度及位移、子空间快照、进程接管、知识与建议效果、冻结基准协议、产物保留和受保护恢复接入同一工作流。数据库仍为schema2，新增实体通过可重建投影和不可变记录扩展；旧实验保留原始哈希与显式旧策略。详见[逐项验收](docs/prd-acceptance.md)、[部署与治理](docs/deployment-and-governance.md)、[数值实现](docs/numerical-completion.md)和[冻结基准协议](docs/benchmark-protocol.md)。功能支持范围、数据与目标硬件条件分别记录，材料检查通过不代表赛事验收。

## 快速运行

多人、多计算节点部署使用新增的 **cluster 模式**：PostgreSQL 元数据、按项目授权的独立 HTTP 计算节点、持久租约和服务端结果复验。设置页可登记、查看和撤销节点；项目/矩阵编辑支持版本冲突检查。部署、迁移与适用边界见[团队架构与部署](docs/team-architecture.md)。原有本地 SQLite 模式继续保留；已冻结的 v0.9.0 离线归档未重打包，新增能力需使用当前源码或新构建的 `emsolver-team:local` 镜像。

要求Python3.11至3.14，推荐3.12；构建界面需要Node22及npm。以下命令从项目根目录运行。仓库中旧`.venv`保留不动，本项目使用独立环境。

新配置、示例和界面草稿默认申请 **1 个 CPU 线程**，可在低核数机器上完成最小流程。显式填写的线程数会原样保存；本地规划仍拒绝超过可用 CPU 数的请求，集群则按计算节点实报容量匹配。历史实验及已冻结配置中的线程值不变。

```bash
uv venv --python 3.12 .venv-emsolver
uv pip install --python .venv-emsolver/bin/python -r requirements.lock
uv pip install --python .venv-emsolver/bin/python --no-deps -e .
cd web
npm ci
npm run build
cd ..
./scripts/start.sh
```

打开 [本地工作台](http://127.0.0.1:8000)。首次运行没有预置伪造数据：点击“体验完整求解”生成标明来源的合成矩阵，检查预检计划，再提交计算。也可在矩阵库导入自己的`.npz`或受限大小的数值`.npy`。

无uv时可用`python3.12 -m venv .venv-emsolver`，随后使用该环境内的`pip install -r requirements.lock`及`pip install --no-deps -e .`。数据默认写入`data/`，由`EMSOLVER_DATA_DIR`覆盖；运行端口可用`EMSOLVER_PORT`覆盖。重启时不要更换数据根目录。

## 不启动界面也能求解

```bash
.venv-emsolver/bin/emsolver doctor --json
.venv-emsolver/bin/emsolver sample --kind complex --n 500 --output /tmp/complex.npz
.venv-emsolver/bin/emsolver inspect --matrix /tmp/complex.npz --json
.venv-emsolver/bin/emsolver solve --matrix /tmp/complex.npz --config examples/default.yaml --output runs/complex
.venv-emsolver/bin/emsolver validate --matrix /tmp/complex.npz --run runs/complex --json
.venv-emsolver/bin/emsolver results export --run runs/complex --format both --output artifacts/complex
.venv-emsolver/bin/emsolver report --run runs/complex
```

输出目录必须是空目录，避免覆盖已有实验。`solve`默认独立子进程执行并监测进程树内存、时间和输出磁盘；达到上限受控结束。资源峰值为采样值，采样间隙可能存在更短瞬时峰值。

**退出码4表示已得到合格候选，但完整请求尚未认证，属于预期的部分成功状态。** 它不等于程序崩溃。任意稀疏目标不能仅因残差小就宣称“没有遗漏”；支持认证的结构在能力探测结果中列明。`validate`同样在数值有效且目标已认证时退出0，只有数值通过而目标未认证时退出4。

`jobs submit`是持久入队操作，退出0只表示入队；启动工作台服务或执行`jobs wait --job JOB_ID`才会持续执行队列。`plan`、`jobs status`等短命令不会抢占或启动后台计算。

| 退出码 | 含义 |
| --- | --- |
| 0 | 所请求操作完成；同步求解还要求数值与目标认证均通过 |
| 2 | 输入或配置不合法 |
| 3 | 未收敛 |
| 4 | 部分模式或目标未认证 |
| 5 | 资源超限 |
| 6 | 后端不可用 |
| 7 | 独立验证失败 |
| 8 | 用户取消 |
| 9 | 工作进程、I/O或内部错误 |
| 10 | 时间预算耗尽 |

## 工作台功能

13个工作区包括项目、矩阵诊断、求解配置、任务、结果、对比、基准、工业验收、规则诊断、报告、设置、审计与能力登记。项目可创建、归档，矩阵和配置形成不可变版本；任务支持幂等提交、事件补取、取消、关联新任务重跑；对比只有在输入、精度、资源和环境匹配时才允许解释性能。

规则助手只提供可解释配置建议，采用后生成新配置，不修改已启动任务。未安装本地AI模型时显示规则来源，求解不依赖AI。没有物理频率映射和网格元数据时，不把特征值冒称Hz或生成虚假的器件场图。

基准实验支持矩阵 × k × σ × 线程限制 × 内存限制 × 自适应位移开关组合。先检查总预算、冻结方案，再显式提交；轴值去重，总运行最多100次。重复提交同一方案返回同一批次，全部失败和部分结果保留在分母中，认证成功耗时单独汇总。总时间与磁盘是逐次硬限额之和，并非实际消耗预测。

## API与开发

启动后查看 [OpenAPI接口文档](http://127.0.0.1:8000/docs)。接口位于`/api/v1`；旧列表返回数组，新分页接口返回`items,next_cursor,total`，错误返回`error.code`及脱敏说明。前端开发：`cd web && npm run dev`，代理到本地8000端口；生产方式由FastAPI同源托管`web/dist`。

单用户默认只监听回环地址。需要暴露到其他主机时，先设置自行生成的`EMSOLVER_API_TOKEN`并使用受控网络和HTTPS入口；工作台设置页可输入token，保存在当前浏览器会话。共享token不提供个人账号和项目级成员权限，不应当作多租户系统使用。计算工作进程是独立进程，不是操作系统文件沙箱。

```bash
export EMSOLVER_API_TOKEN="自行生成的高熵令牌"
.venv-emsolver/bin/emsolver serve --host 0.0.0.0 --port 8000
```

## 验证与基准

```bash
.venv-emsolver/bin/python -m pytest -q
cd web && npm test && npm run build
cd ..
.venv-emsolver/bin/emsolver benchmark --suite benchmarks/required.yaml --output runs/required
```

基准保留成功、部分、失败和超限行；例行套件每个数值场景重复3次，资源不足场景1次。结果在`benchmark.json`，不可将“符合预期状态”混同于全部目标认证。真实运行证据、硬件与限制见[数值实施记录](docs/numerical-implementation.md)及[实际功能覆盖](docs/feature-coverage.json)。十万阶结果仅适用于记录中的矩阵结构和目标，不推广到任意十万阶输入。

最终集成证据见[验证记录](docs/verification.md)。可用`./scripts/demo.sh`运行十万阶合成例的完整命令行闭环；用`python scripts/check_delivery.py --require-build`检查源码材料，缺项时退出2。公开数据候选、许可证和下载限制见[数据说明](docs/datasets.md)。

## 数据与产物

原始输入按内容哈希只读保存，三角存储必须明确声明；复数用共轭恢复，三角外污染和非厄密输入默认拒绝。禁止将原始稀疏矩阵转为n×n稠密矩阵，允许的长瘦向量块与小投影仍受预算约束。

NumPy输出为`eigenvalues.npy`、`eigenvectors.npy`（列为模式）和`residuals.npy`；可选SciPy结果为`eigenvalues.scipy.npz`与`eigenvectors.scipy.npz`。`manifest.json`最后发布并记录校验和。数值核心产物的监测指标与父进程监测分开存储，避免修改已哈希文件。

API任务的数值产物在`data/runs/<job_id>/artifacts/`，事件与控制文件在其外层。公开复现包默认排除输入、身份、敏感路径和令牌；仅当任务冻结元数据明确允许公开且提供许可证时，才包含模态结果。私有任务同时排除特征值、特征向量与运行诊断，包内`export-policy.json`列出排除项和替代样例命令。修改矩阵公开属性不会追溯授权旧任务。用户需提供有权使用的原始矩阵才能重跑。备份是包含私有输入和结果的受保护完整包，恢复仅创建新实例，不覆盖正在运行的数据。

## 部署和交付边界

提供Dockerfile用于Linux部署；v0.8 已实际完成 macOS ARM64 冷安装和受隔离的 Linux amd64 镜像冷运行，后者为 ARM 主机翻译执行的功能证据。可选后端另有 macOS Metal 混合精度修正、Linux SLEPc 实数/复数单秩与双秩的实际验证。支持范围以 [当前架构](docs/v080-numerical-architecture.md)、[SLEPc档案](docs/slepc-backend.md) 和 [Metal档案](docs/metal-profile.md) 为准；不推导其他组合或加速成绩。容器启动使用非回环地址时同样要求token，建议只把容器端口映射到宿主机127.0.0.1。离线交付前应在对应系统预下载Python wheels并携带已构建的`web/dist`，避免现场联网安装。

`./scripts/prepare_offline.sh artifacts/offline-new`会下载锁定的wheels、构建本项目wheel并打包界面；必须在与目标系统、架构和Python版本兼容的环境执行。生成目录中的`install.sh`按`python-version.txt`选择解释器，使用`--no-index`从本地安装。v0.9.0 的平台离线包目录为 `artifacts/offline-macos-arm64-py312-v090/`，安装与验证见 [交付记录](docs/v080-delivery.md)。旧版归档继续保留，不能替代本版证据。

Linux另提供`Dockerfile.dependencies`联网准备依赖和`Dockerfile.runtime`本地wheel禁网构建配方。最终镜像已在`--network none`、只读根、去额外权限及2GiB限制下实际跑通新服务、构建资源、求解、原问题验证、公开包、备份恢复和新任务重跑。镜像为Linux amd64，在macOS arm64翻译环境实测，不能冒充原生比赛硬件性能。详见[部署说明](docs/deployment-and-governance.md)、[新版逐项验收](docs/prd-acceptance.md)与[冻结评测的完整分母](docs/v040-benchmark-results.md)。

许可证和第三方依赖见`LICENSE`、`NOTICE.md`。本软件是基于PRD的实际开发版本；未达到的条件、未测硬件和待补材料均在能力登记中保留，不将规划自动标记为已验收。参赛报名、介绍材料和演示视频需要由参赛负责人根据实际作品提交。

## v0.5 新增能力

工程侧车及真实坐标模式图、四种审核频率映射、稀疏零空间声明、有限预算的通用精确惯性认证、固定块状态精确恢复，以及可选 Metal/SLEPc/MPI 与获许可本地解释模型。架构和边界见 [当前系统设计](docs/v050-system-architecture.md)，可复现二维算例见 [腔体教学算例](docs/cavity-case.md)。
