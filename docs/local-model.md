# 可选本地解释模型

此路径只把已登记规则转换为显示用解释，不读取矩阵、向量或自由文本故障信息，也不能修改配置、规则建议、数值判定或目标认证。主求解流程不依赖模型；未配置、未批准许可、超时、不可用或输出无效时，返回原有规则及明确降级原因。

## 固定来源和已验证文件

本次使用 `mlx-community/Qwen2.5-0.5B-Instruct-4bit`，固定版本为 `a5339a4131f135d0fdc6a5c8b5bbed2753bbe0f3`。4 bit 表示权重的量化存储位宽，不表示最终求解器精度。模型只用于文字解释。

量化仓库的 [固定版本说明](https://huggingface.co/mlx-community/Qwen2.5-0.5B-Instruct-4bit/blob/a5339a4131f135d0fdc6a5c8b5bbed2753bbe0f3/README.md) 记录了向 MLX 的转换来源。上游 `Qwen/Qwen2.5-0.5B-Instruct` 的 [固定版本 LICENSE](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct/blob/7ae557604adf67be50417f59c2c2f167def9a775/LICENSE) 标明 Apache-2.0；该许可原文保存在模型目录的 `LICENSE.upstream`，没有用主分支的可变文件代替固定许可。

| 项目 | 固定值 |
|---|---|
| 权重文件 | `model.safetensors` |
| 权重字节数 | `278064920` |
| 权重 SHA-256 | `ddffab9cbc7bf6dde941c6724841eeca8981fcfa81ca20ff8efff1396326d153` |
| 上游版本 | `7ae557604adf67be50417f59c2c2f167def9a775` |
| 许可 SHA-256 | `832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e` |
| 量化 | 4 bit，group_size=64 |
| 实测运行环境 | macOS / arm64；Python 3.12.13、MLX 0.32.4、MLX-LM 0.32.1；实际 Metal 可用 |

MLX 是本例在 Apple GPU 上执行本地模型的运行库。当前启动门禁只开放已经实跑的 macOS MLX 组合，不宣称该模型运行器已在 Linux 或其他 GPU 上验证。

`verify_model` 对权重、配置、tokenizer、词表、合并表、特殊 token、模型说明及上游许可共 9 个文件逐一检查固定 SHA-256，并拒绝符号链接。实际摘要见 `docs/evidence/v050-local-model-files.json`。下载记录位于模型目录的 `download-provenance.json`，不包含宿主机路径、访问令牌或临时签名下载地址。

## 联网准备与离线启动

先在允许联网的准备阶段安装可选运行依赖，并下载明确批准使用的模型：

```bash
python scripts/download_local_model.py --directory artifacts/local-models/qwen25-05b-instruct-4bit --parallel 4
```

下载器只使用固定 HTTPS 来源，保留证书校验，禁用用户 curlrc，最多 4 路分片。分片必须满足 `206`、精确 `Content-Range` 和长度上限；最终完整权重摘要不匹配时不会发布为可加载模型。中断分片只用于后续恢复，不是已验证模型。旧失败的大模型部分文件没有被冒充为本模型。

准备环境中的可选版本为：

```bash
python -m pip install mlx==0.32.4 mlx-lm==0.32.1
```

断网使用前，须把对应解释器、全部依赖和经过校验的 9 个模型文件一并准备好。主应用 CPU 离线安装包本身不会自动下载这些可选权重。

```bash
python scripts/local_model.py verify --directory artifacts/local-models/qwen25-05b-instruct-4bit
python scripts/local_model.py configure --directory artifacts/local-models/qwen25-05b-instruct-4bit --output artifacts/local-model-config.json --port 18321
python scripts/local_model.py serve --directory artifacts/local-models/qwen25-05b-instruct-4bit --port 18321
```

配置命令只创建新文件，并设置权限 `0600`；已有配置不会被静默覆盖。模型服务只监听 `127.0.0.1`，对外使用 `default_model` 别名，主应用不向解释接口发送本地模型绝对路径。启动前设置 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`、禁用隐式令牌与遥测，并禁止信任远程 tokenizer 代码。该本机开发服务没有作为公网推理服务发布。

另一个终端启动主应用时显式选用该配置：

```bash
EMSOLVER_LOCAL_MODEL_CONFIG="$PWD/artifacts/local-model-config.json" python -m emsolver serve --host 127.0.0.1 --port 8000
```

更改端口时，配置与模型服务端口必须一致。模型不可用时主应用继续使用规则，不扩大权限或降低数值验收门槛。

## 实际解释与格式边界

取证通过真实回环 HTTP 请求调用模型；观察函数只记录请求与响应字节，不修改生成内容。`docs/evidence/v050-local-model-initial-invalid.json` 保存最初英文模板的 4 个实际结果：模型确实完成生成，但均未满足严格顶层 `summary/items` 契约，系统全部降级为 `rules / invalid_response`。其中一次输出 Markdown 围栏，另几次复制为单条规则对象；没有剥离围栏或重组对象来制造通过。

`docs/evidence/v050-local-model.json` 记录当前生产模板 `diagnosis_explanation_v3` 的真实解释、解析结果与降级案例。该模板使用两组完整的对话格式示例，最后一条消息只含当前内部规则，不提供当前案例的答案或占位文字。严格解析器未放宽，生成响应没有经过围栏剥离、字段重组或模板填补。

| 真实输入案例 | 实际来源 | 回环调用耗时 |
|---|---|---|
| 未触发故障的证据复查 | `local_model` | 约 0.60 秒 |
| 近奇异分解 | `local_model` | 约 0.30 秒 |
| 超时诊断 | `local_model` | 约 0.32 秒 |
| 目标未认证 | `local_model` | 约 0.42 秒 |

另外三例分别验证许可未批准、未配置和不可用回环端口的规则降级。所有 7 例的原规则、建议与输入状态均保持不变。上述耗时是本次观察，不是稳定吞吐承诺。

小模型可能简化或遗漏文字中的适用前提；返回数据中的原始 `rules`、`suggestions`、`prerequisites` 继续作为权威说明，不由模型文字代替。这里证明实际推理及结构校验边界，不提供模型质量分数，不把流畅文字作为数值证据。MLX-LM 此版本的本机服务器不实施 `response_format` 的 JSON 受限解码；格式仍依赖提示及主应用的严格解析，失败必须保留降级。

```bash
python -m pytest -q tests/test_model_runtime.py tests/test_model_download.py tests/test_ai_explainer.py
```

单元测试证明摘要/配置门禁、严格解析、范围验证和降级条件；真实模型文件与实际请求响应由上述证据文件分别记录，二者没有相互冒充。
