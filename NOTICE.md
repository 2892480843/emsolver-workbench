# 依赖与数据说明

项目自有代码采用MIT许可证。Python依赖由`pyproject.toml`和`requirements.lock`声明，前端由`web/package.json`和`web/package-lock.json`声明。实际已安装版本和发行包许可证元数据登记在`docs/dependency-inventory.json`；分发时应保留依赖自带的许可证和说明，尤其是NumPy/SciPy发行包中数值库的授权文件。

数值算法使用SciPy的ARPACK与稀疏LU实现；本项目实现的是安全输入、策略控制、自适应工作位移、结构认证、资源执行与独立验证，不将第三方数值内核冒称全部自研。

生成样例是带种子的合成稀疏数据，不能替代真实电磁工程验证。SuiteSparse候选矩阵的原作者与集合许可应随下载包保存；网站软件许可和矩阵许可不同，不自动继承本项目MIT许可证。私有用户输入不会因导入本系统而改变权属或自动公开。
# 前端第三方许可随附说明

参赛介绍 PDF 的字体子集使用 Noto Serif SC（SIL Open Font License 1.1），许可原文见 `docs/third-party-licenses/NotoSerifSC-OFL.txt`。原始字体不随代码仓库分发；文档构建说明记录其固定来源及摘要。

多人部署的 PostgreSQL 驱动由 `server` 可选依赖提供，远程节点的 HTTP 客户端由 `worker` 可选依赖提供，精确版本登记在 `requirements-cluster.lock`。Psycopg 及二进制发行包的许可原文随附在 `docs/third-party-licenses/`，容器保留安装包自带的许可文件。

构建界面包含 React、React DOM 与 Lucide React。其版权及许可原文随交付保存在 `docs/third-party-licenses/`；版本与来源见 `docs/dependency-inventory.json`。Python 离线 wheels 和已安装依赖保留各自发行包的许可文件。本说明不替代对数据、模型或额外依赖的授权核实。
