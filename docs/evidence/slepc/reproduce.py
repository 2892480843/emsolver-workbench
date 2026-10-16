"""Capture bounded synthetic SLEPc evidence; reference is computed after solving."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
from datetime import datetime, timezone

import numpy as np
from scipy import sparse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from emsolver.contracts import resolve_config
from emsolver.integrity import file_hash
from emsolver.numerics.core import run_solver, verify_run
import emsolver.optional_backends as optional


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New, nonexistent evidence directory')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    executable, configured, environment, _ = optional._settings()
    profile = optional.probe_slepc()
    assert profile['supported'] is True
    image = profile['image_id']
    write(args.output / 'capabilities.json', profile)
    build_profiles = []
    for scalar in ('real', 'complex'):
        for ranks in (1, 2):
            value = optional._invoke_json([executable, 'run', '--rm', '--pull=never', '--network=none', '--read-only',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--entrypoint', '/bin/cat', image,
                f'/opt/emsolver/profile-{scalar}-{ranks}.json'], environment)
            assert value['driver_sha256'] == profile['driver_sha256'] and value['self_test_passed'] is True
            build_profiles.append(value)
    write(args.output / 'image-build-selftests.json', build_profiles)
    packages = ['python3', 'python3-numpy', 'python3-scipy', 'python3-mpi4py', 'python3-petsc4py-real',
                'python3-petsc4py-complex', 'python3-slepc4py-real', 'python3-slepc4py-complex', 'openmpi-bin']
    receipt = subprocess.run([executable, 'run', '--rm', '--pull=never', '--network=none', '--read-only',
        '--entrypoint', '/usr/bin/dpkg-query', image, '-W', '-f=${Package}\t${Version}\n', *packages],
        env=environment, check=True, capture_output=True, text=True, timeout=15)
    (args.output / 'packages.tsv').write_text(receipt.stdout, encoding='utf-8')
    records = []
    original_command = optional.sandbox_command
    active = {}

    def capture_command(*positional, **keywords):
        command = original_command(*positional, **keywords)
        if not keywords.get('probe'):
            active['name'] = positional[2]
        return command

    optional.sandbox_command = capture_command
    try:
        for scalar in ('float64', 'complex128'):
            for ranks in (1, 2):
                case = args.output / f'{scalar}-mpi{ranks}'
                case.mkdir()
                off = np.full(59, .035) * (np.exp(.4j) if scalar == 'complex128' else 1.)
                matrix = sparse.diags([off, np.linspace(-4., 5., 60), off.conjugate()], [-1, 0, 1], format='csr')
                input_path = case / 'matrix.npz'
                sparse.save_npz(input_path, matrix)
                config = resolve_config({'problem': {'scalar': scalar, 'reordering': 'reverse_cuthill_mckee'},
                    'target': {'k': 3, 'sigma': .1},
                    'solver': {'backend': 'slepc', 'method': 'krylovschur', 'subspace_max': 24, 'random_seed': 20261016},
                    'resources': {'threads': 1, 'mpi_ranks': ranks, 'memory_limit_gib': 2, 'walltime_seconds': 30,
                                  'disk_limit_gib': .1}})
                fences, events = {}, []

                def observe(event):
                    events.append(event)
                    if event['event'] != 'slepc_iteration' or fences:
                        return
                    inspected = optional._invoke_json([executable, 'inspect', '--format', '{{json .}}', active['name']], environment)
                    host = inspected['HostConfig']
                    fences.update(image_id=inspected['Image'], network_mode=host['NetworkMode'],
                        readonly_rootfs=host['ReadonlyRootfs'], memory_bytes=host['Memory'],
                        memory_swap_bytes=host['MemorySwap'], pids_limit=host['PidsLimit'], nano_cpus=host['NanoCpus'],
                        cap_drop=host['CapDrop'], security_options=host['SecurityOpt'],
                        mounts=[{'destination': mount['Destination'], 'read_write': mount['RW']} for mount in inspected['Mounts']])

                result = run_solver(input_path, config, case / 'output', event_callback=observe)
                validation = verify_run(input_path, case / 'output', config)
                # This independent reference only exists after the isolated candidate container exits.
                reference = np.linalg.eigvalsh(matrix.toarray())
                target = reference[np.argsort(abs(reference - .1), kind='stable')[:3]]
                values = np.load(case / 'output/eigenvalues.npy', allow_pickle=False)
                vectors = np.load(case / 'output/eigenvectors.npy', allow_pickle=False)
                residuals = np.linalg.norm(matrix @ vectors - vectors * values, axis=0)
                expected_error = max(float(np.min(abs(values - value))) for value in target)
                assert validation['numerical_status'] == 'PASSED' and validation['n_validated'] == 3
                assert validation['artifact_integrity'] == 'PASSED' and expected_error < 1e-10
                assert fences['memory_bytes'] == 1024**3 and fences['memory_swap_bytes'] == 1024**3
                assert fences['image_id'] == image and fences['network_mode'] == 'none' and fences['readonly_rootfs']
                assert {m['destination']: m['read_write'] for m in fences['mounts']} == {'/input': False, '/output': True}
                assert not list(case.glob('.slepc-*'))
                write(case / 'events.json', events)
                write(case / 'independent-validation.json', validation)
                write(case / 'sandbox.json', fences)
                write(case / 'result.json', result)
                records.append({'case': case.name, 'n': 60, 'is_diagonal': False, 'scalar': scalar, 'mpi_ranks': ranks,
                    'seed': 20261016, 'input_sha256': file_hash(input_path),
                    'resolved_config_sha256': file_hash(case / 'output/config.resolved.yaml'),
                    'status': result['status'], 'n_accepted': result['n_accepted'],
                    'numerical_status': validation['numerical_status'], 'target_certification': validation['target_certification'],
                    'artifact_integrity': validation['artifact_integrity'], 'eigenvalues': values.tolist(),
                    'max_absolute_residual_2': float(max(residuals)), 'max_reference_eigenvalue_error': expected_error,
                    'orthogonality_error_2': float(np.linalg.norm(vectors.conjugate().T @ vectors - np.eye(3), ord=2)),
                    'reference_scope': 'post-solve 60x60 dense reference on host; never mounted into candidate container',
                    'total_job_memory_limit_bytes': result['metrics']['total_job_memory_limit_bytes'],
                    'host_memory_limit_bytes': result['metrics']['host_memory_limit_bytes'],
                    'container_memory_limit_bytes': result['metrics']['container_memory_limit_bytes'],
                    'container_removed_and_private_workspace_cleaned': True})
    finally:
        optional.sandbox_command = original_command
    archive = ROOT / 'artifacts/emsolver-slepc-bookworm-arm64-v1.docker.tar.gz'
    metadata = {'schema_version': '1.0', 'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
        'host': {'system': platform.system(), 'machine': platform.machine(), 'python': platform.python_version()},
        'image_tag': configured, 'image_id': image, 'container_platform': 'linux/arm64',
        'driver_sha256': profile['driver_sha256'],
        'source_sha256': {name: file_hash(ROOT / name) for name in ('Dockerfile.slepc', 'emsolver/optional_backends.py',
                           'emsolver/resource_budgets.py', 'emsolver/numerics/slepc_driver.py', 'emsolver/numerics/slepc.py')},
        'cases': records, 'denominator': len(records),
        'archive': {'path': 'artifacts/' + archive.name, 'sha256': file_hash(archive), 'bytes': archive.stat().st_size,
                    'load_verified': True, 'load_log': 'Loaded image: emsolver-slepc:bookworm-arm64-v1'},
        'scope': 'Local functional correctness and isolation evidence; no scaling, acceleration, CUDA or general speed claim'}
    write(args.output / 'validation.json', metadata)
    print(json.dumps({'cases': len(records), 'statuses': [r['status'] for r in records],
                      'numerical_statuses': [r['numerical_status'] for r in records], 'image_id': image}))


if __name__ == '__main__':
    main()
