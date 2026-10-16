# 智能诊断、建议追踪与复现来源

本文件说明 F02、F40、F53—F58 的新增独立后端模块。计算与验证仍使用原有求解接口；人工智能解释不具有修改数值状态、配置或验收结果的权限。

## 规则与本地知识

`emsolver.knowledge` 提供以下函数：

| 调用 | 返回与边界 |
| --- | --- |
| `list_knowledge(query=None)` | 按文字过滤的独立条目副本；查询不超过 200 字符 |
| `get_knowledge(entry_id)` | 单条知识；未知 ID 抛出 `KeyError` |
| `diagnose(job=None, config=None, *, matrix=None, capabilities=None)` | 规则诊断；不读取矩阵值、私有文件或参考答案 |

条目包含 `id`、`version`、`registry_version`、`title`、`summary`、`prerequisites`、`risks`、`sources`。每个出处记录 `title`、`url`、`version`、`accessed_at`；版本描述对应所审阅的上游文档，不意味着本地安装了该版本。

诊断返回 `source='rules'`、`rule_version`、`registry_version`、`suggestions`、`rules`、`knowledge`、`ai_available`。每条建议包含 `id`、`rule_id`、`title`、`reason`、`changes`、`evidence`、`knowledge_ids`、`prerequisites`。每条规则均记录 `triggered` 的真假和实际观察证据，未知值保留空值，不虚构测试结果。

故障覆盖非厄密、近奇异、内层不收敛、子空间不足、重根退化、资源不足、超时、后端缺失，以及目标未认证和工作进程丢失。失败尝试的结构化错误码也可触发诊断。涉及 LOBPCG（同时处理多个候选向量的迭代方法）、PML（吸收出射波的人工边界）和广义问题时，前提会出现在知识条目与建议中；这些提示不会自动启用未实现的后端。

