"""Versioned, shared configuration used by CLI, API and numerical workers."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NullspaceMetadata(StrictModel):
    """Coordinate nullspace declarations are evidence to check, never a filter."""
    basis_indices: list[int] = Field(default_factory=list, max_length=512)
    interpretation: Literal["unspecified", "expected_nullspace"] = "unspecified"
    source: str = Field(default="", max_length=256)

    @model_validator(mode="after")
    def valid_basis(self):
        if any(index < 0 for index in self.basis_indices) or len(set(self.basis_indices)) != len(self.basis_indices):
            raise ValueError("INVALID_NULLSPACE: nonnegative unique coordinate indices required")
        return self


class Problem(StrictModel):
    type: Literal["standard_hermitian", "generalized_hermitian"] = "standard_hermitian"
    matrix_id: str = ""
    matrix_b_id: str | None = None
    storage: Literal["full", "upper", "lower"] = "full"
    scalar: Literal["auto", "float64", "complex128"] = "auto"
    hermitian_rtol: float = Field(default=1e-12, gt=0, le=1e-3)
    reordering: Literal["none", "reverse_cuthill_mckee"] = "none"
    nullspace: NullspaceMetadata = Field(default_factory=NullspaceMetadata)


class Interval(StrictModel):
    lower: float
    upper: float
    closure: Literal["left_closed_right_open"] = "left_closed_right_open"
    boundary_atol: float = Field(default=1e-10, ge=0)

    @model_validator(mode="after")
    def valid_bounds(self):
        if self.lower >= self.upper:
            raise ValueError("INVALID_INTERVAL: lower must be less than upper")
        return self


class Target(StrictModel):
    mode: Literal["nearest_k", "interval_all"] = "nearest_k"
    k: int | None = Field(default=20, ge=1)
    sigma: float | None = 0.0
    interval: Interval | None = None
    sort: Literal["distance_then_value"] = "distance_then_value"
    cluster_policy: Literal["fixed_k", "cluster_complete"] = "fixed_k"
    max_modes: int = Field(default=200, ge=1, le=10000)

    @model_validator(mode="before")
    @classmethod
    def interval_defaults(cls, value):
        if isinstance(value, dict) and value.get("mode") == "interval_all":
            value = {**value}
            value.setdefault("k", None)
            value.setdefault("sigma", None)
        return value

    @model_validator(mode="after")
    def coherent_target(self):
        if self.mode == "nearest_k":
            if self.k is None or self.sigma is None or self.interval is not None:
                raise ValueError("INVALID_TARGET: nearest_k requires k and sigma only")
        elif self.interval is None or self.k is not None or self.sigma is not None:
            raise ValueError("INVALID_TARGET: interval_all requires interval and null k/sigma")
        return self


class AdaptiveShift(StrictModel):
    enabled: bool = True
    max_attempts: int = Field(default=4, ge=1, le=16)
    policy: Literal["feedback_bounded_v1","observed_ritz_gap_v2"] = "observed_ritz_gap_v2"


class AdaptiveAccuracy(StrictModel):
    enabled: StrictBool = False
    rtol_min: float = Field(default=1e-10, gt=0, le=1e-2)
    rtol_max: float = Field(default=1e-2, gt=0, le=1e-2)
    forcing_factor: float = Field(default=0.1, gt=0, le=1)

    @model_validator(mode='after')
    def coherent_tolerances(self):
        if self.rtol_min > self.rtol_max:
            raise ValueError('INVALID_INNER_ACCURACY: rtol_min must not exceed rtol_max')
        return self


class Linear(StrictModel):
    method: str = "sparse_lu"
    preconditioner: str = "auto_budgeted"
    rtol_initial: float = Field(default=1e-10, gt=0, le=1e-2)
    max_iterations: int = Field(default=1000, ge=1)
    adaptive_accuracy: AdaptiveAccuracy = Field(default_factory=AdaptiveAccuracy)
    precision: Literal['float64','mixed_float32'] = 'float64'


class BlockIteration(StrictModel):
    oversampling: int = Field(default=4, ge=1, le=128)
    residual_check_interval: int = Field(default=1, ge=1, le=100)
    max_restarts: int = Field(default=2, ge=0, le=16)
    stagnation_checks: int = Field(default=20, ge=2, le=1000)
    checkpoint_interval: int = Field(default=0, ge=0, le=1000000)
    checkpoint_kind: Literal['subspace','exact_block'] = 'subspace'


class Solver(StrictModel):
    backend: Literal["scipy", "slepc"] = "scipy"
    method: Literal["arpack", "auto", "block_shift_invert", "krylovschur", "jacobi_davidson", "lobpcg"] = "arpack"
    transform: Literal["shift_invert", "precond", "none"] = "shift_invert"
    subspace_max: int = Field(default=100, ge=3, le=10000)
    max_outer_iterations: int = Field(default=2000, ge=1, le=1000000)
    random_seed: int = Field(default=20261016, ge=0)
    fallback_profiles: list[str] = Field(default_factory=list)
    adaptive_shift: AdaptiveShift = Field(default_factory=AdaptiveShift)
    linear: Linear = Field(default_factory=Linear)
    block: BlockIteration = Field(default_factory=BlockIteration)


class Validation(StrictModel):
    residual_definition: Literal["scaled_inf_v1"] = "scaled_inf_v1"
    residual_scaled_max: float = Field(default=1e-8, gt=0, le=1e-2)
    absolute_residual_max: float | None = Field(default=None, gt=0)
    orthogonality_max: float = Field(default=1e-6, gt=0, le=1e-1)
    completeness_required: bool = True


class Resources(StrictModel):
    memory_limit_gib: float = Field(default=16, gt=0, le=1024)
    # A portable default must fit even a single-CPU control/workstation host.
    # Explicit requests remain unchanged and are checked by planning/admission.
    threads: int = Field(default=1, ge=1, le=256)
    mpi_ranks: int = Field(default=1, ge=1, le=256)
    device: Literal["cpu", "gpu"] = "cpu"
    walltime_seconds: float = Field(default=1800, gt=0, le=86400)
    disk_limit_gib: float = Field(default=20, gt=0, le=4096)


class Observability(StrictModel):
    enabled: StrictBool = True
    metrics_interval_ms: int = Field(default=1000, ge=50, le=60000)
    redact_sensitive_fields: bool = True


class Output(StrictModel):
    eigenvectors: bool = True
    format: Literal["numpy", "scipy", "both"] = "numpy"
    atomic_write: bool = True


class SolveConfig(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    problem: Problem = Field(default_factory=Problem)
    target: Target = Field(default_factory=Target)
    solver: Solver = Field(default_factory=Solver)
    validation: Validation = Field(default_factory=Validation)
    resources: Resources = Field(default_factory=Resources)
    observability: Observability = Field(default_factory=Observability)
    output: Output = Field(default_factory=Output)


def resolve_config(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("INVALID_CONFIG: configuration must be an object")
    return SolveConfig.model_validate(value).model_dump(mode="json")


def content_hash(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


TERMINAL_STATES = frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "TIMED_OUT"})
EXIT_CODES = {"SUCCEEDED": 0, "PARTIAL": 4, "FAILED": 9, "CANCELLED": 8, "TIMED_OUT": 10}
ERROR_EXIT_CODES = {"INVALID_CONFIG": 2, "INVALID_INPUT": 2, "NOT_CONVERGED": 3,
                    "PRECONDITIONER_FAILED": 3,
                    "RESOURCE_EXHAUSTED": 5, "BACKEND_UNAVAILABLE": 6,
                    "VALIDATION_FAILED": 7}
