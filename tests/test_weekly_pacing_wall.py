"""A weekly-pacing deferral must read as a WALL to the deployed recovery helper.

The fleet's only wall-recovery path is `herdr-429-resume`: it resumes a pane only
when the last output line satisfies `is_wall()`. That predicate is copied VERBATIM
below from the deployed script (`/nix/store/dksfc1im…-herdr-429-resume.py`, read
02/11/2026) — not imported, so a drift in either half fails here instead of
silently costing another five days of dead panes (31/10: 22 panes).

`is_wall()` is the contract this test pins:
* a 429 is a usage window → resume after it;
* a 503 is a wall ONLY with the prospective-refusal marker, because a bare 503 is
  an outage and a pane must not be re-prompted into it.
"""

from __future__ import annotations

import types

from anthropic_throttle_proxy import config, ingress, proxy

PACE_BODY = "weekly pacing: every account is ahead of schedule; retry after catch-up\n"


def is_wall(error_line):
    """True for the two refusals that mean "the lane is walled, not broken".

    429 is the usage window. 503 counts only with the throttler's prospective
    refusal marker (`prospective_admission_refused`, or its message text "local
    policy: prospective admission refused"): that is a SPENT BUDGET and resumes
    when the budget does — Pedro 24/10/2026, w3R:p8 on the free DeepSeek lane
    with retry_after 15 h. A bare 503 is an outage, not a wall, and is left
    alone on purpose.
    """
    if "429" in error_line:
        return True
    lowered = error_line.lower()
    return "503" in error_line and (
        "prospective_admission_refused" in lowered or "prospective admission refused" in lowered
    )


def _client_line(resp) -> str:
    """What a pane prints when the SDK surfaces the refusal (status + body)."""
    return f"Error: {resp.status} {resp.text.strip()}"


def test_deferral_is_recognised_as_a_wall() -> None:
    assert is_wall(_client_line(proxy._weekly_pacing_response(1800))) is True


def test_the_old_503_shape_would_not_be_recognised() -> None:
    """The one-line revert: the same body as a 503 is an outage, not a wall."""
    resp = proxy._weekly_pacing_response(1800)
    resp.set_status(503)
    assert is_wall(_client_line(resp)) is False


def test_deferral_keeps_the_retry_after_the_stamp_and_the_body() -> None:
    resp = proxy._weekly_pacing_response(1800)
    assert resp.status == 429
    assert resp.headers["retry-after"] == "1800"
    assert resp.headers[config.QUEUE_TIMEOUT_HEADER] == "1"
    assert resp.text == PACE_BODY


def test_share_deferral_has_the_same_shape() -> None:
    resp = proxy._weekly_pacing_response(
        60, "client over its weekly share while the fleet is ahead"
    )
    assert is_wall(_client_line(resp)) is True


def test_stamp_still_exempts_the_deferral_from_pushback_and_aimd() -> None:
    """The brake: the stamp, not the status, keeps this out of the retry/AIMD path."""
    assert proxy._is_queue_timeout_response(proxy._weekly_pacing_response(1800)) is True


def test_ingress_reads_the_same_stamp_as_admission_pressure() -> None:
    """A pacing 429 must not be spilt to a sibling lane as if it came from upstream."""
    resp = proxy._weekly_pacing_response(1800)
    stamped = types.SimpleNamespace(status=resp.status, headers=resp.headers)
    assert ingress._queue_timeout_503(stamped) is True
    bare = types.SimpleNamespace(status=429, headers={})
    assert ingress._queue_timeout_503(bare) is False
