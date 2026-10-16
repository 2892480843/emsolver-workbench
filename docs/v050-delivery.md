# v0.5.0 交付记录

2026-10-16。本轮继续完成原有12项的可执行软件路径，增加数值认证、精确恢复、工程模型映射、Metal/SLEPc/MPI及真实本地模型。**当前65项已实现、1项有条件，不能宣称全部工业或赛事验收完成。** 原始66项条款未删减；逐项范围见 [PRD验收](prd-acceptance.md)。

F48保留条件：已实际取得带官方来源、许可与摘要的真实波导和500MHz微波滤波器矩阵，但两个原始输入均非厄密，系统正确拒绝；没有修改系数使其通过，也没有把教学算例或MHD单独B矩阵称为真实微波器件正向验收。

## 主要变更

| 范围 | 实现与约束 |
|---|---|
| F14/F45 | 新增一般实/复稀疏精确有理惯性、重数与候选子空间覆盖检查；限64阶及明确运算预算，超限保持未认证 |
| F15/F30/F46 | 管理员授权的不可变工程侧车、实际坐标/自由度、四种审核频率映射、稀疏零空间声明与模式图；模型随计划及任务冻结 |
| F25/F34/F38 | 真实收敛事件、严格区分后端估计与原问题残差；固定工作位移块算法完整状态恢复，保留随机数/累计预算/实际分解成本 |
| F48 | CC0二维PEC TM_z教学网格与独立离散参考；真实公开电磁输入及原始非厄密拒绝记录 |
| F50/F63 | SLEPc/PETSc/MUMPS实数/复数MPI1/2隔离候选；宿主与容器共用半额分区预算，取消/失联按本任务标记清理；实际Metal混合精度修正与CPU双精度验收 |
| F54 | 固定revision的Qwen2.5-0.5B-Instruct MLX4bit，9个文件摘要及Apache-2.0许可证；仅回环解释，真实4类输出与3类降级；规则及数值状态保持权威 |
| 架构与数据 | schema2不破坏迁移；工程快照、备份引用/载荷、权限、内容寻址、能力档案与独立工作进程贯通 |

架构详见 [系统与数据结构](v050-system-architecture.md)。精确恢复对源码、已加载函数、输入、配置及实际运行环境严格绑定；对代码引用共享和数值库发现顺序做规范化，真实属性变化仍拒绝。Metal选择会采用适合低精度修正的内层初始阈值，原问题验收阈值不变；未实际执行GPU乘积时策略明确记为CPU。

## 新鲜验证

| 证据 | 结果与适用边界 |
|---|---|
| [本机全量](evidence/v050-macos-tests.log) | 637通过、零跳过；macOS ARM64，含实际可选Metal与SLEPc测试，无性能加速泛化 |
| [Linux全量](evidence/v050-linux-tests.log) | 616通过、21跳过、零失败；amd64在ARM64宿主翻译执行。20项未启用的可选环境/硬件测试与1项Rosetta身份不兼容测试明确跳过，未放宽生产守卫 |
| [前端测试](evidence/v050-web-tests.log) / [构建](evidence/v050-web-build.log) | 60通过，类型检查与生产构建通过；包括GPU配置一致性与精确恢复入口门禁 |
| [数学独立核对](evidence/inertia-review.json) | 448/448小矩阵惯性计数核对；不是任意工业谱完整性证明 |
| [SLEPc实际档案](slepc-backend.md) | 实/复×MPI1/2四组原问题验证及目标认证；44项专项含真实取消、强杀、隔离和预算反例 |
| [macOS冷安装](evidence/v050-macos-final-instance.json) | 新环境无索引安装wheel，使用交付目录的前端资源完成服务、求解、独立验证、公开包检查、备份恢复与重跑；宿主网络未禁用 |
| [Linux冷实例](evidence/v050-linux-offline-instance.json) / [镜像绑定](evidence/v050-linux-image.json) | 最终镜像network=none、只读根、去能力，实际完整服务流程；不挂源码/开发环境/参考/容器套接字 |
| [参考隔离](evidence/v050-linux-isolation.json) | 原问题PASSED/CERTIFIED；参考和套接字DENIED、网络错误101，容器退出后才做参考评估 |
| [本地模型](evidence/v050-local-model.json) / [冷安装可选运行包](evidence/v050-optional-cold-install.json) | 固定模型实际生成并经严格解析；独立无索引安装后实际本地推理及数值Metal乘积通过。小模型文字质量不作为科学认证 |
| [真实电磁数据](evidence/v050-public-datasets.json) / [MHD本版复验](evidence/v050-nist-local-validation.json) | 保留真实非厄密拒绝；NIST六候选原问题有效，目标未认证且不公开未确认许可的原输入或向量 |
| [正式实例与数据](evidence/v050-production-validation.json) / [正式模型](evidence/v050-production-local-model.json) | 8000端口0.5.0，新增教学项目已求解认证、16节点真实场、6频率；原82实体、285事件、70审计、34幂等记录均逐行保持原值，只追加新示例 |
| [浏览器画面](evidence/v050-engineering-ui.png) | 实际浏览、频率列、z分量切换、真实16节点及18三角连接；不是后端HTTP测试冒充UI验收 |