技术边界根据 [SciPy eigsh](https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.linalg.eigsh.html)、[SciPy lobpcg](https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.linalg.lobpcg.html)、[SLEPc EPS](https://slepc.upv.es/release/documentation/manual/eps.html)、[MFEM PML 示例](https://mfem.org/examples/#example-25-perfectly-matched-layers)及 [DOLFINx PML 示例](https://docs.fenicsproject.org/dolfinx/v0.10.0.post5/python/demos/demo_pml.html)交叉检查。近奇异或扩大子空间的建议属于受约束探索，不保证求解成功或认证完整性。

## 可选本地模型解释

`LocalModelConfig(endpoint, model, model_version, license_id, license_approved=False, timeout_seconds=3.0, max_response_bytes=65536)` 定义显式配置。`explain_diagnosis(diagnosis, job=None, model_config=None, *, transport=None)` 返回整个诊断副本，另附 `explanation`，不包装到其他字段。

`explanation` 包含：

- `source`：`rules` 或 `local_model`。
- `summary` 和 `items=[{rule_id,text}]`：仅用于显示的文字。
- `model`：模型 ID、版本、许可证和人工审核标记；未配置时为空。
- `template_version`：提示模板版本。
- `fallback_reason`：`not_configured`、`license_not_approved`、`invalid_configuration`、`timeout`、`invalid_response`、`unavailable` 或成功时空值。

只允许明文 HTTP 的回环 IP 或 `localhost`；`localhost` 被固定到 `127.0.0.1`。拒绝凭据、查询参数、外网地址及重定向，不读取系统代理。路径为 `/v1` 时追加 `/chat/completions`，完整 `/chat/completions` 路径直接使用。超时上限 10 秒，响应上限 64 KiB；总响应截止时间还会终止缓慢持续发送的响应。

请求只包含规则生成的标题、解释和前提。传入 `job` 仅保持适配接口兼容，实际不访问其私有字段；矩阵值、特征对、路径及自由格式错误消息不会发送。响应只接受 `summary`、`items` 两个字段，条目必须引用现有规则 ID；拒绝明显代码块、执行片段、HTML 和私有路径。模型内容始终是非权威文字；原规则、数值结果和建议配置保持不变。项目不自动下载模型或推断许可证已审核。

`transport(config, request_bytes) -> response_bytes` 仅供测试或可信内部适配器注入。默认传输具有上述网络和时间约束。

## 不可变建议效果账本

`AdviceEffects(store, get_job, get_config)` 使用两个按 ID 读取字典的回调；不依赖应用服务对象。唯一新增存储种类为 `advice_effects`，无需数据库表结构迁移。

| 方法 | 作用 |
| --- | --- |
| `create(source_job_id, derived_config_id)` | 追加 `derived` 记录，验证派生来源哈希及配置白名单 |
| `link(effect_id, new_job_id)` | 追加 `linked` 记录；同任务重复绑定幂等，拒绝改绑 |
| `evaluate(effect_id)` | 追加 `observed` 记录，保存当前两次运行的实际观察；未绑定返回 `EFFECT_NOT_LINKED` |
| `list(source_job_id=None)` | 返回全部或指定源任务的记录 |
| `get(effect_id)` | 返回派生记录并附 `records` 完整记录序列 |

每条记录具有独立 `id`、`effect_id`、`record_type`、`schema_version`、`created_at`、`project_id`、`source_job_id`、`derived_config_id`。`derived` 记录的 `effect_id` 等于自身 ID；后续记录引用它。记录由应用接口追加，不提供修改或删除方法；该约束不声称能防止直接修改数据库文件的管理员。

派生记录包含 `before_config`、`after_config`、各自哈希、`configuration_diff=[{path,before,after}]`、`protected_conditions`、`source_snapshot`，并固定 `automatic_adoption=false`。仅批准的求解器字段允许变化，问题、目标、精度、资源和其他配置均保持不变。派生配置必须记录 `derivation.source_job_id` 和对应的 `derivation.source_config_hash`，使用 `contracts.content_hash` 计算；不能以其他哈希序列化约定替代。绑定任务必须同项目、同矩阵哈希、配置与派生快照完全一致。

观察记录包括 `new_job_id`、`before`、`after`、`comparable`、`speedup`、`conditions`、`reason`。`before` 与 `after` 均包含任务 ID、原状态、数值状态、目标认证、错误码、运行秒数、峰值内存、最大残差及内外迭代计数，缺失值为空。仅当两次运行都为 `SUCCEEDED`、`PASSED`、`CERTIFIED`，且输入、受保护条件、环境一致、实际运行耗时有效时，才计算 `speedup=before.walltime_seconds/after.walltime_seconds`。失败、部分成功、环境不一致与缺失指标均不产生加速比。

## 来源与复现

`capture_provenance(source_root=None)` 捕获包版本、Python、操作系统与架构、已安装依赖、限定源码树内容哈希、Git 提交及修改状态。源码树只扫描声明的代码与依赖清单；跳过符号链接、文件超过 8 MiB 或总量超过 32 MiB 的内容，并用 `complete=false` 表示未完整扫描。不返回绝对路径、主机名、用户身份或环境变量全集。

没有已验证镜像信息时 `container.status='unknown'`，摘要为空。仅当操作者提供格式合法的 `EMSOLVER_IMAGE_DIGEST=sha256:...` 时记录 `status='declared'`，并明确这不是独立证明。Git 提交无法获取时为空。

`reproducible_commands()` 输出便携命令：

```sh
emsolver solve --matrix INPUT.npz --config run/config.resolved.yaml --output rerun
emsolver validate --matrix INPUT.npz --run rerun
```

`INPUT.npz` 是占位符，使用者须提供与冻结矩阵哈希匹配的合法输入。来源记录不声称安装环境、外部后端或全新系统已验证。

## 本地验证

在 macOS arm64、`.venv-emsolver` 环境运行：

```sh
.venv-emsolver/bin/python -m pytest tests/test_knowledge.py tests/test_ai_explainer.py tests/test_effects.py tests/test_provenance.py -q
```

测试先确认新增模块缺失导致失败，再验证规则正反例、配置保护、模型异常降级、脱敏、回环 HTTP 协议与慢响应截止、不可变追踪、来源哈希和路径排除。测试不依赖外部网络服务或已安装模型；实际本地模型兼容性需要操作者完成许可证审核后另行验证。
