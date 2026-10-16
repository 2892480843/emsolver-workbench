# 冻结评测协议与材料检查

本模块支撑 F49、F52、F59：先冻结开发与保留测试输入、配置和来源，再执行全部规定行；材料检查验证必要的语义和最小运行闭环，不把文件存在当成软件全部验收通过。

## 冻结协议

示例规格如下。开发集和保留集必须显式声明；相同输入字节哈希不能跨两种划分出现。

```yaml
schema_version: '1.0'
repeats: 3
base_config:
  target: {k: 4, sigma: 0.1}
  solver: {method: block_shift_invert, subspace_max: 30}
  resources: {threads: 1, memory_limit_gib: 16, walltime_seconds: 120}
cases:
  - id: development-complex
    split: development
    input: {kind: complex, n: 100, seed: 20261016}
  - id: holdout-complex
    split: holdout
    input: {kind: complex, n: 100, seed: 20261017}
variants:
  - id: baseline
  - id: no-adaptive-shift
    ablation: adaptive_shift
  - id: no-bounded-restart
    ablation: subspace_control
  - id: no-preconditioning
    ablation: preconditioning
  - id: no-mixed-precision
    ablation: mixed_precision
```

也可使用 `input: {path: authorized-input.npz}` 登记已有输入，支持 `.npz` 和 `.npy`，拒绝符号链接与超过 1 GiB 的文件。生成输入保存种子和合成来源；不会将其写成真实工业数据。规格支持每个用例单独的 `config`、`repeats`、`expected_status` 和 `expected_error_code`。输入划分的字节哈希不重叠，只说明这份冻结记录未复用相同文件内容；不等于通过了独立机构的隐藏测试。

```sh
.venv-emsolver/bin/python scripts/run_protocol.py freeze --spec protocol.yaml --output runs/frozen-protocol
.venv-emsolver/bin/python scripts/run_protocol.py execute --plan runs/frozen-protocol/protocol.json --output runs/protocol-results --expected-hash PRINTED_HASH
```

输出目录必须全新。冻结创建只读输入副本及 `protocol.json`，记录输入、每行完整配置、配置哈希、开发／保留划分、重复次数、能力清单、源码与环境哈希、实际加载 Python 包代码文件哈希及整个协议哈希。`--expected-hash` 可与事先另存的冻结值比对；内容摘要不是数字签名，不能替代对发布者身份的认证。

执行前重新检查协议、输入、配置、源码、实际运行代码与环境哈希。源代码或依赖更新后应新建协议版本，不能继续声称使用旧冻结环境。开发期间不应先生成最终评测证据再修改代码；最终证据须在实现和审查结束后生成。

Python 接口为：

```python
from emsolver.protocols import freeze_protocol, execute_protocol

plan = freeze_protocol(spec, frozen_directory, source_root=None)
report = execute_protocol(plan_path, new_result_directory,
                          source_root=None, expected_plan_hash=plan['hash'])
```

`source_root` 用于额外登记指定源码树；无论该参数如何设置，执行包自身的 Python 文件内容仍会单独记录和核对。算法选择只由完整配置与后端能力决定。用例 ID、输入文件名和路径只用于展示与定位，不映射到策略或答案表。

## 真实执行与完整分母

每个可执行行调用已有 `execute_local`，因此沿用独立进程、内存／时间／磁盘限制、原问题验证及真实产物。执行目录按数字行号生成，不用用例名称选择算法。异常转为失败行后继续后续行；超时和失败不从统计中移除。

结果在首个执行之前写入所有规定行，尚未执行的行标记 `NOT_RUN`。每次完成后更新 `results.json`；完整执行还产生 `report.html`。结果保留每行状态、实际指标、数值验证、配置哈希、阻断原因和预期是否满足。没有独立参考时 `reference_error=null`、`reference_certification='NOT_AVAILABLE'`，不会伪造参考误差。

汇总分别列出：

- `prescribed_runs`：协议规定的总行数，含阻断与未运行。
- `executed_runs`、`blocked_runs`、`failed_runs`、`timeout_runs` 及各状态计数。
- `failed_or_timeout_rate_prescribed` 与 `failed_or_timeout_rate_executed`：明确区分全部规定行和实际执行行两个分母。
- `expectations_met`：符合预先声明预期的行数；预期失败也仍然是实际失败，不能改写成数值成功。

协议执行不自动宣称公平加速。策略变体必须保留同一用例的输入、目标、精度、资源与输出要求；需要按已认证结果和相同环境另行比较性能。异常退出时已有 `NOT_RUN` 行保留原规定分母。

## 消融边界

消融（一次关闭一个策略来检查其作用）必须标明真正关闭的行为：

