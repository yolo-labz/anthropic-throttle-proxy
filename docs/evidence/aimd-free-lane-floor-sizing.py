#!/usr/bin/env python3
"""Floor-sizing evidence for docs/AIMD-POLICY-FREE-LANES.md §2.

Runs the REAL ``limiter._compute_drain`` (the function the pre-queue-depth
guard calls) against a lane saturated at its AIMD floor, with the free-lane
generation time (200 s, from the observed 171-303 s band) and pi's 180 s
wait budget, and prints which (floor, arrival-rank) pairs are admitted and
which are answered with a 503.

Two readings, because a held slot is priced differently by its age:

* age 0 s  — every held slot is modelled at a full generation. Nothing is
  admitted at any floor, at any rank: ``R > max_wait`` defeats the queue
  outright.
* age > 0 s — ``R = generation - age <= max_wait``. The lane then admits
  exactly ``floor`` arrivals per generation, so the FLOOR, not the queue
  depth, is the admission width. That is the 19/10/2026 go-gate incident
  restated as arithmetic.

Usage: uv run python docs/evidence/aimd-free-lane-floor-sizing.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from anthropic_throttle_proxy.limiter import _compute_drain

GENERATION_S = 200.0  # observed free-lane generation, p90-ish (171-303 s band)
MAX_WAIT_S = 180.0  # pi's queue_max_wait (client contract)
FLOORS = (1, 2, 4, 6, 8, 12)
RANKS = (0, 1, 2, 4, 8)


def main() -> int:
    print(f"generation={GENERATION_S:.0f}s max_wait={MAX_WAIT_S:.0f}s\n")
    for age in (0.0, 50.0, 100.0, 180.0):
        residual = max(0.0, GENERATION_S - age)
        print(f"--- slots aged {age:.0f}s (residual {residual:.0f}s) ---")
        print(f"{'floor':>5}  " + "  ".join(f"ahead={r}" for r in RANKS))
        for floor in FLOORS:
            cells = []
            for rank in RANKS:
                est = _compute_drain(
                    slots=floor,
                    busy=floor,
                    queued=rank,
                    ahead=rank,
                    service_time_s=GENERATION_S,
                    residuals=[residual] * floor,
                    samples=64,
                    source="measured",
                    evidenced=True,
                    max_wait=MAX_WAIT_S,
                )
                cells.append(f"{est.wait_s:>6.0f}s/{'OK ' if est.admits else '503'}")
            print(f"{floor:>5}  " + "  ".join(cells))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
