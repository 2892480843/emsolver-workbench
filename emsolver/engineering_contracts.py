"""Closed engineering metadata; coordinates never imply a physical model."""
from __future__ import annotations
import math
from typing import Literal
from urllib.parse import urlsplit, parse_qsl

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .errors import ServiceError
from .reporting import PRIVATE_PATH, credential_query


MAX_MANIFEST_BYTES = 16 * 1024**2
MAX_DOFS = 200000


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class Source(Strict):
    title: str = Field(min_length=1, max_length=256)
    url: str = Field(min_length=1, max_length=2000)

    @model_validator(mode='after')
    def safe_source(self):
        parsed = urlsplit(self.url)
        generated = self.url in {'urn:emsolver:analytic-chain:v1', 'urn:emsolver:rectangular-pec-tmz-fd:v1'}
        if not generated and (parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username is not None or parsed.password is not None or parsed.fragment or any(credential_query(key) for key, _ in parse_qsl(parsed.query))):
            raise ValueError('Invalid source URI')
        if PRIVATE_PATH.search(self.title): raise ValueError('Private source path')
        return self


class FrequencyMapping(Strict):
    kind: Literal['omega_squared', 'angular_frequency', 'frequency_hz', 'wavenumber_squared']
    eigenvalue_units: Literal['s^-2', 'rad/s', 'Hz', 'm^-2']
    reviewed: StrictBool
    applicability: str = Field(min_length=1, max_length=2000)
    wave_speed_m_per_s: float | None = Field(default=None, gt=0, le=1e10)

    @model_validator(mode='after')
    def coherent(self):
        units = {'omega_squared': 's^-2', 'angular_frequency': 'rad/s', 'frequency_hz': 'Hz', 'wavenumber_squared': 'm^-2'}
        if not self.reviewed or self.eigenvalue_units != units[self.kind]: raise ValueError('Reviewed equation and units required')
        if (self.kind == 'wavenumber_squared') != (self.wave_speed_m_per_s is not None): raise ValueError('Wave speed applies only to squared wavenumber')
        if PRIVATE_PATH.search(self.applicability): raise ValueError('Private applicability path')
        return self


class Cell(Strict):
    kind: Literal['line', 'triangle', 'tetra']
    nodes: list[int] = Field(min_length=2, max_length=4)


class Basis(Strict):
    indices: list[int] = Field(min_length=1, max_length=100000)
    real: list[float] = Field(min_length=1, max_length=100000)
    imag: list[float] | None = Field(default=None, max_length=100000)


class Nullspace(Strict):
    interpretation: Literal['unspecified', 'expected_nullspace'] = 'unspecified'
    basis: list[Basis] = Field(min_length=1, max_length=16)
    residual_tolerance: float = Field(default=1e-12, gt=0, le=1e-8)


class EngineeringMetadata(Strict):
    schema_version: Literal['1.0']
    model_version: str = Field(min_length=1, max_length=128)
    model_kind: Literal['user_supplied', 'analytic_demo'] = 'user_supplied'
    matrix_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    authorized: StrictBool
    public: StrictBool = False
    source: Source
    license: str = Field(min_length=1, max_length=256)
    coordinate_units: Literal['m', 'cm', 'mm']
    field_units: str = Field(min_length=1, max_length=128)
    frequency_mapping: FrequencyMapping | None = None

    @model_validator(mode='after')
    def authorization(self):
        if not self.authorized: raise ValueError('Explicit project administrator authorization required')
        if any(PRIVATE_PATH.search(value) for value in (self.model_version, self.license, self.field_units)):
            raise ValueError('Private paths are not model provenance')
        return self


