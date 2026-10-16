"""Escaped local reports and auditable, allowlisted reproducibility archives."""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
from pathlib import Path
import zipfile


SENSITIVE = {"token", "password", "secret", "authorization", "api_key", "access_token",
             "path", "filepath", "input_path", "matrix_path", "output_dir", "root", "filename"}
RUN_FILES = {"manifest.json", "config.resolved.yaml", "eigenvalues.npy", "eigenvectors.npy",
             "residuals.npy", "validation.json", "metrics.json", "environment.json",
             "eigenvalues.scipy.npz", "eigenvectors.scipy.npz", "events.jsonl", "execution.metrics.json",
             "strategy.json", "memory-estimate.json", "diagnostics.json", "permutation.npy", "subspace.checkpoint.npz"}

PRIVATE_PATH = re.compile(r'(?:/(?:Users|home|private|tmp|var|srv|mnt)/[^\s\"\'<>;,}]+|(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"\'<>;,}]+)')
IDENTITY_KEYS = frozenset({'actor','subject','members','user','username','email','identity','configured_subjects'})


def credential_query(key):
    key=re.sub(r'[^a-z0-9]','',str(key).lower())
    return any(part in key for part in ('token','secret','password','credential','signature','authorization','apikey','accesskey','sessionid')) or key in {'key','auth','sig','code'}


def public_url(value):
    from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
    try:
        parsed=urlsplit(value)
        if parsed.scheme not in {'http','https'} or not parsed.netloc:return value
        query=urlencode([(key,'[redacted]' if credential_query(key) else item) for key,item in parse_qsl(parsed.query,keep_blank_values=True)])
        return urlunsplit((parsed.scheme,parsed.netloc.rsplit('@',1)[-1],parsed.path,query,''))
    except ValueError:return '[invalid URL redacted]'


def sanitize_public(value):
    """Public exports exclude free-form annotations and personal identities."""
    if isinstance(value,dict):
        return {str(key):sanitize_public(item) for key,item in sanitize(value).items()
                if str(key).lower() not in IDENTITY_KEYS|{'description','purpose','generator_script'}
                and not (str(key).lower()=='source' and isinstance(item,str))
                and not str(key).startswith('_')}
    if isinstance(value,(list,tuple)):return [sanitize_public(item) for item in value]
    if isinstance(value,str):return PRIVATE_PATH.sub('[local path redacted]',public_url(value))
    return sanitize(value)


def sanitize(value):
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()
                if str(k).lower() not in SENSITIVE and not any(w in str(k).lower() for w in ("password", "secret", "token"))}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        if value.startswith(("/Users/", "/home/", "/private/", "/tmp/")):
            return "[local path redacted]"
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def atomic_text(path: Path, text: str) -> Path:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8"); temp.replace(path)
    return path


def build_report(job: dict, events: list, output_path: Path) -> Path:
    clean = sanitize(job)
    result = clean.get("result") or {}
    validation = result.get("validation") or clean.get("validation") or {}
    metrics = result.get("metrics") or clean.get("metrics") or {}
    esc = lambda value: html.escape(str(value), quote=True)
    name = clean.get("name") or clean.get("id", "local run")
    rows = []
    vals = result.get("eigenvalues", [])
    residuals = result.get("residuals", [])
    for i, value in enumerate(vals):
        residual = residuals[i] if i < len(residuals) else "未记录"
        rows.append(f"<tr><td>{i+1}</td><td>{esc(value)}</td><td>{esc(residual)}</td></tr>")
    if not rows:
        rows.append('<tr><td colspan="3">本次运行没有可发布的特征对</td></tr>')
    def block(value):
        return f'<pre>{esc(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))}</pre>'
    text = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EMSolver 运行报告 {esc(name)}</title><style>
