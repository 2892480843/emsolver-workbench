# 工业验收档案与证据门禁

本模块将已结束的求解任务、输入矩阵、独立参考、工程声明和验收条件冻结为可追溯档案，再分别判定数值、目标覆盖、来源、审查和性能。**软件流程通过不代表真实器件、官方赛事或任意规模工业模型已经验收通过。** 测试中的授权机构和矩阵均为验证软件流程的夹具。

**本模块的机构签名及审查回执属于可选增强信任策略，不是原比赛强制要求。** 原赛题所要求的数值、目标覆盖、稳定性、资源和复现证据应分别评估。公开来源核查、开发代码审查、独立数值计算和外部机构签字是不同证据，不能互相替代。v0.9已有完整GSM的数值合格候选与ARPACK参考，严格覆盖仍未认证，见[本轮状态](v090-delivery.md)。

## 验收范围

| 字段 | 取值与含义 |
|---|---|
| `declared_use` | `teaching` 教学、`research` 研究、`industrial` 工业用途声明；教学与研究不能取得工业通过状态。 |
| `acceptance_scope` | 默认 `matrix_eigenproblem`，只验收给定矩阵特征值问题；`resonant_frequencies` 额外验收审核映射后的谐振频率。 |
| `engineering_asset_id` | 可为 `null`，允许没有几何映射的大矩阵建立数值档案；频率范围必须提供与作业冻结版本一致的工程侧车。 |
| `physical` | 可为 `null`；可记录方程、材料、边界、假设、网格来源、审查证据与认证回执。缺少审查证据时保持 `UNVERIFIED`。 |
| `reference` | 可为 `null`；来源、原始制品摘要、矩阵与目标摘要、生成方法和独立参考数据共同冻结。 |

矩阵范围的通过仅限声明的矩阵、目标集合、数值与资源条件，不增加电磁器件正确性或频率结论。频率范围要求已冻结的审核公式、工程映射、匹配单位的网格、适用的无损材料与边界声明及独立审查。网格摘要本身只是声明，不能证明网格实际来源或收敛性。

## 操作流程与权限

1. 完成矩阵导入、配置和求解，取得已结束任务的 `job_id`。
2. 读取服务端模板，保留绑定信息，补充用途、范围、物理来源与验收条件。
3. 如需完整参考向量，上传独立的 NumPy 制品；在档案中绑定返回的资产 ID 与 SHA-256（文件内容摘要，用来检测内容是否变化）。
4. 保存档案；后续变更创建新修订，不覆盖旧档案。
5. 执行验收。缺少证据为 `UNVERIFIED`，已发现反例或内容变更为 `FAIL`，全部门禁通过才为 `PASS`。
6. 查看或导出档案、冻结来源及历史检查记录。恢复备份后需重新检查，历史通过不是当前文件和当前信任配置的证明。

项目管理员可以保存、修订和上传参考；分析员与管理员可以执行检查；查看者可以读取和导出。运维角色没有默认业务读取权。归档项目拒绝新增档案、修订和检查。每个服务实例同时只允许一个验收检查，其他检查返回 `409 INDUSTRIAL_CHECK_BUSY`；求解队列使用原有调度策略。

## 接口