class EngineeringManifest(EngineeringMetadata):
    coordinates: list[list[float]] = Field(min_length=1, max_length=100000)
    dof_nodes: list[int] = Field(min_length=1, max_length=MAX_DOFS)
    dof_components: list[Literal['scalar', 'x', 'y', 'z']] = Field(min_length=1, max_length=MAX_DOFS)
    cells: list[Cell] = Field(default_factory=list, max_length=100000)
    nullspace: Nullspace | None = None

    @model_validator(mode='after')
    def structural(self):
        if any(len(point) != 3 or any(abs(value) > 1e12 for value in point) for point in self.coordinates): raise ValueError('Invalid node coordinates')
        n = len(self.coordinates)
        if len(self.dof_nodes) != len(self.dof_components) or any(not 0 <= node < n for node in self.dof_nodes): raise ValueError('Invalid DOF mapping')
        if len(set(zip(self.dof_nodes, self.dof_components))) != len(self.dof_nodes): raise ValueError('Duplicate node/component DOF')
        if 'scalar' in self.dof_components and set(self.dof_components) != {'scalar'}: raise ValueError('Scalar and vector fields cannot be mixed')
        for cell in self.cells:
            if len(cell.nodes) != {'line': 2, 'triangle': 3, 'tetra': 4}[cell.kind] or len(set(cell.nodes)) != len(cell.nodes) or any(not 0 <= node < n for node in cell.nodes):
                raise ValueError('Invalid cell topology')
        if self.model_kind == 'analytic_demo':
            if (n>512 or self.model_version!='analytic-chain-v1' or self.source.url!='urn:emsolver:analytic-chain:v1' or self.license!='CC0-1.0' or
                self.coordinate_units!='m' or self.field_units!='normalized' or self.public or self.frequency_mapping is not None or self.nullspace is not None or
                self.coordinates!=[[float(index),0.,0.] for index in range(n)] or self.dof_nodes!=list(range(n)) or self.dof_components!=['scalar']*n or
                [cell.model_dump(mode='json') for cell in self.cells]!=[{'kind':'line','nodes':[index,index+1]} for index in range(n-1)]):
                raise ValueError('Analytic demo must be the declared generated node chain')
        if self.nullspace:
            if sum(len(basis.indices) for basis in self.nullspace.basis) > 100000: raise ValueError('Nullspace entry budget')
            for basis in self.nullspace.basis:
                if (len(basis.indices) != len(basis.real) or (basis.imag is not None and len(basis.imag) != len(basis.indices)) or
                    len(set(basis.indices)) != len(basis.indices) or any(not 0 <= index < len(self.dof_nodes) for index in basis.indices) or
                    not any(value != 0 for value in basis.real + (basis.imag or []))): raise ValueError('Invalid sparse nullspace basis')
        return self


def normalize_manifest(value, matrix):
    try:
        model = EngineeringManifest.model_validate(value)
        if model.matrix_sha256 != matrix['hash'] or len(model.dof_nodes) != matrix['n']: raise ValueError('Matrix binding mismatch')
        return model.model_dump(mode='json')
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ServiceError('INVALID_ENGINEERING_MANIFEST', 'Model geometry, authorization, units or matrix binding is invalid') from None


def frequency_values(values, specification):
    try:
        spec = FrequencyMapping.model_validate(specification)
        frequencies = []
        for value in values:
            if type(value) not in (int, float) or not math.isfinite(value): raise ValueError('Nonfinite eigenvalue')
            if value < 0: result = None
            elif spec.kind == 'frequency_hz': result = float(value)
            elif spec.kind == 'angular_frequency': result = value / (2 * math.pi)
            else: result = math.sqrt(value) / (2 * math.pi) * (spec.wave_speed_m_per_s if spec.kind == 'wavenumber_squared' else 1.)
            if result is not None and not math.isfinite(result): raise ValueError('Frequency range exceeded')
            frequencies.append(result)
        return {'frequencies_hz': frequencies, 'frequency_mapping': {**spec.model_dump(mode='json'), 'frequency_units': 'Hz',
            'source': 'Frozen, explicitly reviewed user model; no new numerical certification', 'negative_values': 'No real nonnegative frequency; null'}}
    except (ValueError, TypeError, OverflowError):
        raise ServiceError('INVALID_FREQUENCY_MAPPING') from None


# Kept as a descriptive compatibility alias for domain callers.
validate_manifest = normalize_manifest
