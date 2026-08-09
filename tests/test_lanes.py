"""Subscription-lane report reader.

The report is written out of process by a user timer (NixOS
``modules.home.throttleLanes``). Everything here is about refusing to render a
comfortable lie: a failed probe must not read as idle capacity, and a report
whose writer died must not keep showing yesterday's percentages as live.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from anthropic_throttle_proxy import lanes

NOW = datetime(2026, 8, 3, 21, 0, 0, tzinfo=UTC).timestamp()


def _write(tmp_path, monkeypatch, payload, *, age_s: float = 60.0):
    generated = datetime.fromtimestamp(NOW - age_s, tz=UTC)
    payload = {
        "schema": 1,
        "generatedAt": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "intervalSeconds": 900,
        **payload,
    }
    path = tmp_path / "throttle-lanes.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(path))
    return path


@pytest.fixture(autouse=True)
def _clear_cache():
    lanes._cache = None
    yield
    lanes._cache = None


def test_codex_meters_and_binding_pct(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {
            "lanes": [
                {
                    "id": "codex:a",
                    "kind": "codex",
                    "status": "ok",
                    "meters": [
                        {
                            "limitId": "codex",
                            "usedPercent": 100,
                            "resetsAt": NOW + 3600,
                            "planType": "pro",
                        },
                        {"limitId": "codex_bengalfox", "usedPercent": 4, "resetsAt": NOW + 7200},
                    ],
                }
            ]
        },
    )
    lane = lanes.view(NOW)["lanes"][0]
    assert lane["family"] == "openai"
    assert lane["plan"] == "pro"
    assert lane["binding_pct"] == 100.0  # fullest meter decides admission
    # Fullest meter first — it is the one that decides admission.
    assert [m["label"] for m in lane["meters"]] == ["codex", "codex_bengalfox"]
    assert lane["meters"][0]["reset_in"] == "1h 00m"


def _copilot_lane():
    """The measured shape: premium burnt to 0% while chat stays unlimited."""
    return {
        "lanes": [
            {
                "id": "copilot:personal",
                "kind": "copilot",
                "status": "ok",
                "quotas": {
                    "chat": {"unlimited": True, "percentRemaining": 100.0},
                    "premium_interactions": {"percentRemaining": 0.0, "entitlement": 200},
                },
            }
        ]
    }


def test_copilot_remaining_is_inverted_and_unlimited_is_not_zero(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        _copilot_lane(),
    )
    meters = {m["label"]: m for m in lanes.view(NOW)["lanes"][0]["meters"]}
    # An unlimited quota has no fill level; rendering it as 0% used would claim
    # capacity information the provider never gave.
    assert meters["chat"]["used_pct"] is None
    assert meters["chat"]["unlimited"] is True
    assert meters["premium_interactions"]["used_pct"] == 100.0  # 0% remaining = exhausted


def test_stale_report_degrades_every_ok_lane(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {"lanes": [{"id": "codex:a", "kind": "codex", "status": "ok", "meters": []}]},
        age_s=1801,  # > 2 x intervalSeconds
    )
    view = lanes.view(NOW)
    assert view["stale"] is True
    assert view["lanes"][0]["status"] == "stale"


def test_fresh_report_is_not_stale(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {"lanes": [{"id": "codex:a", "kind": "codex", "status": "ok", "meters": []}]},
        age_s=899,
    )
    assert lanes.view(NOW)["stale"] is False


def test_probe_failure_keeps_status_and_reason(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {
            "lanes": [
                {"id": "codex:b", "kind": "codex", "status": "unknown", "reason": "no auth.json"}
            ]
        },
    )
    lane = lanes.view(NOW)["lanes"][0]
    assert lane["status"] == "unknown"
    assert lane["reason"] == "no auth.json"
    assert lane["binding_pct"] is None  # never coerce unknown to 0


def test_missing_or_corrupt_report_hides_the_panel(tmp_path, monkeypatch):
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(tmp_path / "absent.json"))
    assert lanes.view(NOW) == lanes.EMPTY

    lanes._cache = None
    bad = tmp_path / "bad.json"
    bad.write_text("{ half-written")
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(bad))
    assert lanes.view(NOW) == lanes.EMPTY


def test_view_is_ttl_cached(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        monkeypatch,
        {"lanes": [{"id": "codex:a", "kind": "codex", "status": "ok", "meters": []}]},
    )
    assert len(lanes.view(NOW)["lanes"]) == 1
    path.unlink()
    assert len(lanes.view(NOW + lanes.TTL_S / 2)["lanes"]) == 1  # served from cache
    assert lanes.view(NOW + lanes.TTL_S * 2) == lanes.EMPTY  # re-read past the TTL


def test_unparseable_timestamp_is_not_stale(tmp_path, monkeypatch):
    path = tmp_path / "throttle-lanes.json"
    path.write_text(
        json.dumps(
            {
                "generatedAt": "not-a-date",
                "intervalSeconds": 900,
                "lanes": [{"id": "codex:a", "kind": "codex", "status": "ok"}],
            }
        )
    )
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(path))
    view = lanes.view(NOW)
    assert view["age_s"] is None
    assert view["stale"] is False  # unknown age ≠ stale, and ≠ fresh: it is unknown


def test_report_path_falls_back_to_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("THROTTLE_LANES_FILE", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert lanes.report_path() == f"{tmp_path}/throttle-lanes.json"
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert lanes.report_path() == ""


def test_report_age_seconds_tracks_the_writer(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {"lanes": [{"id": "codex:a", "kind": "codex", "status": "ok"}]},
        age_s=timedelta(minutes=5).total_seconds(),
    )
    assert lanes.view(NOW)["age_s"] == pytest.approx(300.0, abs=1.0)


def _codex_lane(used_pct, **extra):
    return {
        "lanes": [
            {
                "id": "codex:b",
                "kind": "codex",
                "status": "ok",
                "meters": [
                    {"limitId": "codex", "usedPercent": used_pct, "resetsAt": NOW + 3600},
                    {"limitId": "codex_bengalfox", "usedPercent": 10, "resetsAt": NOW + 7200},
                ],
                **extra,
            }
        ]
    }


def test_full_meter_is_never_reported_as_ok(tmp_path, monkeypatch):
    """A lane whose binding meter reads 100% REFUSES — it must not render ok.

    Measured 07/08/2026: both ChatGPT `codex` meters sat at 100% (A resets
    08/08 12:48, B 10/08 17:00) and `codex exec` answered "You've hit your
    usage limit", while the Subscriptions table rendered both rows `ok` — the
    status came verbatim from the probe report and never looked at the meters.
    """
    _write(tmp_path, monkeypatch, _codex_lane(100))
    assert lanes.view(NOW)["lanes"][0]["status"] == "exhausted"


def test_partly_used_meter_still_reads_ok(tmp_path, monkeypatch):
    """The rule fires on a FULL meter only — no invented warn threshold."""
    _write(tmp_path, monkeypatch, _codex_lane(42))
    assert lanes.view(NOW)["lanes"][0]["status"] == "ok"


def test_stale_full_meter_reads_stale_not_exhausted(tmp_path, monkeypatch):
    """Staleness wins: an untrusted 100% may already have reset."""
    _write(tmp_path, monkeypatch, _codex_lane(100), age_s=1801)
    assert lanes.view(NOW)["lanes"][0]["status"] == "stale"


def test_unlimited_quota_keeps_a_lane_out_of_exhausted(tmp_path, monkeypatch):
    """Copilot burns premium to 0 while chat/completions keep serving.

    Calling that lane exhausted would be the mirror-image lie of calling a full
    one healthy: the operator would stop routing base-model work that still
    works. The 100% premium row stays visible in the meters column.
    """
    _write(
        tmp_path,
        monkeypatch,
        _copilot_lane(),
    )
    lane = lanes.view(NOW)["lanes"][0]
    assert lane["status"] == "ok"
    assert lane["binding_pct"] == 100.0


def test_a_failed_probe_status_is_never_upgraded_to_exhausted(tmp_path, monkeypatch):
    """Only `ok` is downgradable — a worse verdict already says more than `exhausted`.

    Adversarial-review NIT: nothing asserted that the `== "ok"` guard holds, so
    a future refactor could let a full meter overwrite the reason a probe gave.
    """
    payload = _codex_lane(100)
    payload["lanes"][0]["status"] = "probe failed"
    payload["lanes"][0]["reason"] = "connection refused"
    _write(tmp_path, monkeypatch, payload)
    lane = lanes.view(NOW)["lanes"][0]
    assert lane["status"] == "probe failed"
    assert lane["reason"] == "connection refused"


def test_an_exhausted_lane_says_which_meter_and_when_it_reopens(tmp_path, monkeypatch):
    """EXHAUSTED with an empty tooltip is a verdict the operator can't act on.

    #189 derives the verdict from the meter, so nothing populated `reason` and
    the row rendered a bare EXHAUSTED. Name the measured facts instead.
    """
    _write(tmp_path, monkeypatch, _codex_lane(100))
    lane = lanes.view(NOW)["lanes"][0]
    assert lane["status"] == "exhausted"
    assert lane["reason"] == "codex meter at 100% — reopens in 1h 00m"


def test_the_probes_own_reason_outranks_the_derived_one(tmp_path, monkeypatch):
    """First-hand beats inferred: the upstream's words win when the probe has them."""
    payload = _codex_lane(100)
    payload["lanes"][0]["reason"] = "You've hit your usage limit."
    _write(tmp_path, monkeypatch, payload)
    lane = lanes.view(NOW)["lanes"][0]
    assert lane["status"] == "exhausted"
    assert lane["reason"] == "You've hit your usage limit."