## 交付文件

| 文件 | 用途 |
|---|---|
| `artifacts/emsolver-source-0.5.0.zip` | 源码、前端、测试、PRD、平台配方、材料与证据；不含私有数据或模型权重 |
| `artifacts/emsolver-offline-macos-arm64-py312-0.5.0.zip` | 基础CPU离线应用、29个本地wheel及已构建界面；macOS14+、arm64、Python3.12 |
| `artifacts/emsolver-linux-amd64-0.5.0.docker.tar.gz` | 已安装应用与前端的Linux核心运行镜像；不含可选模型或Docker套接字 |
| `artifacts/emsolver-slepc-bookworm-arm64-v1.docker.tar.gz` | 已实测的Linux ARM64 SLEPc/PETSc/MPI依赖镜像；保留精确包版本及许可证收据 |
| `artifacts/emsolver-optional-metal-local-ai-macos26-arm64-py312-0.5.0.zip` | 可选Metal/本地模型完整wheel、固定4bit权重及分词器、许可和校验脚本；需要macOS26+、Apple Silicon、Python3.12 |
| `artifacts/RELEASE-0.5.0.json` | 最终摘要、字节数、源码/镜像绑定及验证指针；各ZIP内部另有逐文件摘要 |

旧0.4及更早发布包保留。本机主应用已启动在 `http://127.0.0.1:8000/`，模型仅监听回环18321；临时冷验收服务已停止。基础CPU包不会假称具备未安装的可选后端。模型与数值GPU是不同功能。

## 重启与使用

普通CPU启动使用 `scripts/start.sh`。当前本机完整配置使用 `.venv-emsolver-model`，模型先单独启动：

```bash
.venv-emsolver-model/bin/python scripts/local_model.py serve --directory artifacts/local-models/qwen25-05b-instruct-4bit --port 18321
```

另一个终端从项目根目录启动主应用：

```bash
export EMSOLVER_LOCAL_MODEL_CONFIG="$PWD/artifacts/local-model-config.json"
export EMSOLVER_SLEPC_IMAGE=emsolver-slepc:bookworm-arm64-v1
export EMSOLVER_DOCKER_SOCKET="$HOME/.docker/run/docker.sock"
.venv-emsolver-model/bin/python -m emsolver serve --host 127.0.0.1 --port 8000
```

没有该平台环境时按对应离线包README安装；不要替换原数据根目录。查看“v0.5 教学验收示例”项目可复核工程映射及结果。已有任务维持其冻结版本；新精确检查点只对兼容版本及环境开放。

## 仍需外部验收

真实厄密微波器件模型及合法独立参考、官方保留集、原生赛事硬件性能与评分尚未取得。通用认证不超过声明预算，CUDA、多节点、任意MPI规模和ARPACK/JD/SLEPc精确恢复未列为已支持。以上是明确的支持与科学证据边界，不能用材料检查通过替代。