| 方法与路径（统一前缀 `/api/v1`） | 行为 |
|---|---|
| `GET /projects/{project_id}/industrial-template?job_id=...` | 返回完整默认 `manifest`、矩阵与目标绑定、JSON Schema（字段与类型说明）及大小上限。 |
| `POST /projects/{project_id}/industrial-cases` | 请求为 `{"manifest": {...}}`；冻结新档案，相同内容返回原档案。 |
| `GET /projects/{project_id}/industrial-cases` | 按项目分页，支持 `limit`、`cursor`。 |
| `GET /industrial-cases/{case_id}` | 返回摘要和完整 `latest_check`。 |
| `GET /industrial-cases/{case_id}/dossier` | 读取冻结声明、绑定与快照。 |
| `POST /industrial-cases/{case_id}/revisions` | 请求为 `{"manifest": {...}}`；创建带父档案与修订号的新版本。 |
| `POST /industrial-cases/{case_id}/checks` | 新检查，返回 `status`、`industrial_accepted`、`acceptance_scope` 和逐项 `gates`。 |
| `GET /industrial-cases/{case_id}/checks` | 最近检查，`limit` 为 1–100。 |
| `GET /industrial-checks/{check_id}` | 读取指定历史检查。 |
| `GET /industrial-cases/{case_id}/report` | 取得受项目权限保护的 JSON 报告；不含服务端绝对路径或信任密钥。 |
| `POST /projects/{project_id}/industrial-reference-assets` | multipart 上传单个 `file`，扩展名为 `.npz`。 |
| `GET /industrial-reference-assets/{asset_id}` | 读取参考摘要、维度和模式数。 |
| `GET /industrial-reference-assets/{asset_id}/download` | 按权限下载已核对完整性的参考制品。 |

独立参考 NPZ 文件严格包含 `eigenvalues.npy` 和 `eigenvectors.npy` 两个成员，例如：

```python
import numpy as np

# 来自独立验证流程，不能从候选任务的输出复制后称为独立真值。
np.savez("independent-reference.npz", eigenvalues=values, eigenvectors=vectors)
```

档案中的外置参考字段示例（其余字段使用服务端模板与 Schema）：

```json
{
  "matrix_sha256": "矩阵的64位十六进制摘要",
  "target_sha256": "模板返回的目标摘要",
  "method": "independent_solver",
  "producer": "独立机构、工具及版本",
  "source": {"title": "独立参考来源", "url": "https://example.org/reference"},
  "artifact_sha256": "上传资产返回的sha256",
  "artifact_id": "上传资产返回的ir_标识",
  "eigenvalues": null,
  "vectors_real": null,
  "vectors_imag": null,
  "attestation": null
}
```

该示例中的中文摘要说明需要换成实际值。也可以内联小参考的 `eigenvalues`、`vectors_real`、可选 `vectors_imag`，此时 `artifact_id` 为 `null`。两种形式互斥。

## 独立机构回执与信任边界

普通来源声明不会自动通过独立参考与物理审查门禁。系统支持可选的 HMAC-SHA256（共享密钥消息认证码，用于核对回执是否由配置的机构签发）回执，字段为 `issuer` 与 `signature`。**它认证配置机构的断言，不证明断言在物理上为真，也不是具有公开验证能力或不可抵赖性的数字签名。** 独立机构应自行完成参考质量、目标覆盖、工程来源和适用条件审查；不能用候选求解器自产答案替代。

实例所有者可设置 `EMSOLVER_INDUSTRIAL_ATTESTATION_KEYS_FILE`，指向业务数据根目录外的 UTF-8 JSON 文件：

```json
{
  "keys": [
    {
      "issuer": "independent-lab",
      "key_hex": "由安全随机源生成的至少64位十六进制密钥",
      "claims": ["reference", "physical_review"]
    }
  ]
}
```

将示例中的密钥说明换成实际随机密钥。密钥文件必须属于当前服务用户、权限为 `0600` 或 `0400`，文件与父路径不能是符号链接，实际位置不能在业务数据根内。密钥不写入档案、工作进程环境或备份。外部机构与验证端共享的密钥需要另行安全管理；系统不主动向候选任务传递密钥。本机普通工作进程没有文件系统沙箱，不能据此承诺同用户恶意程序无法读取文件。

签发端使用 `industrial_attestation.attestation_message(binding, purpose)` 的原始字节计算 `hmac.new(key, message, hashlib.sha256).hexdigest()`；`purpose` 分别为 `reference`、`physical_review`。该辅助函数仅生成待认证消息，系统没有签发回执或创建信任机构的 HTTP 接口。

