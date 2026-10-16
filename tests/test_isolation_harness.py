from pathlib import Path
import importlib.util


def test_candidate_mounts_only_explicit_files_and_output_no_reference_parent():
    path=Path(__file__).resolve().parents[1]/'scripts'/'run_isolated.py'
    spec=importlib.util.spec_from_file_location('isolated_harness',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    options=module.container_options(Path('/private/case.npz'),Path('/private/cfg.yaml'),Path('/results'),{'memory_limit_gib':2,'threads':1})
    mounts=[options[i+1] for i,arg in enumerate(options) if arg=='--mount']
    assert len(mounts)==3
    assert 'src=/private,' not in ','.join(mounts)
    assert sum(mount.endswith(',readonly') for mount in mounts)==2
    assert '--read-only' in options and options[options.index('--network')+1]=='none'
    assert options[options.index('--cap-drop')+1]=='ALL'
    assert 'no-new-privileges' in options and '--privileged' not in options
