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

    monkeypatch.setattr(desktop_report, "read_credentials", lambda root: {"mimoPassToken": None})
    modules = {
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


def test_seed_reader_does_not_import_mutating_config_manager(tmp_path, monkeypatch):
    from types import SimpleNamespace

    seed = {
        "mimo_accounts": [
            {
                field: "enc:v1:synthetic"
                for field in ("mimo_pass_token", "mimo_user_id", "mimo_c_user_id")
            }
        ]
    }
    for name, content in (("config.json", json.dumps(seed)), (".secret_key", "synthetic-key")):
        path = tmp_path / name
        path.write_text(content)
        path.chmod(0o600)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    imported = []

    def plugin(name):
        imported.append(name)
        assert name == "cryptography.fernet"
        return SimpleNamespace(
            Fernet=lambda key: SimpleNamespace(decrypt=lambda value: b"synthetic")
        )

    monkeypatch.setattr(desktop_report.importlib, "import_module", plugin)
    assert desktop_report.read_credentials(tmp_path) == {
        "mimoPassToken": "synthetic",
        "mimoUserId": "synthetic",
        "mimoCUserId": "synthetic",
    }
    assert imported == ["cryptography.fernet"]
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    (tmp_path / "config.json").write_text('{"mimo_accounts": [{"mimo_pass_token": "plaintext"}]}')
    assert desktop_report.read_credentials(tmp_path) is None
    assert imported == ["cryptography.fernet"]


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