def test_a_healthy_lane_gains_no_invented_reason(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, _codex_lane(42))
    assert lanes.view(NOW)["lanes"][0]["reason"] == ""


def test_two_full_meters_name_the_one_that_reopens_last(tmp_path, monkeypatch):
    """Adversarial-review MAJOR: the fullest-first sort is STABLE, so with two
    meters at 100% `meters[0]` is just whichever the probe wrote first. Naming
    the earlier reset would promise the lane back while the other is walled.
    """
    payload = _codex_lane(100)
    payload["lanes"][0]["meters"] = [
        {"limitId": "codex_bengalfox", "usedPercent": 100, "resetsAt": NOW + 1800},
        {"limitId": "codex", "usedPercent": 100, "resetsAt": NOW + 7200},
    ]
    _write(tmp_path, monkeypatch, payload)
    lane = lanes.view(NOW)["lanes"][0]
    assert lane["status"] == "exhausted"
    assert lane["reason"] == "codex meter at 100% — reopens in 2h 00m"


def test_over_quota_usage_is_reported_not_rounded_down(tmp_path, monkeypatch):
    """Adversarial-review MAJOR: the branch fires at >=100, so hardcoding
    "100%" understates a provider that publishes 130%."""
    _write(tmp_path, monkeypatch, _codex_lane(130))
    assert lanes.view(NOW)["lanes"][0]["reason"] == "codex meter at 130% — reopens in 1h 00m"