| 名称 | 当前实现 |
| --- | --- |
| `adaptive_shift` | ARPACK／块位移反演路线仅设置 `solver.adaptive_shift.enabled=false`，关闭有界工作位移恢复；JD 中不适用并标记 `BLOCKED`，其 `sigma` 仅用于 Ritz 目标 |
| `subspace_control` | 在 `block_shift_invert` 基线或能力清单明确声明有界重启的 JD 基线中设置 `solver.block.max_restarts=0`；保留 `subspace_max` 硬上限，不放开内存 |
| `preconditioning` | 仅当 JD 能力明确登记 `none/jacobi`，且基线确实启用 `jacobi` 时，改为 `solver.linear.preconditioner=none`；否则 `BLOCKED` |
| `mixed_precision` | 仅当 JD 能力明确登记混合精度，且基线确实启用 `mixed_float32` 时，改为 `solver.linear.precision=float64`；否则 `BLOCKED` |

JD（Jacobi–Davidson，一种通过近似修正方程改进候选子空间的方法）的预处理、混合精度与重启消融受实际后端能力门禁控制，字段存在不等于实现已验证。基线已经关闭对应行为时，同样阻断无意义的“再次关闭”。命名消融不能夹带迭代次数或其他算法修改；通用基线比较可使用 `variants[].config` 明确提供配置，但不得冒称单因素贡献。请求的方法或后端未在当前能力清单验证时，协议保留 `BLOCKED` 行，不能以其他算法的成功代替。

上面的块位移反演示例会将其不具备的预处理／混合精度开关标记阻断。要对已登记能力的 JD 进行这些真实对照，可将基线求解配置改为：

```yaml
solver:
  method: jacobi_davidson
  transform: precond
  subspace_max: 30
  linear:
    method: gmres
    preconditioner: jacobi
    precision: mixed_float32
  block:
    max_restarts: 2
```

协议会分别生成 `none`、`float64`、`max_restarts=0` 的实际配置，其余字段保持不变；是否成功仍由真实求解与独立验证决定，不能由开关名称预先判定。

工作位移对照与 JD 对照应分别建立适用的协议：前者使用 ARPACK／块位移反演的 `adaptive_shift`；后者只包含真正生效的预处理、混合精度和有界重启开关。不能在 JD 中关闭一个无效标志后把结果计作自适应位移消融已执行；保留该行时必须显示不适用的 `BLOCKED`。

若协议包含阻断或未满足预期的行，执行脚本退出 2；这表示该协议不能报告全部对照已完成，不代表已经执行的成功结果被删除。新增后端或策略开关后，应重新冻结明确的新协议并实测。

## 材料语义检查

```sh
.venv-emsolver/bin/python scripts/check_delivery.py --require-build
```

保留原 `inspect(root, require_build=False)` 接口和 `source_delivery_complete` 字段，增加以下核对：

- 必需源码存在、UTF-8 可解析，关键函数不能只有空体或常量占位；核心 Python 文件进行语法树检查。
- 启动、演示和离线准备脚本通过 `bash -n`，并包含实际流程所需命令；CLI 子命令必须存在于交付解析器。
- `pyproject.toml` 的构建与 CLI 入口有效，示例配置通过共享配置模型，合成基准规格字段有效。
- 通过交付目录中的 CLI 实际生成并检查 20 阶样例，再使用默认配置求解，要求原问题数值验证通过。只写“样例可生成”不会直接计为该检查成功。
- README、许可证和依赖说明具有必要内容。直接 Python 与前端运行依赖必须登记名称、精确锁定版本、许可证元数据及来源 URL；这不替代完整法律审查，也不代表自动批准商业分发。
- F01—F66 必须恰好各一条，状态、范围、限制和证据齐全；证据文件必须存在，引用的测试／函数符号必须能在对应 Python 语法树中定位。
- 已提供或要求构建界面时，入口引用的本地脚本、样式与样式内资源必须存在且保持在构建目录内。

每项输出 `category`、`path`、`passed` 和必要原因。任何核心缺失、占位脚本、坏配置、样例运行失败、伪造证据或缺失资源都会阻断 `source_delivery_complete`。检查不联网，不执行离线下载脚本，不读取参考答案。

无论材料检查结果如何，`functional_acceptance` 固定为 `NOT_CERTIFIED`，提示材料的有限语义检查不能替代全部 PRD、目标平台、外部后端或参赛审核验收。

## 验证

```sh
.venv-emsolver/bin/python -m pytest tests/test_protocols.py tests/test_delivery.py -q
```

本地 macOS arm64 测试使用真实隔离求解，覆盖冻结划分、全量失败／超时分母、不可变输出、篡改与环境来源变化、名称无关性、消融单因素，以及空脚本、坏配置、缺少生成器、假证据、无效许可证清单和坏构建资源等负例。最终评测运行需要在源码、依赖与环境稳定后另行冻结；本文不包含虚构的最终性能记录。
