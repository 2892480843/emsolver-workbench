"""Versioned offline knowledge and evidence based, non-mutating diagnosis rules."""
from __future__ import annotations

import copy

from .advice import apply_solver_advice
from .contracts import resolve_config


REGISTRY_VERSION = '2026.10.16.1'
RULE_VERSION = 'diagnosis_v2'


def _source(title, url, version):
    return {'title': title, 'url': url, 'version': version, 'accessed_at': '2026-10-16'}


SCIPY = _source('SciPy eigsh official manual', 'https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.linalg.eigsh.html', '1.18.0 documentation')
SLEPC = _source('SLEPc EPS official manual', 'https://slepc.upv.es/release/documentation/manual/eps.html', '3.26.0 documentation')
LOBPCG = _source('SciPy lobpcg official manual', 'https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.linalg.lobpcg.html', '1.18.0 documentation')
MFEM = _source('MFEM Example 25: Perfectly Matched Layers', 'https://mfem.org/examples/#example-25-perfectly-matched-layers', 'online example reviewed 2026-10-16')
DOLFINX = _source('DOLFINx electromagnetic scattering with PML', 'https://docs.fenicsproject.org/dolfinx/v0.10.0.post5/python/demos/demo_pml.html', '0.10.0.post5 documentation')


def _entry(identity, title, summary, prerequisites, risks, sources):
    return {'id': identity, 'version': '1.0', 'registry_version': REGISTRY_VERSION,
            'title': title, 'summary': summary, 'prerequisites': prerequisites,
            'risks': risks, 'sources': sources}


_ENTRIES = (
    _entry('hermitian', '厄密结构与三角存储',
           '厄密矩阵等于自身的共轭转置。复数矩阵不能用普通转置代替；输入声明和实际结构必须一致。',
           ['实对称或复厄密结构已通过输入检查', '上、下三角存储由用户明确声明'],
           ['拒绝非厄密输入；不得静默对称化改变原算子'], [SCIPY, SLEPC]),
    _entry('shift_invert', '位移反演与近奇异恢复',
           '位移反演通过求解移位线性方程寻找目标附近的模式。工作位移的恢复尝试仍须对原问题独立验证。',
           ['移位线性方程可稳定求解', '目标中心与工作位移分别记录'],
           ['近奇异分解可能失败', '稀疏分解填充和内存需求依赖矩阵'], [SCIPY, SLEPC]),
    _entry('subspace', '候选子空间与内存预算',
           '子空间是迭代中保留的候选方向集合。扩大它可能改善谱覆盖，也会增加内存；建议只在原预算内试验。',
           ['候选维数大于目标数量并满足当前后端限制', '预估与运行监控均满足冻结资源预算'],
           ['扩大子空间不保证找到全部目标', '资源指标缺失不能当成零消耗'], [SCIPY, SLEPC]),
    _entry('clusters', '重根与模式簇',
           '重根允许多个独立向量对应同一特征值。比较时应关注整个子空间和正交性，不能只比较单个向量的符号或分量。',
           ['使用原问题残差和独立向量验证', '模式簇边界及目标数量保持冻结'],
           ['不得通过删除重根或修改目标数量宣称通过'], [SLEPC]),
    _entry('lobpcg', 'LOBPCG（局部最优块预条件共轭梯度法）适用前提',
           '块方法同时处理一组向量。它常用于谱两端；直接切换不能保证仍满足内部最近模式目标。',
           ['算子为实对称或复厄密', '广义问题的质量矩阵 B 必须为厄密正定',
            '明确预处理器适用性，确认后端实际支持', '目标等价性和小型稠密投影符合任务约束'],
           ['SciPy 在块大小过大时可能采用稠密求解', '不得因建议存在就声称后端已实现'], [LOBPCG, SLEPC]),
    _entry('generalized', '广义厄密问题的矩阵前提',
           '广义问题求解 A x = λ B x；需要同时登记两个输入，按具体算法确认 B 的结构及定性，并使用原方程残差。',
           ['A 与 B 的厄密性均有检查证据', '质量矩阵定性满足所选算法，而非仅有元数据声明',
            '后端实际支持此问题类型并验证 B 内积下的正交性'],
           ['不能仅省略 B 转成标准问题', '不同位移或算法对 B 的正定、半正定条件不同'], [SCIPY, LOBPCG, SLEPC]),
    _entry('pml', 'PML（完美匹配层，吸收出射波的人工边界）风险',
           'PML 的复坐标伸缩会改变离散算子；有电磁来源或复数元素并不能证明矩阵厄密。',
           ['在包含吸收层的最终离散矩阵上重新检查共轭对称', '确认当前厄密后端适用，失败时保留诊断'],
           ['禁止去掉虚部或对称化以强行通过', '无模型映射不得把特征值直接解释为频率'], [MFEM, DOLFINX, SCIPY]),
)