回执绑定矩阵、目标、候选任务、基线任务、用途、验收范围、工程摘要、物理证据摘要以及参考资产 ID/摘要。修改其中任意项需要重新签发。实际回执本身从摘要计算中剔除，避免循环依赖。可先保存无回执档案取得完整 `binding`，再由外部机构签发并创建新档案或修订。

未配置机构、机构用途不匹配、密钥不可用等情况保持 `UNVERIFIED`；已配置机构的认证码不匹配为 `FAIL`。撤销机构后新检查会重新使用当前配置，历史检查原样保留。

## 门禁与资源界限

| 门禁 | 判定依据 |
|---|---|
| `declared_use` | 工业用途明确声明；教学、研究保持未验证。 |
| `frozen_binding` | 当前任务与冻结快照一致；所用工程侧车存在且内容摘要、项目与矩阵绑定一致。 |
| `artifacts` | 原矩阵与发布结果哈希在检查前后均匹配冻结值。 |
| `residual` | 独立子进程重新计算原问题残差，达到档案门槛；不只信任原作业状态。 |
| `target_coverage` | 独立目标认证与成功终态；仅残差小不能替代目标完整性。 |
| `physical_review` | 适用范围内的来源与审查证据，加配置机构认证回执；教学模型不冒充工业来源。 |
| `reference_provenance` | 参考绑定与独立机构回执；候选任务标识或其已知制品摘要不能用作独立来源。 |
| `reference_comparison` | 实际参考文件与候选文件计算特征值匹配及重根子空间误差，不逐列强行匹配重根向量。 |
| `frequency` | 仅频率范围比较已审核映射后的实际文件值；矩阵范围明确记录没有提出频率结论。 |
| `performance` | 仅验收声明的耗时、采样内存与加速比目标；加速比要求不同任务、同输入/目标/精度/预算/环境且基线重新独立认证。 |

内联请求最大 1 MiB、参考最多 32 个模式与 50,000 个向量元素；外置 NPZ 上传与展开各最多 128 MiB、最多 2,000,000 行、32 模式、8,000,000 个向量元素。完整向量必须保留全部原自由度行，不能通过抽样来伪装完整参考。

外置参考校验与比较在独立进程中进行，最多 2 GiB、30 秒、单线程。内联比较最多 512 MiB、10 秒。原问题独立复验沿用最多 2 GiB、30 秒的受限进程；超出当前支持预算时保留未验证。检查侧车最大 512 KiB，档案侧车最大 4 MiB。

参考只在候选运行结束后由验收模块读取，不传入求解配置或工作进程。普通本地工作进程不是操作系统文件沙箱；需要防恶意代码读取参考时，应使用已有受限容器执行路径，并在独立评测侧保管参考。成功的有界校验不证明任意矩阵都满足时间、内存或目标认证条件。

## 持久化与恢复

档案、检查与参考制品位于数据根的 `industrial/`，按内容摘要寻址并设为只读；实体分别为 `industrial_cases`、`industrial_checks`、`industrial_reference_assets`。使用现有实体表，不修改数据库结构。保存时沿用项目配额和审计。

私有备份包含这些制品与元数据。恢复时校验大小、摘要、闭合字段、项目/矩阵/任务/工程/参考/父版本绑定，并根据完整门禁重算历史总体状态，不能只信任 `industrial_accepted` 布尔值。显式降级恢复的缺失制品标为 `MISSING`，后续不能凭历史摘要取得新通过。备份的内容摘要用于一致性检测，不应当作可信机构认证或新的数值证明。

## 验证

```bash
.venv-emsolver/bin/python -m pytest tests/test_industrial.py tests/test_industrial_reference.py -q
```

专项测试覆盖未签发回执的未验证状态、测试机构回执、跨档案移植拒绝、错误参考子空间、权限、篡改、缺失、只读修订、接口预算、恢复状态校验，以及 50,001 行完整参考矩阵流程。该大向量案例是对角测试夹具，只证明软件支持路径，不是实际工业器件正向证据。