body{{font:15px/1.7 system-ui,sans-serif;color:#172b3a;background:#f6f8fa;margin:0}}main{{max-width:1040px;margin:40px auto;background:white;padding:42px}}
h1{{font-size:28px}}h2{{margin-top:32px;border-bottom:1px solid #dbe3e9;padding-bottom:8px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #dbe3e9;padding:10px;text-align:left}}th{{background:#eaf0f5}}
pre{{background:#f2f5f8;padding:18px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}.status{{font-weight:700;color:#a4481c}}small{{color:#647483}}
@media print{{body{{background:white}}main{{padding:0;margin:0}}h2{{break-after:avoid}}tr{{break-inside:avoid}}}}</style>
<main><small>EMSolver · 独立原问题验证 · 本地报告</small><h1>{esc(name)}</h1>
<p class="status">作业状态：{esc(clean.get('status','UNKNOWN'))}</p>
<p>结果文件状态：{esc(clean.get('artifacts_state','AVAILABLE'))}；历史验证状态不表示已过期文件仍可下载。</p>
<p>数值有效与目标认证分别记录。残差小不能单独证明最近k个模式或区间完整性。未认证的候选不得用于声称完整目标通过。</p>
<h2>目标与配置</h2>{block(clean.get('config', {}))}
<h2>数据来源与输入哈希</h2>{block({'matrix_hash':clean.get('matrix_hash'),'metadata':clean.get('matrix_metadata',{})})}
<h2>环境、源码与依赖</h2>{block(clean.get('runtime',{}))}
<h2>实际算法与复现命令</h2>{block({'strategy':result.get('strategy'), 'commands':(clean.get('runtime',{}).get('provenance') or {}).get('reproducible_commands',{})})}
<h2>验证结论</h2>{block(validation)}
<h2>异常与停止原因</h2>{block(clean.get('error') or result.get('error') or {'error_code':result.get('error_code'), 'note':'无异常记录' if not result.get('error_code') else '保留原始错误码'})}
<h2>特征对</h2><table><thead><tr><th>序号</th><th>特征值</th><th>缩放残差</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>性能与资源</h2>{block(metrics)}
<h2>算法尝试</h2>{block(result.get('attempts', []))}
<h2>事件记录</h2>{block(sanitize(events[-1000:]))}
<h2>复现边界</h2><p>本报告只陈述当前输入、配置和实际后端的运行结果。公开复现包默认不包含输入及私有模态结果；请由数据权利人提供合法输入。不同硬件和并行库的结果按容差比较。</p>
</main></html>"""
    return atomic_text(Path(output_path), text)


def build_bundle(job: dict, events: list, run_dir: Path, output_path: Path,
                 matrix_path: Path | None = None, include_input: bool = False,
                 *, include_results: bool = False) -> Path:
    run_dir = Path(run_dir); output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    hashes = {}
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        def put(name, content):
            raw = content.encode("utf-8") if isinstance(content, str) else content
            z.writestr(name, raw); hashes[name] = hashlib.sha256(raw).hexdigest()
        def put_file(name,path):
            sha=hashlib.sha256()
            with path.open('rb') as source,z.open(name,'w') as destination:
                for block in iter(lambda:source.read(1024*1024),b''):
                    sha.update(block);destination.write(block)
            hashes[name]=sha.hexdigest()
        # Private modal data can reveal the source model even without its matrix.
        # Export only the declared public subset; internal backups use a separate API.
        clean_job = {k: job[k] for k in ('id','status','stage','config','created_at','finished_at') if k in job}
        clean_job['runtime'] = {k:v for k,v in (job.get('runtime') or {}).items() if k in ('python','platform','capabilities','provenance')}
        clean_events = [{k:v for k,v in event.items() if k in ('seq','stage','event','elapsed_seconds','error_code')} for event in events]
        if include_results:
            clean_job.update({k:job[k] for k in ('result','metrics','matrix_hash','matrix_metadata') if k in job})
        put("job.json", json.dumps(sanitize_public(clean_job), ensure_ascii=False, indent=2, allow_nan=False))
        put("events.json", json.dumps(sanitize_public(clean_events), ensure_ascii=False, indent=2, allow_nan=False))
        put('export-policy.json',json.dumps({'schema_version':'1.0','input_included':bool(include_input and matrix_path),
            'results_included':include_results,'excluded':['user identity','credentials','absolute paths']+
            ([] if include_input else ['input matrix'])+([] if include_results else ['private modal results','private run diagnostics']),
            'alternative':'emsolver sample --kind indefinite --n 1000 --output public-sample.npz',
            'events_note':'Service export includes at most the latest 1000 canonical events; full history is available from the authenticated events.jsonl download.'},ensure_ascii=False,indent=2))
        source_manifest = None
        source_manifest_hash = None
        for name in sorted(RUN_FILES):
            if not include_results and name != 'config.resolved.yaml':
                continue
            path = run_dir / name
            if not path.is_file() or path.is_symlink():
                continue
            if name == 'manifest.json':
                source_manifest = sanitize_public(json.loads(path.read_text()))
                source_manifest_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                continue
            if name.endswith(".json"):
                try:
                    content = json.dumps(sanitize_public(json.loads(path.read_text())), ensure_ascii=False, indent=2, allow_nan=False)
                except (ValueError, UnicodeError):
                    continue
            elif name.endswith(".jsonl"):
                records=[]
                for line in path.read_text().splitlines():
                    try: records.append(json.dumps(sanitize_public(json.loads(line)), ensure_ascii=False))
                    except ValueError: continue
                content="\n".join(records)
            elif name.endswith(".yaml"):
                import yaml
                content=yaml.safe_dump(sanitize_public(yaml.safe_load(path.read_text())), allow_unicode=True, sort_keys=False)
            else:
                put_file(f'run/{name}',path)
                continue
            put(f"run/{name}", content)
        if source_manifest is not None:
            # Redaction and YAML normalization change bytes. Rebuild exported hashes
            # while retaining the source manifest identity for provenance.
            source_manifest['artifacts'] = {name.removeprefix('run/'):digest for name,digest in hashes.items() if name.startswith('run/')}
            if 'run/config.resolved.yaml' in hashes:source_manifest['config_sha256']=hashes['run/config.resolved.yaml']
            source_manifest['redacted_export'] = True
            source_manifest['source_manifest_sha256'] = source_manifest_hash
            put('run/manifest.json',json.dumps(source_manifest,ensure_ascii=False,indent=2,allow_nan=False))
        if include_input and matrix_path is not None:
            matrix_path=Path(matrix_path)
            if matrix_path.suffix not in (".npz", ".npy") or matrix_path.is_symlink():
                raise ValueError("INVALID_INPUT: only explicit numeric matrix input may be included")
            put_file("input/matrix"+matrix_path.suffix,matrix_path)
        package=Path(__file__).resolve().parent
        project=Path(os.environ.get('EMSOLVER_RESOURCE_DIR',package.parent))
        for file in sorted(package.rglob("*.py")):
            if not file.is_symlink(): put('emsolver/'+str(file.relative_to(package)), file.read_bytes())
        for name in ("pyproject.toml", "requirements.lock", "LICENSE", "NOTICE.md"):
            file=project/name
            if file.is_file(): put(name,file.read_bytes())
        input_note="包含用户明确选择的输入文件。" if include_input else "未包含输入矩阵，请提供经授权的原始文件。"
        result_note='包含任务冻结元数据已授权公开的模态结果。' if include_results else '未包含私有模态结果及运行诊断；请在内部受保护环境中重跑。'
        put("README.md", f"# EMSolver 复现包\n\n{input_note}{result_note}\n\n安装：`python -m venv .venv`，激活后 `pip install .`。\n\n运行：`emsolver solve --matrix INPUT.npz --config run/config.resolved.yaml --output rerun`。\n\n验证：`emsolver validate --matrix INPUT.npz --run rerun`。\n\n本包中的配置、状态、指标和候选均保留原有认证边界。源码不依赖外部AI服务；没有专用GPU可采用CPU。\n")
        put("checksums.json",json.dumps(hashes,sort_keys=True,indent=2))
    from .public_export import scan_public_archive
    scan=scan_public_archive(temp)
    if not scan['passed']:
        temp.unlink(missing_ok=True)
        raise ValueError('PUBLIC_EXPORT_SCAN_FAILED: generated runtime metadata failed public export checks')
    temp.replace(output_path)
    return output_path


def build_benchmark_report(batch: dict, output_path: Path) -> Path:
    clean=sanitize(batch);esc=lambda value:html.escape(str(value),quote=True)
    # Config paths name fields, while the generic export policy removes local
    # filesystem paths. Give these explicit report fields an unambiguous name.
    for source,group in zip(batch.get('groups',[]),clean.get('groups',[])):
        group['configuration_diff']=[{'field':sanitize(entry.get('path')),
                                     'before':sanitize(entry.get('before')),
                                     'after':sanitize(entry.get('after'))}
                                    for entry in source.get('configuration_diff',[])]
    rows=[];details=[]
    for job in clean.get('runs',[]):
        result=job.get('result') or {};metrics={**result.get('metrics',{}),**job.get('metrics',{})}
        validation=result.get('validation') or {};error=job.get('error') or {}
        experiment=job.get('experiment') or {};config=job.get('config') or {}
        target=config.get('target') or {};resources=config.get('resources') or {}
        residuals=[value for value in validation.get('residuals',[]) if type(value) in (int,float) and math.isfinite(value)]
        evaluation=(job.get('evaluation') or (batch.get('evaluations') or {}).get(job.get('id')) or {})
        reference=evaluation.get('max_eigenvalue_absolute_error','NOT_AVAILABLE（无独立参考）')
        angle=evaluation.get('max_subspace_sine','NOT_AVAILABLE（无独立参考）')
        guidance={'RESOURCE_EXHAUSTED':'缩小子空间或选择有依据的低内存路线；保留硬预算。',
                  'WALLTIME_EXCEEDED':'检查收敛与停滞，创建新实验对照；保留时间硬上限。',
                  'TARGET_UNVERIFIED':'补充隔离参考或可靠谱计数；当前候选不能证明目标覆盖。',
                  'NOT_HERMITIAN':'核对输入与三角声明；不得静默对称化。',
                  'BACKEND_UNAVAILABLE':'选择已探测并验收的 CPU 后端。'}
        code=error.get('code') or result.get('error_code')
        disposition=guidance.get(code,'检查原问题残差、目标认证和结构化事件；诊断元数据不足时不自动修改参数。')
        try:
            from .knowledge import diagnose
            diagnosis=diagnose(job=job)
            active=[rule.get('title') or rule.get('id') for rule in diagnosis.get('rules',[]) if rule.get('triggered')]
            if active:disposition+=' '+', '.join(str(value) for value in active)
        except (ValueError,TypeError,KeyError):pass
        accuracy={'job_id':job.get('id'),'status':job.get('status'),'residual_max':max(residuals) if residuals else None,
                  'reference_error':reference,'subspace_error':angle,'outer_iterations':metrics.get('outer_iterations'),
                  'linear_iterations':metrics.get('linear_iterations'),'metrics':metrics,'validation':validation,
                  'evaluation':evaluation or {'status':'NOT_AVAILABLE','reason':'无独立参考；未认证'},'disposition':disposition}
        details.append(f'<details><summary>{esc(job.get("id"))} · 精度、迭代与处置建议</summary><pre>{esc(json.dumps(accuracy,ensure_ascii=False,indent=2,allow_nan=False))}</pre></details>')
        rows.append('<tr>'+''.join(f'<td>{esc(value)}</td>' for value in [job.get('id'),
                    experiment.get('cell_id','旧版单组'),experiment.get('repeat','未记录'),
                    target.get('k','—'),target.get('sigma','—'),resources.get('threads','—'),resources.get('memory_limit_gib','—'),
                    job.get('status'),
                    validation.get('numerical_status','未验证'),validation.get('target_certification','未认证'),
                    metrics.get('walltime_seconds',metrics.get('elapsed_seconds','未记录')),
                    metrics.get('peak_rss_tree_bytes',metrics.get('peak_memory_bytes','未记录')),code or '',
                    max(residuals) if residuals else 'NOT_AVAILABLE',reference,angle,
                    metrics.get('outer_iterations') if metrics.get('outer_iterations') is not None else 'NOT_AVAILABLE',
                    metrics.get('linear_iterations') if metrics.get('linear_iterations') is not None else 'NOT_AVAILABLE',disposition])+'</tr>')
    summary=esc(json.dumps(clean.get('summary',{}),ensure_ascii=False,indent=2))
    groups=esc(json.dumps(clean.get('groups',[]),ensure_ascii=False,indent=2))
    config=esc(json.dumps({k:v for k,v in clean.items() if k not in ('runs','summary')},ensure_ascii=False,indent=2))
    text=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EMSolver 基准报告 {esc(clean.get('id',''))}</title><style>body{{font:14px/1.7 system-ui,sans-serif;margin:40px;color:#172b3a}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #dbe3e9;padding:8px;text-align:left;overflow-wrap:anywhere}}th{{background:#eaf0f5}}pre{{white-space:pre-wrap;background:#f2f5f8;padding:18px}}tr{{break-inside:avoid}}</style>
<h1>EMSolver 基准报告</h1><p>以下保留全部作业，包括失败、部分、超时和取消。统计分母按已记录数据列明；未通过数值与目标验收的运行不计有效加速。</p>
<p>参数组合的累计时间与磁盘预算是各运行硬限额之和，并非预测实际耗时或空间。目标或资源条件不一致的组不能直接据此声称公平加速。</p>
<h2>冻结套件</h2><pre>{config}</pre><h2>全量汇总</h2><pre>{summary}</pre>
<h2>参数组合与配置差异</h2><pre>{groups}</pre>
<h2>逐次运行</h2><div style="overflow-x:auto"><table><thead><tr><th>作业</th><th>参数组合</th><th>重复编号</th><th>k</th><th>σ</th><th>线程限制</th><th>内存限制GiB</th><th>终态</th><th>数值</th><th>目标认证</th><th>秒</th><th>峰值字节</th><th>错误码</th><th>最大残差</th><th>独立参考误差</th><th>子空间误差</th><th>外层迭代</th><th>线性内层迭代</th><th>处置建议</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<h2>逐例原始指标与处置</h2>{''.join(details)}</html>'''
    return atomic_text(Path(output_path),text)
