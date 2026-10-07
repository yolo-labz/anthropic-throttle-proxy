"""Synthetic acceptance for the private bridge's public allowlisted producer."""

import json
import stat
import sys
from datetime import UTC, datetime

import pytest

from anthropic_throttle_proxy import desktop_report

NOW = datetime(2026, 10, 7, 21, tzinfo=UTC)


def test_remaining_is_not_used_or_monthly_credits():
    raw = {
        "percent": 93.6,
        "resetAt": 1791844029,
        "resetDate": "2026-10-12",
        "secret": "never-output",
    }
    result = desktop_report.report(raw, NOW)
    row = result["lanes"][0]
    assert row["id"] == "mimo:desktop-subscription"
    assert row["status"] == "ok"
    meter = row["meters"][0]
    assert meter["usedPercent"] == pytest.approx(6.4)
    assert meter["remainingPercent"] == 93.6
    assert meter["unit"] == "percent" and meter["limitId"] == "weekly"
    assert "allowance" not in meter and "windowMins" not in meter
    assert "never-output" not in json.dumps(result)


@pytest.mark.parametrize(
    "value", [None, True, "93.6", -1, 101, float("nan"), float("inf"), 10**400]
)
def test_bad_quota_unknown(value):
    row = desktop_report.report({"percent": value}, NOW)["lanes"][0]
    assert row["status"] == "unknown"
    assert not row["meters"]


def test_zero_is_exhausted():
    assert desktop_report.report({"percent": 0}, NOW)["lanes"][0]["status"] == "exhausted"


def test_failure_replaces_success_atomically_without_secret(tmp_path):
    path = tmp_path / "quota.json"
    desktop_report.publish(path, desktop_report.report({"percent": 100}, NOW))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    desktop_report.publish(
        path, desktop_report.report({"error": "secret-url", "percent": 100}, NOW)
    )
    result = json.loads(path.read_text())
    assert result["lanes"][0]["status"] == "unknown"
    assert "secret-url" not in path.read_text()
    assert not list(tmp_path.glob(".desktop-quota-*"))


@pytest.mark.parametrize("wrong_origin", [False, True])
async def test_external_backend_origin_and_existing_usage_function(
    tmp_path, monkeypatch, wrong_origin
):
    from types import SimpleNamespace

    for name in ("config.json", ".secret_key"):
        path = tmp_path / name
        path.write_text("synthetic-seed")
        path.chmod(0o600)
    calls = []

    async def usage(credentials):
        calls.append(credentials)
        return {"percent": 80}

    account = SimpleNamespace(mimo_pass_token=None, mimo_user_id=None, mimo_c_user_id=None)
    modules = {
        "app.config": SimpleNamespace(
            __file__=str(tmp_path / "app" / "config.py"),
            config_manager=SimpleNamespace(config=SimpleNamespace(mimo_accounts=[account])),
        ),
        "app.desktop_session": SimpleNamespace(
            __file__=str(
                tmp_path / ("elsewhere" if wrong_origin else "app") / "desktop_session.py"
            ),
            get_account_usage=usage,
        ),
    }
    monkeypatch.setattr(desktop_report.importlib, "import_module", lambda name: modules[name])
    monkeypatch.setattr("sys.path", list(sys.path))
    result = await desktop_report.collect(tmp_path)
    if wrong_origin:
        assert result == {"error": "backend-origin"} and calls == []
    else:
        assert result == {"percent": 80} and len(calls) == 1


def test_cli_suppresses_backend_logs_and_exception(tmp_path, monkeypatch, capsys):
    async def bad_collect(root):
        from pathlib import Path

        assert Path.cwd() == root == tmp_path
        print("private-backend-log")
        raise ValueError("private-credential-url")

    monkeypatch.setattr(desktop_report, "collect", bad_collect)
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv", ["producer", "--backend", str(tmp_path), "--output", str(output)]
    )
    assert desktop_report.main() == 0
    captured = capsys.readouterr()
    assert not captured.out and not captured.err
    assert "private-" not in output.read_text()
    assert json.loads(output.read_text())["lanes"][0]["status"] == "unknown"
