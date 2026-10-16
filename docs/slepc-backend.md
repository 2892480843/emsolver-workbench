# 可选 SLEPc / PETSc / MPI 后端

v0.9新增的实数单秩磁盘外对称不定分解见[OOC配置与实际证据](slepc-ooc-profile.md)。下述实数/复数、单秩/双秩范围描述基础LU配置，不能据此给OOC开放复数或双秩。当前运行镜像必须与本版 `slepc_driver.py` 摘要匹配；仅保留旧版镜像时，需重新构建或载入对应版本镜像。

已在 **Linux arm64 容器**完成真实实数、复数与 1 / 2 个 MPI 进程的计算。SLEPc 是大规模特征值求解库；PETSc 提供稀疏矩阵和线性求解；MPI 负责进程间通信。本配置执行 `EPSKRYLOVSCHUR`、`STSINVERT`、`KSPPREONLY`、`PCLU`、MUMPS 稀疏分解。候选向量必须经过主工作进程对原始矩阵的独立残差与目标范围认证，容器返回值本身不构成成功证书。

## 已验证范围

| 项目 | 真实环境或行为 |
|---|---|
| 容器系统 | Debian bookworm，Linux arm64，原生执行；宿主 macOS arm64 |
| 运行时 | Python 3.11.2、PETSc 3.18.5、SLEPc 3.18.2、Open MPI 4.1.4 |
| 标量和并行组合 | `float64` / `complex128` × `mpi_ranks=1` / `2`；每进程 1 线程 |
| 问题 | 标准 Hermitian（实对称或复共轭对称），`nearest_k`，固定目标位移 |
| 策略 | `backend=slepc`，`method=krylovschur`，`transform=shift_invert`，原始或实际 RCM 重排 |
| 上限 | `k<=512`、`subspace_max<=1024` 且大于 `k`；维度不超过 1,000,000；实际资源预算仍可更早拒绝 |
| 未提供 | 广义问题、检查点恢复、混合精度、可切换预处理器、自适应工作位移重试、该 SLEPc 配置的 CUDA 路径 |

`adaptive_shift.enabled` 在此后端不作用，策略记录 `applies=false`；不得将改变该标志计为有效消融。MPI 输入读取在每个进程中复制 CSR（压缩稀疏行矩阵），随后 PETSc 按行分配算子；**这里没有分布式输入读取或大规模加速结论**。项目另有独立的 Apple Metal 混合计算配置，其设备证据不属于本 SLEPc 配置。

## 构建与离线加载

`Dockerfile.slepc` 默认基于 Debian bookworm。此次实际使用本地已有的官方 `node:22-bookworm-slim` arm64 基础镜像；Node 没有参与数值计算。APT 安装独立的实数和复数 PETSc/SLEPc Python 绑定；驱动显式选择对应绑定，避免把 Python 3.11 系统扩展装入主应用的其他 Python 版本。镜像构建只有通过四种组合的真实 12 阶自测后才写入能力标签。

```sh
docker build --platform=linux/arm64 --pull=false \
  --build-arg BASE_IMAGE=node:22-bookworm-slim \
  -f Dockerfile.slepc -t emsolver-slepc:bookworm-arm64-v1 .
(cd artifacts && shasum -a 256 -c emsolver-slepc-bookworm-arm64-v1.docker.tar.gz.sha256)
gzip -dc artifacts/emsolver-slepc-bookworm-arm64-v1.docker.tar.gz | docker load
```

校验文件里的归档名相对于 `artifacts/`，所以也可在该目录运行 `shasum -a 256 -c emsolver-slepc-bookworm-arm64-v1.docker.tar.gz.sha256`。归档实际完成 `docker load`，恢复出的镜像身份与下表一致。旧版 0.4 制品未覆盖。

| 身份 | 值 |
|---|---|
| 镜像不可变 ID | `sha256:04a9337651ebae49cffb40b0fdce800aadb5cbc15325de53ba73acbbbb486f11` |
| 驱动 SHA256 | `d885ae408596fcfa5b8015c3bbd8f37fe81778f28cb3759f099c531d8633072a` |
| 归档 | `artifacts/emsolver-slepc-bookworm-arm64-v1.docker.tar.gz`，336,201,931 字节 |
| 归档 SHA256 | `c685a224dc3943fda7452081cc9a57cd9d83390431897ab5b7d003e71cff9843` |

配置须由服务启动环境显式给出，镜像不会自动拉取：

```sh
export EMSOLVER_SLEPC_IMAGE=emsolver-slepc:bookworm-arm64-v1
export EMSOLVER_DOCKER_SOCKET="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"
```

仅接受当前用户拥有、非符号链接且规范化的本地 Unix socket；远程 TCP / SSH 端点拒绝启用。容器无法访问该 socket。管理员需要确认所选本地 Docker daemon 是受信任的执行环境。

## 能力与独立进程契约

`probe_slepc()` 在服务端执行实数 / 复数 × 1 / 2 进程的实际自测，公开档案只包含稳定能力、版本、镜像 ID、驱动哈希与已验证组合。缓存 30 秒，键包含镜像 ID、socket 身份及驱动哈希；公开结果没有时间戳、自由内存、私有路径或随机运行标识。

