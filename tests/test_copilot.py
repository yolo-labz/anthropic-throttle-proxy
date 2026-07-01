"""Tests for the GitHub Copilot billing panel."""

from __future__ import annotations

import pytest

from anthropic_throttle_proxy import config, copilot


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    copilot._cache.clear()
    copilot._locks.clear()
    monkeypatch.setattr(config, "COPILOT_ORGS", "")
    monkeypatch.setattr(config, "COPILOT_TOKEN", "tok")
    monkeypatch.setenv("THROTTLE_COPILOT_TOKEN", "")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    yield
    copilot._cache.clear()
    copilot._locks.clear()


def test_parse_orgs_dedupes_and_strips():
    assert copilot.parse_orgs("yolo-labz, DeliCasa ,yolo-labz,") == ["yolo-labz", "DeliCasa"]
    assert copilot.parse_orgs("") == []


def test_parse_billing_flattens_seats_and_plan():
    body = {
        "seat_breakdown": {
            "total": 3,
            "active_this_cycle": 2,
            "inactive_this_cycle": 1,
            "pending_invitation": 0,
        },
        "plan_type": "business",
        "seat_management_setting": "assign",
        "ide_chat": "enabled",
        "cli": "enabled",
        "platform_chat": "enabled",
    }
    view = copilot._parse_billing(body)
    assert view["ok"] is True
    assert view["plan_type"] == "business"
    assert view["seats_total"] == 3 and view["seats_active"] == 2
    assert view["seats_inactive"] == 1


def test_parse_billing_tolerates_missing_buckets():
    view = copilot._parse_billing({})
    assert view["ok"] is True
    assert view["seats_total"] == 0
    assert view["plan_type"] == "—"


def test_parse_billing_non_dict_is_not_ok():
    assert copilot._parse_billing([])["ok"] is False


async def test_refresh_empty_when_no_orgs():
    assert await copilot.refresh(0.0) == []


async def test_refresh_empty_when_no_token(monkeypatch):
    monkeypatch.setattr(config, "COPILOT_ORGS", "yolo-labz")
    monkeypatch.setattr(config, "COPILOT_TOKEN", "")
    assert await copilot.refresh(0.0) == []


async def test_refresh_surfaces_404_as_no_copilot(monkeypatch):
    monkeypatch.setattr(config, "COPILOT_ORGS", "no-cop")

    async def _404(org, tok):
        return (404, None)

    monkeypatch.setattr(copilot, "_fetch_billing", _404)
    rows = await copilot.refresh(0.0)
    assert rows[0]["ok"] is False
    assert "no Copilot" in rows[0]["err"]


async def test_refresh_surfaces_403_as_scope(monkeypatch):
    monkeypatch.setattr(config, "COPILOT_ORGS", "locked")

    async def _403(org, tok):
        return (403, None)

    monkeypatch.setattr(copilot, "_fetch_billing", _403)
    rows = await copilot.refresh(0.0)
    assert rows[0]["ok"] is False
    assert "read:org" in rows[0]["err"]


async def test_refresh_env_order_and_ttl_cache(monkeypatch):
    monkeypatch.setattr(config, "COPILOT_ORGS", "yolo-labz,DeliCasa")
    calls: list[str] = []

    async def fake(org, tok):
        calls.append(org)
        return 200, {
            "plan_type": "business",
            "seat_breakdown": {"total": 1, "active_this_cycle": 1},
        }

    monkeypatch.setattr(copilot, "_fetch_billing", fake)
    rows = await copilot.refresh(0.0)
    assert [r["org"] for r in rows] == ["yolo-labz", "DeliCasa"]
    assert rows[0]["plan_type"] == "business"
    assert calls == ["yolo-labz", "DeliCasa"]
    # within TTL → no new fetches
    await copilot.refresh(0.0)
    assert calls == ["yolo-labz", "DeliCasa"]
    # past TTL → re-fetch
    await copilot.refresh(copilot.TTL_S + 1)
    assert calls == ["yolo-labz", "DeliCasa", "yolo-labz", "DeliCasa"]
