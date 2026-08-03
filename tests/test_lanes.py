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


def test_copilot_remaining_is_inverted_and_unlimited_is_not_zero(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        {
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
        },
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
