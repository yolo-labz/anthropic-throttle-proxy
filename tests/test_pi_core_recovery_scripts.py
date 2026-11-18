"""Collect the pi-core-recovery script tests under ``testpaths``.

``scripts/pi-core-recovery/test_recover_{core,idle}.py`` pass (59 cases) but sit
outside ``testpaths = ["tests"]``, so CI never ran them and Sonar counted both
the scripts and those tests as 0 % covered new code. Re-export them here; the
modules are loaded by path because the directory name is not importable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_DIR = Path(__file__).resolve().parents[1] / "scripts" / "pi-core-recovery"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"pi_core_recovery_{name}", _DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


for _mod in (_load("test_recover_core"), _load("test_recover_idle")):
    for _name, _obj in vars(_mod).items():
        # Tests plus the fixtures they request (e.g. ``recovery_seat``).
        if (_name.startswith("test_") and callable(_obj)) or hasattr(_obj, "_fixture_function"):
            assert _name not in globals(), f"duplicate test name {_name}"
            globals()[_name] = _obj