def list_knowledge(query=None):
    """Return independent JSON entries; no model or network is needed."""
    if query is not None and (not isinstance(query, str) or len(query) > 200):
        raise ValueError('Knowledge query must be at most 200 characters')
    needle = (query or '').strip().casefold()
    return copy.deepcopy([entry for entry in _ENTRIES if not needle or needle in str(entry).casefold()])


def get_knowledge(entry_id):
    for entry in _ENTRIES:
        if entry['id'] == entry_id:
            return copy.deepcopy(entry)
    raise KeyError(entry_id)


def diagnose(job=None, config=None, *, matrix=None, capabilities=None):
    """Describe observed evidence; never infer success or inspect matrix values."""
    job, matrix, capabilities = job or {}, matrix or {}, capabilities or {}
    config = resolve_config(job.get('config') or config or {})
    result = job.get('result') or {}
    error = job.get('error') or {}
    code = error.get('code') or result.get('error_code') or ''
    attempt_codes = [row.get('error_code') for row in result.get('attempts', []) if isinstance(row, dict)]
    solver, target, resources = (config[key] for key in ('solver', 'target', 'resources'))
    k = target.get('k') or min(target['max_modes'], 20)
    subspace = solver['subspace_max']
    n = matrix.get('n')
    if type(n) is not int or n < 2:
        n = None
    ceiling = min(n - 1 if n else 10000, capabilities.get('limits', {}).get('max_subspace', 10000))
    larger = min(ceiling, subspace + max(k, 20))
    # This is only a conservative vector estimate. Fill-in remains unknown.
    fits = n is not None and n * larger * 32 < resources['memory_limit_gib'] * 1024**3 * .5
    grow = {'solver': {'subspace_max': larger}} if larger > subspace and fits else {}
    smaller = max(2 * k + 1, subspace // 2, 3)
    reduce = {'solver': {'subspace_max': smaller}} if smaller < subspace else {}
    metadata = matrix.get('metadata') or job.get('matrix_metadata') or {}
    cases = [
        ('non_hermitian', {'NON_HERMITIAN', 'NOT_HERMITIAN'}, '核对三角声明和源数据',
         '输入未通过厄密检查；修正数据来源后新建输入版本。', {}, ['hermitian']),
        ('near_singular', {'SINGULAR_SHIFT', 'FACTORIZATION_FAILED', 'SHIFT_SINGULAR', 'NEAR_SINGULAR'},
         '启用有界工作位移恢复', '分解接近奇异；保持原目标中心并记录每次工作位移。',
         {'solver': {'adaptive_shift': {'enabled': True}}}, ['shift_invert']),
        ('inner_not_converged', {'INNER_NOT_CONVERGED', 'LINEAR_NOT_CONVERGED', 'LINEAR_RESIDUAL_FAILED'},
         '检查内层线性方程求解', '内层求解未达到约定误差；先检查分解、预处理及工作位移，保留原验收门槛。',
         {'solver': {'adaptive_shift': {'enabled': True}}}, ['shift_invert']),
        ('subspace_insufficient', {'SUBSPACE_INSUFFICIENT', 'SUBSPACE_LIMIT', 'ARPACK_NO_CONVERGENCE', 'NOT_CONVERGED', 'NO_VALID_MODES'},
         '在原内存预算内检查候选覆盖', '候选子空间或迭代未满足需求；增大维数仅是探索，不能替代完整性认证。', grow, ['subspace']),
        ('repeated_modes', {'CLUSTER_DEGENERACY', 'REPEATED_EIGENVALUES', 'ORTHOGONALITY_FAILED'},
         '按模式簇检查重根和正交性', '检查整个簇的独立向量及子空间，保留模式重数。', grow, ['clusters', 'subspace']),
        ('memory_exhausted', {'RESOURCE_EXHAUSTED', 'MEMORY_LIMIT_EXCEEDED', 'RESOURCE_ESTIMATE_EXCEEDED'},
         '减少候选子空间并检查资源构成', '进程树、输出或预估触及冻结预算；资源硬限额不扩大。', reduce, ['subspace', 'shift_invert']),
        ('timeout', {'WALLTIME_EXCEEDED', 'TIMEOUT', 'TIMED_OUT'}, '限制下一次数值尝试',
         '运行超过冻结时限；新实验减少外层尝试，目标、误差与时限保持不变。',
         {'solver': {'max_outer_iterations': max(1, solver['max_outer_iterations'] // 2)}}, ['subspace']),
        ('backend_unavailable', {'BACKEND_UNAVAILABLE', 'UNSUPPORTED_BACKEND'}, '选择经验证的 CPU 路径',
         '请求的可选后端缺失；创建新配置后仍须重新检查计划与能力。',
         {'solver': {'backend': 'scipy', 'method': 'arpack', 'transform': 'shift_invert', 'fallback_profiles': []}}, ['hermitian', 'shift_invert']),
        ('target_unverified', {'INCOMPLETE_TARGET', 'TARGET_UNVERIFIED'}, '检查目标完整性证据',
         '合格残差不能单独证明最近模式或区间完整性；未认证状态必须保留。', grow, ['subspace', 'clusters']),
        ('worker_lost', {'WORKER_LOST', 'IO_ERROR'}, '保留故障记录并新建实验',
         '当前运行无法继续；新实验不是原进程检查点的精确续算。', {}, ['subspace']),
    ]
    rules, suggestions, used = [], [], set()

    def add_rule(rule_id, triggered, title, reason, changes, knowledge_ids, evidence):
        entry = {'id': rule_id, 'triggered': bool(triggered), 'evidence': evidence, 'knowledge_ids': knowledge_ids}
        rules.append(entry)
        if triggered:
            apply_solver_advice(config, changes)
            used.update(knowledge_ids)
            prerequisites = list(dict.fromkeys(text for key in knowledge_ids for text in get_knowledge(key)['prerequisites']))
            suggestions.append({'id': f'{RULE_VERSION}:{rule_id}', 'rule_id': rule_id, 'title': title,
                                'reason': reason, 'changes': copy.deepcopy(changes), 'evidence': copy.deepcopy(evidence),
                                'knowledge_ids': knowledge_ids, 'prerequisites': prerequisites})

    for rule_id, codes, title, reason, changes, knowledge_ids in cases:
        matched_attempts = [value for value in attempt_codes if value in codes or
                            (rule_id == 'near_singular' and value == 'Factor is exactly singular')]
        evidence = [{'field': 'error.code', 'value': code or None, 'condition': 'one_of:' + ','.join(sorted(codes))}]
        evidence.extend({'field': 'result.attempts.error_code', 'value': value, 'condition': 'observed_failed_attempt'} for value in matched_attempts)
        add_rule(rule_id, code in codes or bool(matched_attempts), title, reason, changes, knowledge_ids, evidence)
    for rule_id, triggered, field, value, title, reason, knowledge_id in [
        ('lobpcg_prerequisites', solver['method'] == 'lobpcg', 'solver.method', solver['method'],
         '确认 LOBPCG 适用前提', '切换算法前确认算子、质量矩阵、目标等价性和实际后端。', 'lobpcg'),
        ('generalized_prerequisites', config['problem']['type'] == 'generalized_hermitian', 'problem.type', config['problem']['type'],
         '确认广义问题的第二矩阵与后端', '必须保留 A、B 输入及原广义方程验证。', 'generalized'),
        ('pml_structure', metadata.get('pml') is True or metadata.get('has_pml') is True, 'matrix_metadata.pml',
         metadata.get('pml', metadata.get('has_pml')), '检查 PML 后的最终算子',
         'PML 元数据触发风险提示；实际是否厄密必须由最终矩阵检查决定。', 'pml'),
    ]:
        add_rule(rule_id, triggered, title, reason, {}, [knowledge_id],
                 [{'field': field, 'value': value, 'condition': 'requires_explicit_prerequisites'}])
    if not suggestions:
        used.add('hermitian')
        suggestions.append({'id': f'{RULE_VERSION}:review', 'rule_id': 'review', 'title': '检查验证与运行证据',
                            'reason': '未触发已登记的故障规则；这不构成数值通过声明。', 'changes': {},
                            'evidence': [{'field': 'error.code', 'value': code or None, 'condition': 'no_matching_rule'}],
                            'knowledge_ids': ['hermitian'], 'prerequisites': get_knowledge('hermitian')['prerequisites']})
    return {'source': 'rules', 'rule_version': RULE_VERSION, 'registry_version': REGISTRY_VERSION,
            'suggestions': suggestions, 'rules': rules, 'knowledge': [get_knowledge(key) for key in sorted(used)],
            'ai_available': False}
