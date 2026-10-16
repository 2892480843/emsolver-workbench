# 实施接口契约

版本 0.1，2026年10月16日。所有开发者共用本文件；默认离线单用户、本机回环地址。严格不稠密化原矩阵，数值与目标认证分离。

## 目录归属

- numerics开发者独占 `emsolver/numerics/`、`tests/test_numerics*.py`。
- service开发者独占 `emsolver/service.py`、`emsolver/store.py`、`emsolver/api.py`、`emsolver/worker.py`、`tests/test_service*.py`、`tests/test_api*.py`。
- workbench开发者独占 `web/`。
- 主开发者维护 `emsolver/contracts.py`、`emsolver/cli.py`、`emsolver/reporting.py`、打包、脚本和说明。先沟通接口修改，不互相覆盖文件。

## 数值接口

`from emsolver.numerics import load_matrix, inspect_matrix, run_solver, generate_sample, capabilities, verify_run`

- `load_matrix(path: str|Path, storage='full', hermitian_rtol=1e-12, max_bytes=1073741824) -> scipy.sparse.csr_matrix`：安全加载、规范化、厄密检查；非法输入抛 ValueError 或 RuntimeError，消息含稳定错误码。
- `inspect_matrix(matrix) -> dict`：n、nnz、dtype、hermitian_error、norm_inf、storage_bytes、preview（至多1500个[row,col,abs_value]）、diagonal_min/max。全为JSON可序列化Python值。
- `generate_sample(path, kind='indefinite', n=500, seed=20261016) -> dict`：kind支持indefinite、clustered、repeated、near_singular、complex；返回name、description、source='synthetic'、n、nnz、kind，不生成参考答案文件。
- `capabilities() -> dict`：scipy可用、slepc/mpi/gpu真实能力、版本与限制。
- `run_solver(input_path, config:dict, output_dir, event_callback=None, cancel_check=None) -> dict`：同步计算、独立校验、原子写产物。事件回调 `(event:dict)->None`，事件含stage、event、elapsed_seconds和可用指标；cancel_check无参数返回bool。不得读取参考数据。返回status（SUCCEEDED/PARTIAL/FAILED）、error_code、eigenvalues（实数list）、residuals（float list）、validation（含numerical_status/target_certification）、metrics、attempts、n_accepted。向量只存文件，不塞入API响应。输出需有manifest.json、config.resolved.yaml、eigenvalues.npy、eigenvectors.npy、residuals.npy、validation.json、metrics.json；可选SciPy格式；非有限指标转null。取消可抛InterruptedError。未认证目标必须PARTIAL。
- `verify_run(input_path, output_dir, config:dict|None=None) -> dict`：重新读取产物独立验证，不盲信既有报告。

配置由 `emsolver.contracts.resolve_config(dict)->dict` 规范化，与PRD字段相同，默认backend=scipy/method=arpack/fallback_profiles=[]，稀疏标准厄密CPU路径。矩阵本地路径不进入对外配置，仅matrix_id。resources内存GiB、线程、时间和磁盘；数值核心不可超出边界。

## 应用服务与接口

`Service(root:Path|str=None, start_runner=True)` 默认数据根目录取EMSOLVER_DATA_DIR或./data。必须提供 `close()`。

API根`/api/v1`，列表直接JSON数组；实体直接对象。创建项目POST /projects {name,description?}。矩阵POST /matrices为multipart file/project_id/name/storage；POST /samples {project_id,kind,n,name?}生成样例。GET /matrices返回含id/project_id/name/n/nnz/dtype/diagnostics的列表；GET /matrices/{id}/diagnostics。

POST /configs {config:dict} → {id,config,hash}；POST /plans {config:dict,project_id?}或{config_id,project_id?} → {id,config,matrix_id,project_id,estimate,capabilities,warnings,hash}。POST /jobs {plan_id,idempotency_key?}→任务。GET /jobs列表；GET /jobs/{id}对象包含id/project_id/matrix_id/status/stage/created_at/config/result/error/metrics；GET /jobs/{id}/events?after=0返回事件数组，事件有seq/time/stage/event。POST cancel与restart；GET results返回run_solver结果（不传向量）。GET /jobs/{id}/download?file=eigenvalues.npy白名单下载。

GET /capabilities；GET /health；GET /audit；POST /benchmarks {project_id,matrix_ids,k,sigma,repeats?}→{id,jobs,...}；GET /benchmarks/{id}；GET /benchmarks列表。POST /reports {job_id}→{id,url}；GET /reports/{id}返回HTML；GET /jobs/{id}/bundle公开脱敏复现包；POST /diagnose {job_id?,config?}→{source:'rules',suggestions:[{title,reason,changes}],knowledge:[{title,url}],ai_available:false}。

POST /backup →授权下载zip；恢复如支持采用受保护入口，不接受路径穿越。GET /coverage 返回66行实现登记与限制，可由仓库文件生成。共享模式EMSOLVER_API_TOKEN可选，所有api要求Bearer token，默认单用户。细分项目成员角色如未完成必须明确披露，不得把共享token冒称RBAC。

服务使用SQLite和不可变JSON配置；每个作业独立子进程，父进程监控整个进程树的内存/时间，取消杀进程树。事件序号可补取，重试新建关联任务。服务器重启后的遗留作业须WORKER_LOST而非永远RUNNING。幂等键带请求摘要，陈旧计划复验输入哈希与能力。

## CLI与报告

主开发者从服务公开方法或API实现CLI（无Web也可运行）。核心同步solve优先直接调用numerics；其他共享服务方法名由service开发者及时公布。`reporting.build_report(job:dict, events:list, output_path:Path)->Path`生成转义HTML；`reporting.build_bundle(...)`在主开发者实现后对接，禁止打包.env和私有输入。对外不泄露绝对路径。

前端推荐12项导航，工业科研工作台风格：浅底、深蓝灰导航、橙色主操作，真实数据，清晰的候选/认证状态，不能伪造运行进度。启动`npm run dev`，构建`npm run build`到web/dist，FastAPI同源托管。
