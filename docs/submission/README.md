# 参赛交付说明

本目录为中国商飞 06 赛题的提交材料入口。代码仓库为 https://github.com/2892480843/emsolver-workbench 。

| 材料 | 文件 |
|---|---|
| 作品方案 PDF（15 页） | [EMSolver-project-introduction.pdf](EMSolver-project-introduction.pdf) |
| 同源可编辑介绍 | [作品方案介绍.md](作品方案介绍.md)、[project-introduction.json](project-introduction.json) |
| 功能状态与缺口 | [功能完成度与未完成项.md](功能完成度与未完成项.md)、[requirements-status.json](requirements-status.json) |
| 既有验收摘要 | [verification-summary.json](verification-summary.json) |
| 干净源码复验 | [source-checkout-verification.json](source-checkout-verification.json) |
| 发布内容审查 | [publication-audit.json](publication-audit.json) |

## 四项提交要求的对应

1. **完整方案及源码**：PDF 包含背景、目标、场景、技术架构、数据模型、数值路线、创新、运行、验证与缺口；根目录提供原 PRD。Python 后端、React 前端、CLI、样例、测试、依赖锁定与部署脚本均在仓库。
2. **原创与第三方归属**：自有代码采用 MIT，第三方依赖保留各自许可与来源。未上传企业原题 PDF、真实业务库、模型权重或许可未明确的大型输入。数值底层库不冒称自研。报名人的最终法律承诺不由自动化工具代签。
3. **可运行与一致性**：基础 CPU/SQLite 路径无需 API 密钥，运行命令见根 README；当前与设计对应的 66 项逐条保留。62 项声明范围内闭环、4 项部分实现或条件未闭环，部分项不计完成；完整闭环比例 93.94%，待闭环项占比 6.06%。不把该比例解释为代码覆盖率、工业通过率或官方评分。
4. **清晰注释**：核心模块使用职责说明，关键控制点解释并发锁、数值适用前提、资源界限和恢复条件。代码、文档和测试入口互相映射，不使用空实现或 UI 演示替代算法。

## 复现顺序

先按根 README 安装 Python 和前端依赖、构建并运行小型 CPU 示例。再执行 `python -m pytest -q`、`npm test --prefix web` 和 `python scripts/check_delivery.py --require-build`。只有显式配置独立测试库的 `EMSOLVER_TEST_DATABASE_URL` 时才运行真实 PostgreSQL 用例；不要指向生产库。

本次源码不附带旧离线二进制归档。第一次从源码安装通常需要网络；求解本身不依赖外网或大模型。更改平台后需重新验证可选硬件路径。当前官方比赛环境、多物理主机 HTTPS 与工业目标严格覆盖不在已通过结论中。

## 2026-10-16 修订版

从 Git 暂存源码导出的独立目录复验：后端 1074 项通过、23 项跳过，前端 79 项通过并完成生产构建；具体命令和范围见 `source-checkout-verification.json`。

修订版将后端、前端新建草稿和默认样例的线程预算统一为 1，使单核机器可以使用默认规划与基准流程；显式超额预算仍被拒绝。补充单核预算回归测试。另修复 PostgreSQL 对已退出请求线程的会话回收；以真实数据库会话计数验证线程轮换不会持续累积连接，并保留并发事务隔离。介绍中的 `scaled_inf_v1` 公式与代码统一使用无穷范数，绝对二范数门槛单独说明。

旧 `submission-2026-10-16` 发布记录保留；修订交付使用 `submission-2026-10-16-v2`。GitHub Linux 检查结果以该提交对应的 Actions 页面为准，本机结果不代替目标平台验证。

## 重建介绍 PDF

运行应用不需要 PDF 构建依赖。重新编辑介绍时，安装 `reportlab` 和 `pillow`，向脚本传入合法 TrueType 中文字体：

```bash
python scripts/build_submission_pdf.py \
  --font /path/to/licensed-chinese-font.ttf
```

同一 JSON 生成 Markdown 和 `output/pdf/EMSolver-project-introduction.pdf`；经渲染检查后复制至本目录。渲染字体为 Google Fonts 的 Noto Serif SC，固定来源：

`https://raw.githubusercontent.com/google/fonts/bf7c667ae010c0ffb39944ae85097021478cb91d/ofl/notoserifsc/NotoSerifSC%5Bwght%5D.ttf`

其 SIL Open Font License 1.1 原文随附在 `docs/third-party-licenses/NotoSerifSC-OFL.txt`。字体仅嵌入 PDF 字形子集，原字体不入 Git。介绍内容与功能状态修改后必须同步复查计数、范围和证据，不能保留过期完成率。

## 隐私及历史证据

公开源码不包括 `.env`、节点令牌、身份配置、数据库连接串、真实业务数据、进程环境和备份。测试中的固定模拟凭据和 CI 临时数据库配置仅用于独立测试。历史文档可能保留本机 `artifacts/` 记录位置，它们是归档标识，不保证存在于公开仓库；公开验收应使用本目录摘要与仓库内测试重新执行。原始大型数据按其来源与许可证合法获取。
