# Metal 混合修正 profile

`metal_jd_correction_v1` 使用可选 MLX（Apple 设备数值计算库）的真实 Metal GPU 执行 JD 内层近似修正的稀疏矩阵向量乘。仅支持 macOS arm64、实际可用的 Apple Metal、标准厄密最近 k、单秩、`jacobi_davidson/precond/gmres/mixed_float32`。无 MLX 或设备不能实际完成探测时拒绝。

矩阵采用 COO（行号、列号和存储值）gather/scatter，内存随非零项数和阶数增长，不生成完整稠密矩阵。复数乘法由实、虚部单精度乘加组成。外层 Ritz、预条件投影及每次修正的权威真实残差仍由 CPU float64/complex128 执行；单精度结果不合格时提升64位重算，最终原问题残差、正交性和目标认证不放宽。

实际参与求解的 GPU 稀疏乘次数记录为 `gpu_matvec_calls`；另报耗时、稀疏项数、实际 MLX 峰值和明确的混合执行范围。配置选择 GPU、探测或分配过 GPU 内存，均不能证明 GPU 参与了候选计算：实际调用为0时策略保留 `requested_device=gpu`，将 `compute_device` 记为 `cpu` 并解释原因。要求更严格的内层容差时全部走 CPU64 是合法的保守行为。

MLX 官方本地接口说明明确 `set_memory_limit` 是内存建议值，不能当作硬分配上限。实现先按 `nnz*40+n*64` 情景检查预算，再分配 COO；实际每次求值后核验 MLX active/peak，用量超过配置就终止并拒绝发布成功产物。普通进程树预算也继续执行。一次求值中的暂态峰值不能承诺硬实时阻断，指标 `gpu_memory_enforcement` 明确记录“MLX建议值加求值后实际分配守卫”。

实际验证命令：

```bash
.venv-emsolver-model/bin/python -m pytest tests/test_gpu_metal.py -q
```

当前已实际通过9项：40阶实数/复数稀疏乘对照 CPU 独立结果，真实 JD 修正具有正 GPU 调用与单精度求解次数、最终原问题64位残差通过，以及在 COO 分配前拒绝低预算、actual/peak 超限拒绝。普通未安装 MLX 的 `.venv-emsolver` 环境对应4项硬件实测明确跳过，不能以跳过宣称 GPU 验收通过。

这些小型数值测试证明路径真实参与及验收边界，**不构成加速比、任意规模性能、GPU双精度、CUDA或完整GPU求解器的证明**。可复现环境和准确测试数量见 [数值证据](evidence/v050-numerical-summary.json)。
