# v0.3交付验证

本次为本地macOS arm64/Python3.12验证，数据库仍为schema2；数值内核与迭代前快照哈希一致。

| 验收 | 结果 | 证据 |
|---|---|---|
| 完整后端回归 | 268通过；1项既有Starlette弃用警告 | `experiment-tests.log` |
| 前端模型及构建 | 30通过；生产构建通过 | `experiment-web-tests.log`、`experiment-build.log` |
| 浏览器真实实验 | 4组×2次，8次全部认证；重复提交无新增任务 | `experiment-runtime-verification.json` |
| 浏览器取消 | 20次运行中5次完成、15次取消；所有行保留 | 同上 |
| 终态详情请求 | 79秒观察窗口内新增0次 | 同上 |
| 既有数据 | 28条实体正文、84条事件逐条一致 | 同上 |
| 新独立环境 | 本地wheel以`--no-index`安装，4组全部读回通过；恢复后幂等关联正确 | `experiment-offline-verification.json` |
| 独立审查 | 重要问题已修补，无剩余Critical/Important发现 | `experiment-review.md` |

完整运行命令：

```bash
.venv-emsolver/bin/python -m pytest -q
npm --prefix web test
npm --prefix web run build
.venv-emsolver/bin/python scripts/check_delivery.py --require-build
```

新能力的源码、旧接口和旧备份兼容性由测试覆盖；F18已形成完整的有界参数实验闭环。全部66项PRD仍登记为16项已实现范围、40项部分实现、10项条件性或未实现，不能把本轮开发等同于整个赛题验收完成。

Linux实机、物理断网、真实工业数据性能、多用户角色、精确续算及MPI/GPU仍未验收。预算是硬限额总和，不能解释成耗时预测。浏览器下载事件工具未返回保存路径，报告内容与服务端下载响应已验证。
