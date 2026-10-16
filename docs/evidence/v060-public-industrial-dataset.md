# v0.6 真实微波矩阵取得与输入验证

2026-10-16 已从 SuiteSparse 原官方 UF 的 HTTPS 镜像取得完整的 `Dziekonski/gsm_106857`。这是九阶微波梳状滤波器的 FEM（有限元法，将连续电磁场离散为矩阵）真实矩阵，**589446 阶、21758924 非零元、实对称、非正定**。实对称满足本项目的厄密输入条件；“非正定”并不意味着非厄密。[当前官方矩阵页](https://sparse.tamu.edu/Dziekonski/gsm_106857)、[原官方 UF 页](https://www.cise.ufl.edu/research/sparse/matrices/Dziekonski/gsm_106857.html)与原文件头一致。

## 保留文件与身份

原数据位于本地忽略目录 `artifacts/public-dataset-v060/Dziekonski_gsm_106857/`，未覆盖旧版数据。源码及离线包无需重复携带约 704.45MiB 的三个大文件；可按下述来源重取，并核对 [JSON 证据](v060-public-industrial-dataset.json) 的 SHA-256（文件内容摘要，用于判断字节是否相同）。

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `gsm_106857.tar.gz` | 152679272 | `8095cadbdb2ddd654693a32a1fe30f72d3bfd61d87815cdefd56ed6f866c09a1` |
| `gsm_106857.mtx` | 381166735 | `8e375d8c3a4663127dc147b81864caf822d410f18a6351255bb1d2f423e4c150` |
| `gsm_106857.npz` | 204819649 | `b56c5bac7fde872951153508eb0641b17e0f52876445d59d4ec8d3ff7687f268` |

原包与原 Matrix Market 文件逐字节保留。Matrix Market（公开稀疏矩阵交换格式）声明 `real symmetric`，存储下三角 11174185 项；按其声明重建完整对称矩阵后得到 21758924 非零元。只转换为 CSR（按行压缩的稀疏存储）、合并重复项并排序索引；没有修改系数、平均对称化、裁剪、取子矩阵或生成参考答案。

## 许可与归属

[SuiteSparse 官方许可说明](https://sparse.tamu.edu/about)将集合矩阵许可声明为 [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/)，要求署名、保留矩阵元数据并说明变换。作者为 A. Dziekonski、A. Lamecki、M. Mrozowski，编辑为 T. Davis。原始文件头及引用完整保留于原文件和 JSON 证据。

原研究 DOI 为 `10.1109/LMWC.2010.2089974`；[作者所在 Gdańsk 大学的官方研究页](https://eti.pg.edu.pl/kima/nauka-i-badania/wicomms-gpu-research-center)列为 *IEEE Microwave and Wireless Components Letters* 21(1), 1–3, January 2011。原文件头的 2010 年卷期信息与之不同，故保留原文并在证据中记录差异。集合引用为 Davis 与 Hu (2011), DOI `10.1145/2049662.2049663`；Kolodziej 等 (2019), DOI `10.21105/joss.01244`。

当前 TAMU 与原 UF 页属于同一集合来源；作者大学可以独立核对论文归属及卷期，不能独立认证此包的系数或哈希。本地哈希是 TLS（传输加密及服务器证书校验）下载后的计算值，尚无上游签名校验和。

## 本地验证与边界

| 检查 | 实际结果 |
|---|---|
| 平台 | macOS，Darwin arm64；Python 3.12.13、NumPy 2.5.4、SciPy 1.18.1 |
| 下载 | HTTPS 与证书校验开启；最大压缩文件 499999999B；原请求在 113952263B 处提前断流，成功续传一次；`gzip -t` 通过 |
| 系数 | `float64`，全部有限，无空行；维度及非零元数与官方信息一致 |
| 厄密检查 | `A-Aᴴ` 的非零元为 0；`||A-Aᴴ||∞ / ||A||∞ = 0`，`||A||∞ = 51.46419321341725`；`Aᴴ` 表示共轭转置 |
| 内存与时间 | CSR 缓冲区 263464876B；转换与检查合计 10.72 秒；子进程最高实际内存 999702528B |
| 监视边界 | 外部每 100ms 读取内存，2GiB / 180 秒软边界；正常退出，未触发边界 |
| 下载器回归 | `tests/test_fetch_dataset.py`：12 项通过；覆盖官方镜像回退、HTTP 降级拒绝、头部及流式大小边界、断流失败、忽略归档路径、拒绝符号链接/重复成员、原文件保留与对称存储重建 |

**本证据只证明真实工业矩阵取得与厄密结构核对；未启动特征值求解器，不表示原问题残差通过、目标谱完整认证或工业性能达标。** UF 的排序统计显示，无主元 AMD 排序的稀疏 LU 约 3.68 亿非零元；这是公开估计，不能代替本机实际求解内存。直接分解的内存明显高于原 CSR，求解应单独设置和验证资源预算。

## 重取与导入

下载器先尝试现行 HTTPS 数据主机；连接或正文断流失败时尝试原官方 UF HTTPS 镜像。所有重定向在发送下一请求前拒绝 HTTP 降级；响应声明长度和实际流量都受预算限制。只复制唯一、准确命名的常规矩阵成员，归档最多 1000 项、展开总量最多 2GiB；不按归档路径解压。输出目录必须全新，避免覆盖历史证据。遇到下载异常时失败退出，不能将不完整包标记为成功。

```bash
.venv-emsolver/bin/python scripts/fetch_dataset.py gsm_106857 \
  --output data/public/gsm_106857-v060 --max-download-mib 256
.venv-emsolver/bin/emsolver inspect \
  --matrix data/public/gsm_106857-v060/gsm_106857.npz --json
.venv-emsolver/bin/emsolver matrix import --project PROJECT_ID \
  --matrix data/public/gsm_106857-v060/gsm_106857.npz \
  --metadata data/public/gsm_106857-v060/import-metadata.json --json
```

本轮实际获取由 HTTPS 限制的 `curl` 完成，续传后调用与下载器相同的 `convert_archive` 转换函数。镜像回退行为由离线回归测试覆盖；重取命令的完整在线下载尚未另行重复执行，网络是否再次断流仍由服务端和链路状态决定。下载器不自动续传，失败后需使用新输出目录重试；原始下载及续传记录已列入 JSON 证据。
