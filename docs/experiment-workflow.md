# 参数实验操作说明

适用：EMSolver v0.3，本地单工作进程。k表示要找的模式数量；σ表示目标附近的特征值位置；参数轴是一组待测试的值，各轴组合形成独立计算组。自适应位移是分解遇到困难时有限重试工作位置的策略，目标位置保持不变。

## 工作台

1. 在矩阵库导入合法输入，或生成明确标记来源的合成样例；选择项目范围。
2. 在“求解配置”设置验收标准、单次时间和磁盘限制等固定条件。
3. 进入“基准实验”，选择同项目矩阵，填入逗号分隔的k、σ、线程和内存限制值，选择自适应位移开关以及重复次数。
4. 检查总预算后冻结方案。预览列出规范化组合、实际计划哈希及相对每个矩阵首组的字段差异；修改表单后需重新冻结。
5. 明确提交冻结方案，系统原子创建全部任务。同一方案重复提交不会增加第二批任务。
6. 选择批次查看分组结果及所有运行，打开任意任务核对原问题验证。批次取消只作用于未结束任务；报告保留成功、部分、失败、取消及超时行。

单次时间和磁盘限制来自当前固定配置，实验总预算是这些限制乘以总运行数。内存为单工作进程最大配置限额，不是系统总内存预测。各轴最多10个值，自适应开关最多2个；矩阵最多30个，总运行最多100次。实际数值后端的更小能力上限仍会在冻结阶段拒绝。

## 命令行

复制`examples/experiment.yaml`，将项目和矩阵ID替换为本实例实际ID。固定配置、规格与UI共用服务端校验。

```bash
.venv-emsolver/bin/emsolver --data-dir data experiments plan --spec examples/experiment.yaml --json
.venv-emsolver/bin/emsolver --data-dir data experiments submit --plan ep_REPLACE_WITH_REAL_ID --json
.venv-emsolver/bin/emsolver --data-dir data experiments status --batch b_REPLACE_WITH_REAL_ID --json
.venv-emsolver/bin/emsolver --data-dir data experiments list --limit 20 --json
.venv-emsolver/bin/emsolver --data-dir data experiments cancel --batch b_REPLACE_WITH_REAL_ID --json
```

这些短命令不会启动数值工作进程。入队后通过正在运行的工作台服务或`jobs wait --job JOB_ID`执行队列；退出0表示命令完成，不等于所有子任务已通过。

## 解释结果

- 总运行分母包括仍在排队、运行及所有终态，不能只挑成功样本。
- `timing_denominator`表示有合法耗时记录的运行数；`certified_timing_denominator`只包括已认证成功且有合法耗时的运行数。
- 参数、目标或资源预算不同的分组不能直接当作公平加速结果；现有运行对比仍要求相同输入、目标、精度、资源及环境，并且均认证成功。
- 自动开关实验只覆盖已实现的自适应工作位移策略；混合精度、GPU、MPI和多预处理器不因本功能而成为可用算法。
- 建议采用后显示字段差异并保存新的配置及来源关联，长期效果追踪账本尚未实现。
