# 软件交付验证记录

验证日期：2026-10-16。环境：macOS、arm64、Python3.12，物理内存16GiB。证据仅支持所列输入和当前代码，不能当作正式Linux评测或全部PRD验收。

| 检查 | 实际结果 | 证据 |
|---|---|---|
| Python完整自动测试 | 91 passed，0 failed，22.88s；1项Starlette/httpx迁移提示 | `python -m pytest -q`；`evidence/verification.json` |
| 前端生产构建 | TypeScript检查、Vite构建通过，JS约320kB、gzip约99kB | `cd web && npm run build` |
| 固定基准套件 | 共16次：12成功、3部分、1次预期内存不足；全部符合预期 | `evidence/required-benchmark.json` |
| 最终数值核心十万阶 | 7次全部数值有效、目标已认证、读回哈希通过；实测结构为对角及厄密三对角 | `evidence/100k-records.json`、`100k-stdout.jsonl`；`numerical-implementation.md` |
| 本地wheel无索引安装 | 全新同平台虚拟环境，从本地wheels安装；独立安装的Web存活、队列、后端和存储均正常，返回真实前端HTML | `scripts/prepare_offline.sh`生成的`install.sh`；`evidence/verification.json` |
| 新安装环境计算 | 100000阶、8个模式，独立数值/目标/哈希校验全部通过 | `evidence/offline-100k-result.json`、`offline-100k-check.json` |
| 最终界面求解 | 新样例→冻结计划→提交独立进程→12个模式，最大缩放残差9.32e-17、正交误差1.57e-15 | `screenshots/workbench-result.jpg` |
| 工作台交互 | 12个工作区、向量预览、规则采用、基准、报告及包下载；390px无横向溢出 | 前端独立浏览器实测；最终主流程经应用内浏览器复核 |
| 交付材料检查 | 31项通过，缺项清单为空 | `delivery-check.json` |
| 独立审查 | 发现并修复数值假认证、极端排序、残差下溢、重试竞态、公开包隐私、CLI副作用及失败报告等问题 | `independent-review.md`及对应回归测试 |

核心源码和前端源文件指纹在`evidence/source-sha256.json`。数值原始日志指纹与最终算法说明在`numerical-implementation.md`，历史记录和最终代码记录分别列明。32GiB配置是在16GiB物理机器上设置的软预算，不能称为32GiB硬件测试。请求线程数不能等同于实际BLAS并行数。

当前66项登记为：15项所列范围已实现、41项部分实现、10项有条件或未实现。每项精确范围、限制、代码与测试入口见`feature-coverage.json`；这些状态不能直接折算为比赛完成比例。

## 使用

```bash
./scripts/start.sh
```

打开`http://127.0.0.1:8000`，API文档位于`/docs`。命令行演示：

```bash
./scripts/demo.sh runs/new-demo
.venv-emsolver/bin/python scripts/check_delivery.py --require-build
```

原始数据默认保存在`data/`，启动不会清空。公开包默认排除输入、身份及私有结果，只有冻结的公开授权和许可证均存在才导出结果。内部备份包含私有输入和输出，仅能恢复到新的空实例。

## 尚未完成的验收

未实测Linux容器及比赛指定硬件，未建立项目级成员权限与操作系统文件沙箱，未实现ARPACK精确检查点续算、混合精度修正路线、SLEPc/MPI/GPU适配或本地大模型。一般非三对角稀疏目标的完整性仍标记未认证。公开工业电磁矩阵下载受TLS连接问题阻断，未生成其性能成绩。离线安装使用本地无索引依赖，未通过关闭宿主机网络的方式额外测试。

本次交付是有实际数值核心的本地CPU工作台版本。报名、竞赛外部提交、全部66项验收与工业场景有效性均无完成声明。
