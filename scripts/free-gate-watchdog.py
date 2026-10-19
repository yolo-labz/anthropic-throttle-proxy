#!/usr/bin/env python3
"""CLI shim for the free-gate per-bearer watchdog.

The implementation lives in ``anthropic_throttle_proxy.free_gate_watchdog`` so
it is importable, unit-tested and shipped in the wheel alongside the gates it
watches. This shim exists so an operator (or a systemd timer) can run it
straight out of a checkout without installing the package:

    python scripts/free-gate-watchdog.py --live
    python scripts/free-gate-watchdog.py --replay /tmp/go-gate-incident.log --floor 1

Exit status: 0 = no critical alert, 1 = at least one critical alert (or an
unusable input), so ``systemd`` ``OnFailure=`` and shell ``||`` both work.
"""

from __future__ import annotations

import pathlib
import sys

_REPO_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if _REPO_SRC.is_dir() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from anthropic_throttle_proxy.free_gate_watchdog import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
