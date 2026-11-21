"""Map the existing TU-02R v2 metadata contract into pinned readiness v3.

This module neither authenticates source assertions nor authorizes live admission.
Call it with already sanitized, upstream-authenticated metadata for ONE account.
There is no collector, provider call, scheduler, file writer, or quota arithmetic.
"""

from __future__ import annotations

import hashlib
import json
import sys
from importlib.abc import SourceLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_PINS = {
    "reset-observation-contract-v2.json": (
        "5cc8b9a4b4b622c4443bc6d14cd5bffc754a891052cc9e70e39ff984994dde38"
    ),
    "reset_readiness_v3.py": "50f8290e62247cfb645d1917e736481598c8bd3ce25582b558d952b96c0e544e",
}


class _VerifiedSourceLoader(SourceLoader):
    """Load only the source bytes already checked against the dependency pin."""

    def __init__(self, source_path: Path, verified_source: bytes):
        self._source_path = str(source_path)
        self._verified_source = verified_source

    def get_filename(self, fullname):
        return self._source_path

    def get_data(self, path):
        if path != self._source_path:
            raise OSError("reset_adapter_unverified_source_path")
        return self._verified_source

    def path_stats(self, path):
        # SourceLoader uses this metadata to admit or write bytecode caches.
        raise OSError("reset_adapter_bytecode_cache_disabled")

    def set_data(self, path, data):
        raise OSError("reset_adapter_bytecode_cache_disabled")


def _load_inputs():
    verified = {}
    for name, expected in _PINS.items():
        content = (_ROOT / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise RuntimeError("reset_adapter_dependency_pin_mismatch:" + name)
        verified[name] = content
    contract = json.loads(verified["reset-observation-contract-v2.json"])
    name = "_tu02r_readiness_" + _PINS["reset_readiness_v3.py"][:12]
    loader = _VerifiedSourceLoader(
        _ROOT / "reset_readiness_v3.py", verified["reset_readiness_v3.py"]
    )
    spec = spec_from_loader(name, loader)
    if spec is None:
        raise RuntimeError("reset_adapter_module_spec_unavailable")
    module = module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return frozenset(contract["required_fields"]), module


_FIELDS, _READINESS = _load_inputs()
_RENAMES = {
    "limit_scope_id": "scope_id",
    "source_attested": "attested",
    "clock_skew_bound_seconds": "clock_skew_bound_s",
    "restoration_attestation": "restoration_attested",
}


def _bounded_result(result):
    # READY remains a metadata classification, never a live release decision.
    return dict(
        result,
        pooled_capacity=None,
        live_admission_authorized=False,
        adapter_schema="tu.reset-contract-adapter.v1",
    )


def evaluate_observations(
    *, now, observations, required_scopes, max_source_age_s, max_poll_age_s, max_clock_skew_s
):
    """Return the pinned v3 verdict for v2 JSON observation records.

    Every v2 required field must be present, including explicit null unknowns.
    Alternate/runtime aliases and additional fields are refused, never guessed.
    Timestamps stay in their supplied clock; flags and counters are not coerced.
    Duplicate observations are passed to v3 for rejection, never collapsed.
    Accounts must be evaluated separately; the pinned v3 rejects mixed accounts.
    """
    reasons = []
    if not isinstance(observations, (list, tuple)):
        reasons.append("invalid_observations")
    else:
        for index, observation in enumerate(observations):
            if type(observation) is not dict:
                reasons.append(f"observation_{index}:invalid_record")
            elif set(observation) != _FIELDS:
                if _FIELDS - set(observation):
                    reasons.append(f"observation_{index}:missing_contract_fields")
                if set(observation) - _FIELDS:
                    reasons.append(f"observation_{index}:unknown_contract_fields")
    if reasons:
        return _bounded_result(
            {"state": "HOLD", "reasons": reasons, "capacity": None, "cumulative_debit": None}
        )
    buckets = [
        _READINESS.Bucket(**{_RENAMES.get(key, key): value for key, value in observation.items()})
        for observation in observations
    ]
    result = _READINESS.readiness(
        now=now,
        buckets=buckets,
        required_scopes=required_scopes,
        max_source_age_s=max_source_age_s,
        max_poll_age_s=max_poll_age_s,
        max_clock_skew_s=max_clock_skew_s,
    )
    return _bounded_result(result)
