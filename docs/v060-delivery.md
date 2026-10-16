# v0.6 本机工业流程交付

本轮新增工业验收工作区、不可变档案／修订／历史检查／报告、完整外置参考资产、项目权限与备份恢复。实际公开微波原矩阵已取得，但**尚未完成该工业原矩阵的正向目标模式与完整性验收**，不得把流程实现计为器件验证通过。

## 本轮实现

- 工业验收档案绑定项目、矩阵、候选与基线、目标、物理证据、独立参考和可选资源门槛。
- 矩阵特征值与器件谐振频率分开验收；无几何映射的真实大矩阵可建档，不补造物理场图。
- 完整参考以只读 NPZ 资产存储；主服务读有界头部，独立子进程做有限值与模式对齐。支持上限为200万行、32模式且总向量项不超过800万；文件和展开各限128MiB。
- 参考和物理审查可核对已配置机构回执；没有独立审查证据保持未认证。回执认证的是机构声明，不是官方认证。
- 备份对档案、检查、参考及跨实体关系做校验；信任密钥禁止放在业务根，配置文件另从备份排除。
- 修复最近k任务的空结果被标“数值通过”，及大整数参考转FP64静默舍入后零容差误判通过的问题。

## 真实工业算例

来源：[SuiteSparse Dziekonski/gsm_106857](https://sparse.tamu.edu/Dziekonski/gsm_106857)，九阶微波梳状滤波器有限元系统。完整矩阵589446阶、21758924非零元，实对称，未裁剪或更改系数，原包和原MTX均保留。来源、署名、CC-BY-4.0许可、三份哈希和转换资源见[数据证据](evidence/v060-public-industrial-dataset.json)。

首次通用CPU JD配置为k=4、sigma=0、子空间24、Jacobi预条件、GMRES最多200步、最终缩放残差1e-8、任务4GiB/120秒。实际14.88秒结束、采样峰值1753464832字节，首轮内层相对残差0.36694，返回0个合格模式，失败为NO_VALID_MODES。该旧实验也揭示空结果状态问题；最终版本正式服务重新运行用时18.805秒、宿主进程树采样峰值1618477056字节，仍返回0个合格模式；`numerical_status=FAILED`、`target_certification=UNVERIFIED`、工业档案检查为FAIL，详见[实际记录](evidence/v060-industrial-production.json)。旧实验保留且未覆盖。

不能根据公开矩阵难点调整参数后宣称通用性能；没有执行不受本机可用内存保护的大规模LU。独立参考、目标完整性、器件频率及工业性能均未据此通过。

## 验证与运行

本轮环境：macOS ARM64，16GiB物理内存。验证结果：

| 范围 | 已观察结果 | 证据 |
|---|---|---|
| macOS 全量回归 | 680通过、14跳过、0失败；跳过含12项显式开关的SLEPc测试及2项本机无扩展精度测试。 | [原始日志](evidence/v060-macos-tests.log) |
| 实际可选后端 | 另启用显式开关，后端26项及MPI取消1项通过；覆盖前述12项后端跳过。 | [后端日志](evidence/v060-optional-backend-tests.log)、[取消日志](evidence/v060-slepc-service-tests.log) |
| 工业模块最终定向回归 | 41通过、2项扩展精度跳过；包含备份恢复和完整外置参考。 | [日志](evidence/v060-industrial-final-tests.log) |
| 前端 | 60测试通过；类型检查及生产构建成功。 | [测试](evidence/v060-web-tests.log)、[构建](evidence/v060-web-build.log) |
| macOS 新安装wheel | 新环境无索引安装；服务、原输入复验、报告／包、备份、空实例恢复及重跑通过。 | [最终实例](evidence/v060-macos-final-instance.json) |
| 新安装工业流程 | 独立参考比较、冻结档案、备份恢复与再检查通过；教学夹具整体保持UNVERIFIED。 | [领域记录](evidence/v060-industrial-cold-domain.json) |
| Linux 镜像 | 无网络、只读根、去能力、最多2GiB/2CPU；103专项测试及运行／领域冷启动通过。amd64在ARM宿主翻译运行，不是原生性能证据。 | [镜像证据](evidence/v060-linux-image.json) |
| 历史数据 | 基线90实体、299事件、78审计、34幂等记录逐行保持；新增工业项目不覆盖原数据。 | [保留检查](evidence/v060-original-data-preserved.json) |

精度修复前的失败日志另保留，最终状态以重新运行的日志为准。软件夹具的通过不增加实际工业算例的数值或物理结论。旧v0.5镜像、归档和证据已核对保持，不替代新模块的验证。

说明：[工业验收使用流程](industrial-acceptance.md)、[系统架构](v060-system-architecture.md)、[逐项PRD登记](prd-acceptance.md)。

## 本轮分发与启动

- 源码：`artifacts/emsolver-source-0.6.0.zip`，含当前纯Python wheel与已构建前端。
- macOS ARM64/Python3.12 CPU离线包：`artifacts/emsolver-offline-macos-arm64-py312-0.6.0.zip`；解压后执行`./install.sh`，依赖通过`--no-index`安装。
- Linux amd64：`artifacts/emsolver-linux-amd64-0.6.0.docker.tar.gz`；先`docker load`，镜像名`emsolver-runtime:0.6.0`。挂载可写数据目录，生产启动按部署说明配置项目认证。
- 公开工业数据独立包：`artifacts/emsolver-public-industrial-gsm-106857-v1.zip`，包含原始官方压缩包、完整CSR转换输入、许可归属和摘要；向离线主机传输时保留所有来源文件。
- 发布摘要与边界：`artifacts/RELEASE-0.6.0.json`。此前可选Metal/本地模型和SLEPc包按各自原版本使用，不将旧工作台wheel覆盖到新版环境；本轮没有重新分发这些可选包。

当前本机界面为 http://127.0.0.1:8000/ ，侧栏“工业验收”，项目“公开工业验证 · 微波梳状滤波器”。首次部署基本CPU路径可用`./scripts/start.sh`，可选后端须按既有文档明确配置。所有归档哈希逐个文件检查，不替代赛事报名与正式提交。
