"""Two processes sharing one spend file never lose spend (p2S adversarial read of #62, D1)."""

import multiprocessing as mp
import time

from anthropic_throttle_proxy import metered_spend


def _writer(path: str, n: int) -> None:
    for _ in range(n):
        metered_spend.record(path, 1.0, 86400.0, time.time())


def test_two_writers_never_lose_spend(tmp_path) -> None:
    path = str(tmp_path / "api-key-spend.json")
    procs = [mp.get_context("fork").Process(target=_writer, args=(path, 300)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    spend = metered_spend.window_spend(path, 86400.0, time.time())
    assert spend == 600.0, f"lost spend: recorded {spend} of 600.0"
