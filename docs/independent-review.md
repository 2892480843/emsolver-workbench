# EMSolver 独立正确性审查

审查日期：2026-10-16。已通过 `uname -s` 确认 macOS（Darwin），解释器 `.venv-emsolver/bin/python`。本记录只代表本地代码审查和下述定向实证，不代表 Linux 比赛环境或全部 PRD 已验收。没有联网、读取参考答案供求解器使用、运行未授权外部消息或修改数值核心。

## 已确认问题与处理

| 编号 | 优先级 | 位置 | 确认现象 | 处理状态 |
|---|---|---|---|---|
| R1 | P1 | `emsolver/cli.py:main(validate)` | 数值 PASSED、目标 UNVERIFIED 的 PARTIAL 运行，validate 仍 exit 0 | 主开发已修改为 4 |
| R2 | P1 | `emsolver/cli.py:main` 服务命令 | 仅运行 jobs status 就启动队列，并在 close 时把原 QUEUED 任务改成 FAILED/WORKER_LOST | 主开发已限制仅 jobs wait 启动执行器，并调整非执行器实例恢复行为 |
| R3 | P1 | `emsolver/numerics/core.py:_target_order` | `diag([0,1,2,3]), sigma=1e20, k=1` 返回 0 且 SUCCEEDED/CERTIFIED；正确最近值为 3 | 数值开发已加入精确距离参考与同侧单调排序 |
| R4 | P1 | `emsolver/service.py:restart_job` | submit 返回 QUEUED 快照后工作线程可已完成；再保存 parent_job_id 把终态覆盖为 QUEUED | 主开发已改为 submit 初次事务写入 parent_job_id，并补回归 |
| R5 | P1 | `emsolver/numerics/core.py:_certify_tridiagonal` | 切簇容差 precision*8 与匹配容差 precision*16 不一致，错误近邻值可通过 | 数值开发已加局部谱间隙拒绝，并完成 R10 补充 |
| R6 | P1 | `emsolver/reporting.py:build_bundle`、`service.py:create_bundle` | 默认公开包仅排除输入矩阵，私有任务完整向量和特征值仍进入包 | 主开发已基于冻结 public 与 license 授权过滤结果、向量、验证及事件数据，并补2项回归 |
| R7 | P1 | `emsolver/numerics/core.py:_validate` | 极小残差的二范数平方下溢为 0，绕过 absolute_residual_max | 数值开发已加原单位稳定范数 |
| R8 | P2 | `emsolver/execution.py:execute_local`、`cli.py:main(report)` | 输入错误或超时发生在数值导出前，没有冻结配置，失败报告不可生成；缺输入 exit9 | 本审查者已修复并测试 |
| R9 | P1 | `emsolver/cli.py:_reproduce` | checksum 清单漏列成员、列不存在成员、重复 ZIP 成员仍能进入复现 | 本审查者已修复并测试 |
| R10 | P1 | `emsolver/numerics/core.py:_certify_tridiagonal` | 正确目标数值搭配错误邻居向量，残差通过后仍被认证；认证不接收向量 | 数值开发已加入向量残差/谱分离约束；审查者独立复核6种正确与错误向量组合通过 |

## 关键数值复现

```python
import numpy as np
from scipy import sparse
from emsolver.numerics.core import _validate, _certify
from emsolver.contracts import resolve_config

# R5: 真最近值是0，提供3e-13相邻模式，原实现仍CERTIFIED。
a = sparse.block_diag([
    sparse.csr_matrix([[0.]]), sparse.csr_matrix([[3e-13]]),
    sparse.csr_matrix([[1., .01], [.01, 2.]])
], format='csr')
x = np.array([[0.], [1.], [0.], [0.]])
cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
v, x = _validate(a, np.array([3e-13]), x, cfg)
v.update(_certify(a, np.array([3e-13]), x, cfg))
print(v)

# R10: 将第二个对角值改为1e-9，仍用x=e2，但谎报lambda=0。
# 原问题缩放残差约4.975e-10，在1e-8标准内。
# 真目标零模式子空间是e1，与候选完全正交。
# 初次R5修复后仍CERTIFIED，因为证书只核值，且错误邻居位于窗口外。

# R7:
a = sparse.diags([1e-200, 1.], format='csr')
cfg = resolve_config({'target': {'k': 1}, 'validation': {'absolute_residual_max': 1e-210}})
print(_validate(a, np.array([0.]), np.array([[1.], [0.]]), cfg)[0])
# 修复前绝对残差为0且PASSED；真实绝对残差1e-200，应FAILED。
```

R10 的认证修复需考虑窗口外模式：伪候选值 0 使窗口半径约为 `margin=1e-11`，1e-9 近邻根不在 full_window 中，不能仅用窗内邻根间隙。需要结合窗口端点距离或外侧根提供谱分离下界，再用实际残差与该间隙限制候选子空间误差。

最终复核：新实现已使用窗内不同根间隔和计数窗端点距离的较小者，减去参考及候选值不确定度，再约束真实候选向量残差与分离下界之比。针对 gap=1e-10、1e-9、1e-8，真零向量 e1 均 CERTIFIED，错误邻居向量 e2 均 UNVERIFIED(mode_subspace_not_verified)。本轮审查问题已关闭；最终完整测试、构建与界面验证由主开发统一执行。

## 本审查者直接修改与验证

修改文件仅：`emsolver/execution.py`、`emsolver/cli.py`、`tests/test_cli_failure_regressions.py`。

- 在数值子进程结束后补存未发布运行的冻结配置，避免提前写入导致“输出目录必须为空”冲突。
- 失败 JSON 缺少验证结果时明确 `NOT_VALIDATED/UNVERIFIED`。
- 缺输入、非法格式与结构等错误映射 exit 2。
- 历史失败运行缺配置时生成报告并标明 `UNAVAILABLE`，不编造默认配置。
- 复现 ZIP 拒绝重复成员，要求 checksum 覆盖恰好等于除清单自身外全部文件，流式重算哈希；不执行包中的 Python 源码。

先运行新增测试观察实际红灯：6 failed、2 passed，分别命中错误码、缺配置、checksum 漏列/缺失/重复问题。修复后运行：

```shell
.venv-emsolver/bin/python -m pytest tests/test_cli_failure_regressions.py tests/test_cli.py tests/test_cli_integration.py tests/test_reporting.py -q
```

结果：18 passed。包含真实子进程缺输入、真实短预算超时、历史失败报告、完整与损坏复现包，以及已发布成功产物哈希不被父进程破坏的回归。

## 范围与尚不可宣称全 PRD 验收的部分

- 普通非三对角稀疏矩阵保留 UNVERIFIED/PARTIAL 符合 PRD 10.3 明确边界，本审查不把诚实降级本身当作缺陷。
- 当前可选 MPI/GPU/SLEPc 明确不可用，未把安装探测等同于适配器实现。
- F50 要求操作系统层参考访问隔离与访问审计；当前工作进程仍继承用户文件权限，文档已披露无文件沙箱，不能将“未给参考路径”称为完成 F50。
- 完整 PRD 还包含检查点续算、项目成员权限、可取消长导入、观测开销实验、真实电磁数据登记与完整验收证据；应逐项按实际状态披露，不能以当前通过的自动测试替代66项验收。
- 此次检查未发现原矩阵 `toarray/todense` 路径；基向量块和小投影矩阵与原始 n×n 稠密化应区分。未运行正式 Linux 内存基准、真实公开电磁求解或最终比赛提交。