def test_a_whitespace_probe_reason_does_not_win(tmp_path, monkeypatch):
    """Adversarial-review MINOR: "   " is truthy, so it beat the derived reason
    and rendered a blank tooltip on an exhausted lane."""
    payload = _codex_lane(100)
    payload["lanes"][0]["reason"] = "   "
    _write(tmp_path, monkeypatch, payload)
    assert lanes.view(NOW)["lanes"][0]["reason"] == "codex meter at 100% — reopens in 1h 00m"


def test_an_elapsed_reset_drops_the_reopens_clause(tmp_path, monkeypatch):
    """`reset_in` is recomputed at VIEW time and is empty once elapsed, so the
    message degrades to the fact it still knows rather than a stale countdown.
    """
    payload = _codex_lane(100)
    payload["lanes"][0]["meters"] = [{"limitId": "codex", "usedPercent": 100, "resetsAt": NOW - 1}]
    _write(tmp_path, monkeypatch, payload)
    assert lanes.view(NOW)["lanes"][0]["reason"] == "codex meter at 100%"


def _balance_lane(total, *, currency="USD", status="ok", **extra):
    return {
        "lanes": [
            {
                "id": "deepseek",
                "kind": "deepseek",
                "status": status,
                "balance": {"currency": currency, "total": total},
                **extra,
            }
        ]
    }


def test_a_pay_go_lane_reports_its_remaining_balance(tmp_path, monkeypatch):
    """Money is a limit too, and this panel had no row for it.

    On 07/08/2026 the DeepSeek lane fell to $0.15 while two Anthropic accounts
    were 7d-rejected, and the table that exists to show every subscription's
    remaining budget could not render it: `_normalize` built meters for `codex`
    and `copilot` only, so a pay-go lane arrived with an empty meter list.
    """
    _write(tmp_path, monkeypatch, _balance_lane("15.20"))
    lane = lanes.view(NOW)["lanes"][0]
    assert lane["family"] == "chinese-frontier"  # same gate family as Kimi/GLM
    assert lane["status"] == "ok"
    assert [(m["label"], m["used_pct"], m["note"]) for m in lane["meters"]] == [
        ("balance", None, "$15.20")
    ]
    # No invented denominator: a balance has no ceiling to be a percentage of.
    assert lane["binding_pct"] is None


def test_a_drained_balance_is_exhausted_not_ok(tmp_path, monkeypatch):
    """Zero balance refuses (DeepSeek answers 402) — the % branch can't see it."""
    _write(tmp_path, monkeypatch, _balance_lane("0.00"))
    lane = lanes.view(NOW)["lanes"][0]
    assert lane["status"] == "exhausted"
    assert lane["reason"] == "balance $0.00 — the lane refuses at zero"


def test_a_non_usd_balance_keeps_its_currency(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, _balance_lane(7.5, currency="eur"))
    assert lanes.view(NOW)["lanes"][0]["meters"][0]["note"] == "EUR 7.50"


def test_an_unreadable_balance_renders_no_meter_rather_than_zero(tmp_path, monkeypatch):
    """ "couldn't read it" must never render as "you have nothing left"."""
    _write(tmp_path, monkeypatch, _balance_lane("not-a-number"))
    lane = lanes.view(NOW)["lanes"][0]
    assert lane["meters"] == []
    assert lane["status"] == "ok"


def test_a_windowed_lane_keeps_its_windows_when_it_also_has_a_balance(tmp_path, monkeypatch):
    """Kind decides the primary meters; balance only fills an empty list."""
    payload = _codex_lane(42)
    payload["lanes"][0]["balance"] = {"currency": "USD", "total": "9.00"}
    _write(tmp_path, monkeypatch, payload)
    labels = [m["label"] for m in lanes.view(NOW)["lanes"][0]["meters"]]
    assert labels == ["codex", "codex_bengalfox"]
