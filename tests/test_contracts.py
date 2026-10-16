import pytest

from emsolver.contracts import resolve_config


def test_configuration_resolves_defaults_and_does_not_mutate_request():
    raw = {"problem": {"matrix_id": "matrix_1"}, "target": {"k": 3}}
    result = resolve_config(raw)
    assert result["solver"]["backend"] == "scipy"
    assert result["target"]["sigma"] == 0.0
    assert "solver" not in raw


@pytest.mark.parametrize("patch", [
    {"unexpected": 1},
    {"target": {"k": 0}},
    {"target": {"sigma": float("nan")}},
    {"resources": {"memory_limit_gib": -1}},
    {"problem": {"type": "pretend_gpu_problem"}},
])
def test_invalid_configuration_is_rejected(patch):
    with pytest.raises(ValueError):
        resolve_config(patch)


def test_interval_contract_requires_bounds_and_clears_nearest_fields():
    result = resolve_config({"target": {"mode": "interval_all", "k": None, "sigma": None,
                                       "interval": {"lower": -1, "upper": 1}}})
    assert result["target"]["interval"]["closure"] == "left_closed_right_open"
    with pytest.raises(ValueError):
        resolve_config({"target": {"mode": "interval_all", "interval": {"lower": 1, "upper": -1}}})
