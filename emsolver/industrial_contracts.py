"""Closed, bounded acceptance evidence. Declarations do not certify physics."""
from __future__ import annotations

from typing import Literal
from pydantic import Field, model_validator

from .engineering_contracts import Strict, Source
from .errors import ServiceError
from .reporting import PRIVATE_PATH

MAX_MANIFEST_BYTES = 1024**2
MAX_DOSSIER_BYTES = 4 * 1024**2
MAX_CHECK_BYTES = 512 * 1024
MAX_REFERENCE_ENTRIES = 50000
MAX_REFERENCE_MODES = 32


class Attestation(Strict):
    issuer: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')
    signature: str = Field(pattern=r'^[0-9a-f]{64}$')


class Evidence(Strict):
    source: Source
    artifact_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class Material(Strict):
    name: str = Field(min_length=1, max_length=128)
    relative_permittivity: float = Field(gt=0, le=1e12)
    relative_permeability: float = Field(gt=0, le=1e12)
    conductivity_s_per_m: float = Field(ge=0, le=1e12)


class Boundary(Strict):
    kind: Literal['PEC', 'PMC', 'periodic', 'mixed', 'unspecified']
    description: str = Field(min_length=1, max_length=2000)


class Mesh(Strict):
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    generator: str = Field(min_length=1, max_length=256)
    units: Literal['m', 'cm', 'mm']
    source: Source


class Physical(Strict):
    equation: str = Field(min_length=1, max_length=2000)
    assumptions: list[str] = Field(min_length=1, max_length=32)
    materials: list[Material] = Field(min_length=1, max_length=64)
    boundary: Boundary
    mesh: Mesh | None = None
    review: Evidence | None = None
    attestation: Attestation | None = None


class Reference(Evidence):
    matrix_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    target_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    method: Literal['independent_solver', 'measurement', 'analytic']
    producer: str = Field(min_length=1, max_length=256)
    artifact_id: str | None = Field(default=None, pattern=r'^ir_[0-9a-f]{32}$')
    eigenvalues: list[float] | None = Field(default=None, min_length=1, max_length=MAX_REFERENCE_MODES)
    vectors_real: list[list[float]] | None = Field(default=None, min_length=1, max_length=MAX_REFERENCE_ENTRIES)
    vectors_imag: list[list[float]] | None = None
    attestation: Attestation | None = None

    @model_validator(mode='after')
    def bounded_shape(self):
        if self.artifact_id is not None:
            if any(value is not None for value in (self.eigenvalues, self.vectors_real, self.vectors_imag)):
                raise ValueError('Reference artifact and inline data are mutually exclusive')
            return self
        if self.eigenvalues is None or self.vectors_real is None: raise ValueError('Reference data required')
        rows, columns = len(self.vectors_real), len(self.eigenvalues)
        if rows * columns > MAX_REFERENCE_ENTRIES: raise ValueError('Reference resource limit')
        if any(len(row) != columns for row in self.vectors_real): raise ValueError('Reference shape')
        if self.vectors_imag is not None and (len(self.vectors_imag) != rows or any(len(row) != columns for row in self.vectors_imag)):
            raise ValueError('Complex reference shape')
        return self


class Criteria(Strict):
    max_scaled_residual: float = Field(default=1e-8, gt=0, le=1e-6)
    max_eigenvalue_absolute_error: float = Field(default=1e-8, ge=0, le=1e6)
    max_eigenvalue_relative_error: float = Field(default=1e-6, ge=0, le=1e-2)
    max_subspace_sine: float = Field(default=1e-4, ge=0, le=1e-2)
    max_frequency_relative_error: float = Field(default=1e-6, ge=0, le=1e-2)
    max_walltime_seconds: float | None = Field(default=None, gt=0, le=8640000)
    max_peak_memory_bytes: int | None = Field(default=None, gt=0, le=65536 * 1024**3)
    min_speedup: float | None = Field(default=None, gt=0, le=1e6)


class Manifest(Strict):
    schema_version: Literal['1.0']
    title: str = Field(min_length=1, max_length=200)
    declared_use: Literal['teaching', 'research', 'industrial']
    acceptance_scope: Literal['matrix_eigenproblem', 'resonant_frequencies'] = 'matrix_eigenproblem'
    matrix_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,200}$')
    engineering_asset_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,200}$')
    candidate_job_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,200}$')
    baseline_job_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,200}$')
    physical: Physical | None = None
    reference: Reference | None = None
    criteria: Criteria = Field(default_factory=Criteria)


def validate_manifest(value):
    def safe_text(item):
        if isinstance(item, dict):
            for child in item.values(): safe_text(child)
        elif isinstance(item, list):
            for child in item: safe_text(child)
        elif isinstance(item, str) and (len(item) > 4096 or PRIVATE_PATH.search(item)):
            raise ValueError('Private path or oversized text')
    try:
        result = Manifest.model_validate(value).model_dump(mode='json')
        safe_text(result)
        return result
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ServiceError('INVALID_INDUSTRIAL_MANIFEST', 'Invalid, unbounded or sensitive acceptance evidence') from None