作业提交保存该公开档案。`seed_slepc_profile(profile)` 在工作进程中验证完整、有界且固定的档案 schema、当前不可变镜像、构建自测标签、驱动哈希及 socket；工作进程随后只检查身份，不再启动四组全局自测。缺失或错误档案进入拒绝模式，不能回退到作业内自测。服务恢复还要核对档案与已冻结作业计划完全一致。

`solve_slepc(input_path_or_csr, config, outputdir, event_callback=None, check=None)` 返回 `values`、`vectors`、`metrics`、`strategy`、`attempts`、`candidate_status`。向量遵循传入矩阵的行序，数值是原单位；主适配器负责重排的逆映射，再使用原始矩阵复核。输出的 `CONVERGED` 是 SLEPc 候选状态，最终 `SUCCEEDED` / `PARTIAL` 由主应用认证确定。

驱动只读固定 `/input/matrix.npz` 和严格白名单 `spec.json`，禁止任意文件引用、pickle、非有限值、重复字段、异常形状与超限数组。只写固定候选数组、哈希、有界 JSON 和 rank 0 事件；事件最多 256 条，内部误差估计明确标注用途，不能代替原问题残差。输入、输出私有目录位于发布产物目录外，控制文件和候选文件在终止路径清理。

## 资源和取消

一个作业的内存总额由共享 `memory_budgets` 分成两半：主机工作进程树一半，Docker 容器一半。容器的 `--memory` 和 `--memory-swap` 都是容器份额，禁止把用户总预算再次完整分配给容器。指标同时保存 total / host / container 字节和执行原因；主机 RSS（驻留内存）不包含 daemon 管理的容器进程，不能把它当作全作业测量。

容器固定使用 `--network=none`、只读根文件系统、移除所有 capabilities、禁止提权、128 进程上限、受限 CPU、64 MiB 临时目录，只挂载输入只读和输出可写两处，不挂载参考答案。外层主进程执行时间 / 内存 / 磁盘 / 取消检查，容器内另有 `timeout`，防止主机进程意外丢失后永久计算。

正常与异常路径先终止 Docker 客户端，再移除该作业的精确命名容器。服务强制杀死工作进程后调用 `cleanup_slepc(run_dir)`，只按该目录内私有 UUID 与精确匹配标记回收，拒绝符号链接与其他容器名，不执行全局 prune。daemon 不可用时保留待回收控制目录，避免虚报清理成功。

## 验证证据

本次定向套件 `tests/test_slepc_driver.py tests/test_optional_backends.py tests/test_slepc_integration.py tests/test_slepc_service.py`：**44 passed in 19.20s，零跳过**。包含实际容器内存不足、磁盘 / 时间预算、数组篡改、严格能力门禁、宿主工作进程被杀后的容器清理，以及真实 Service 双进程作业取消。取消测试先暂停已运行容器，再取消作业，确认 `CANCELLED`、恰好一次终态事件、容器与私有目录消失、输入哈希不变。

完整四组主求解器运行及二次独立验证见 [validation.json](evidence/slepc/run-20261016/validation.json)，每组 60 阶非对角矩阵、3 个目标、固定 seed 20261016、实际 RCM 重排：**4/4 `SUCCEEDED`，原问题 `PASSED`，目标 `CERTIFIED`**。最大二范数绝对残差不超过 `9.97e-16`，与计算后才生成的独立 NumPy 参考的特征值最大误差不超过 `3.53e-15`。每组保存原输入、解析后的配置、发布产物、独立复核、原始事件及实际 Docker inspect 资源限制；证据没有挂载源路径。

复现时输出目录必须不存在：

```sh
EMSOLVER_TEST_SLEPC=1 .venv-emsolver/bin/python -m pytest \
  tests/test_slepc_driver.py tests/test_optional_backends.py \
  tests/test_slepc_integration.py tests/test_slepc_service.py -q
.venv-emsolver/bin/python docs/evidence/slepc/reproduce.py --output artifacts/slepc-validation-new
```

这些结果证明本地功能、原问题数值正确性和隔离行为；不证明 MPI 加速比、跨主机扩展性或 SLEPc GPU 支持。

## 官方依据与许可

驱动接口参考 [SLEPc Python 示例](https://slepc.upv.es/release/slepc4py/demo/ex2.html)、[位移逆变换教程](https://slepc.upv.es/release/documentation/hands-on/hands-on2.html) 与 [谱变换和并行分解说明](https://slepc.upv.es/release/documentation/manual/st.html)。软件来源是 Debian 官方 [SLEPc 复数绑定](https://packages.debian.org/bookworm/python3-slepc4py-complex) 和 [PETSc 复数绑定](https://packages.debian.org/bookworm/python3-petsc4py-complex) 及对应实数包；最新上游文档不替代本次实际 3.18 运行证据。安装版本收据见 [packages.tsv](evidence/slepc/run-20261016/packages.tsv)。容器保留各 Debian 包 `/usr/share/doc/<package>/copyright` 与基础镜像许可文件；再次分发镜像应随同保留它们。
