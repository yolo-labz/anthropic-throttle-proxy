"""Subscription recovery requires fresh, genuine same-model provider evidence."""

import importlib.util
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def checker():
    source = Path(__file__).resolve().parents[1] / "scripts/claude-subscription-check.py"
    spec = importlib.util.spec_from_file_location("claude_subscription_check", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def credentials(tmp_path):
    path = tmp_path / "subscription.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "test-subscription-secret",
                    "expiresAt": (time.time() + 600) * 1000,
                }
            }
        )
    )
    return path


def message(**changes):
    body = {
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }
    return {**body, **changes}


def provider(monkeypatch, checker, body, status=200):
    requests = []

    class Connection:
        def __init__(self, host, *, timeout):
            assert host == "api.anthropic.com" and timeout == 45

        def request(self, method, path, *, body, headers):
            requests.append(SimpleNamespace(method=method, path=path, data=body, headers=headers))

        def getresponse(self):
            response = io.BytesIO(json.dumps(body).encode())
            response.status = status
            response.headers = {
                "anthropic-ratelimit-unified-7d-utilization": "0",
                "set-cookie": "private",
            }
            return response

        def close(self):
            pass

    monkeypatch.setattr(checker.http.client, "HTTPSConnection", Connection)
    return requests


def test_genuine_subscription_message_preserves_model_and_secret(checker, credentials, monkeypatch):
    requests = provider(monkeypatch, checker, message())
    result = checker.check("account", credentials, "claude-opus-5-5")
    assert result["message_ok"] is True
    assert len(requests) == 1
    request = requests[0]
    assert json.loads(request.data)["model"] == "claude-opus-5-5"
    payload = json.loads(request.data)
    assert payload["max_tokens"] == 8
    assert "thinking" not in payload and "output_config" not in payload
    assert request.headers["Authorization"] == "Bearer test-subscription-secret"
    assert not any(key.lower() == "x-api-key" for key in request.headers)
    assert "test-subscription-secret" not in json.dumps(result)
    assert "set-cookie" not in result["headers"]


def test_fable_probe_uses_supported_bounded_thinking(checker, credentials, monkeypatch):
    requests = provider(monkeypatch, checker, message(model="claude-fable-5-2"))
    result = checker.check("account", credentials, "claude-fable-5-2")
    assert result["message_ok"] is True
    payload = json.loads(requests[0].data)
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "low"}
    assert payload["max_tokens"] == 64


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "message",
        message(model="another-model"),
        message(role="user"),
        message(content=[]),
        message(content=[{"type": "text", "text": ""}]),
        message(content=[{"type": "thinking", "thinking": "hidden"}]),
        message(stop_reason="unknown"),
        {"type": "message"},
    ],
)
def test_false_positive_provider_success_never_releases(checker, credentials, monkeypatch, body):
    provider(monkeypatch, checker, body)
    assert checker.check("account", credentials, "claude-opus-5-5")["message_ok"] is False


@pytest.mark.parametrize("expiry", [None, True, "tomorrow", float("nan"), float("inf"), 0])
def test_invalid_expiry_fails_before_network(checker, credentials, monkeypatch, expiry):
    credentials.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "test-subscription-secret",
                    "expiresAt": expiry,
                }
            }
        )
    )
    requests = provider(monkeypatch, checker, message())
    with pytest.raises(ValueError):
        checker.check("account", credentials, "claude-opus-5-5")
    assert requests == []


def test_api_key_only_is_never_used(checker, credentials, monkeypatch):
    credentials.write_text(json.dumps({"apiKey": "never-use-api-credit"}))
    requests = provider(monkeypatch, checker, message())
    with pytest.raises(ValueError, match="subscription"):
        checker.check("account", credentials, "claude-opus-5-5")
    assert requests == []


def test_provider_error_redacts_access_token(checker, credentials, monkeypatch):
    provider(
        monkeypatch,
        checker,
        {
            "error": {
                "type": "authentication_error",
                "message": "bad test-subscription-secret",
            }
        },
        status=401,
    )
    result = checker.check("account", credentials, "claude-opus-5-5")
    assert result["message_ok"] is False
    assert "test-subscription-secret" not in json.dumps(result)
    assert result["error_type"] == "authentication_error"


def test_redirect_is_refused_without_second_request(checker, credentials, monkeypatch):
    requests = provider(monkeypatch, checker, message(), status=302)
    result = checker.check("account", credentials, "claude-opus-5-5")
    assert result["message_ok"] is False
    assert len(requests) == 1
    assert requests[0].path == "/v1/messages?beta=true"


@pytest.mark.parametrize("counter", ["inflight", "queued", "admitted_holds"])
def test_idle_gate_refuses_busy_work_and_resumes(checker, monkeypatch, counter):
    calls = []

    def local(route, **kwargs):
        calls.append(route)
        return (
            {"inflight": 0, "queued": 0, "admitted_holds": 0, counter: 1}
            if route == "health"
            else {}
        )

    monkeypatch.setattr(checker, "local_request", local)
    with pytest.raises(ValueError, match="admitted work"), checker.idle_gateway():
        pytest.fail("must never run provider probes while admitted work exists")
    assert calls == ["quiesce", "health", "resume"]


def test_main_quiesces_before_provider_checks_and_always_resumes(checker, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "sys.argv",
        ["check", "--model", "claude-opus-5-5", "--account", "a:/fake", "--release-verified-holds"],
    )

    def local(route, **kwargs):
        calls.append(route)
        return {"inflight": 0, "queued": 0, "admitted_holds": 0}

    def run_checks(accounts, model, *, release):
        assert accounts == [("a", Path("/fake"))]
        assert model == "claude-opus-5-5" and release
        calls.append("probe")
        raise ValueError("provider failure")

    monkeypatch.setattr(checker, "local_request", local)
    monkeypatch.setattr(checker, "run_checks", run_checks)
    with pytest.raises(ValueError, match="provider failure"):
        checker.main()
    assert calls == ["quiesce", "health", "probe", "resume"]


def test_one_failed_account_prevents_recovery_but_checks_both(checker, monkeypatch, capsys):
    checked = []

    def check(label, path, model):
        checked.append(label)
        return {"label": label, "message_ok": label == "second"}

    monkeypatch.setattr(checker, "check", check)
    monkeypatch.setattr(
        checker, "release_verified_holds", lambda _: pytest.fail("must not clear holds")
    )
    assert (
        checker.run_checks(
            [("first", Path("/a")), ("second", Path("/b"))], "claude-opus-5-5", release=True
        )
        == 1
    )
    assert checked == ["first", "second"]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["recovery"] == "refused"


@pytest.mark.parametrize(
    "results",
    [[], [{"message_ok": False}], [{"message_ok": True, "bearer_id": "a", "observed_at": 1}]],
)
def test_release_rejects_failed_or_stale_evidence_without_service_actions(
    checker, monkeypatch, results
):
    monkeypatch.setattr(
        checker.subprocess, "check_output", lambda *a, **k: pytest.fail("must not touch service")
    )
    with pytest.raises(ValueError):
        checker.release_verified_holds(results)


@pytest.fixture
def configure_recovery(checker, monkeypatch):
    def configure(state):
        monkeypatch.setattr(
            checker.subprocess,
            "check_output",
            lambda *a, **k: f"THROTTLE_RETRY_AFTER_STATE_FILE={state}",
        )
        monkeypatch.setattr(
            checker,
            "local_request",
            lambda *a, **k: {
                "inflight": 0,
                "queued": 0,
                "admitted_holds": 0,
                "bearers": {"verified": {}},
            },
        )

    return configure


@pytest.mark.parametrize("socket_active", [True, False])
@pytest.mark.parametrize("model", ["claude-fable-5-2", "claude-opus-5-5"])
def test_release_preserves_other_holds_and_socket_state(
    checker, monkeypatch, tmp_path, socket_active, model, configure_recovery
):
    state = tmp_path / "holds.json"
    original = {
        key: time.time() + 7200
        for key in ("verified", "other", "verified:model:fable", "verified:model:other")
    }
    state.write_text(json.dumps(original))
    calls = []
    configure_recovery(state)

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0 if socket_active else 3)

    monkeypatch.setattr(checker.subprocess, "run", run)
    result = checker.release_verified_holds(
        [{"message_ok": True, "bearer_id": "verified", "observed_at": time.time(), "model": model}]
    )
    released = {"verified"}
    if model == "claude-fable-5-2":
        released.add("verified:model:fable")
    assert json.loads(state.read_text()) == {k: v for k, v in original.items() if k not in released}
    assert result["released_keys"] == sorted(released)
    assert json.loads(Path(result["backup"]).read_text()) == original
    assert Path(result["backup"]).stat().st_mode & 0o777 == 0o600
    assert result["released_bearers"] == ["verified"]
    expected = [
        [
            "/run/current-system/sw/bin/systemctl",
            "--user",
            "stop",
            "throttler-gateway.socket",
            "throttler-gateway.service",
        ],
        ["/run/current-system/sw/bin/systemctl", "--user", "start", "throttler-gateway.service"],
    ]
    if socket_active:
        expected.append(
            ["/run/current-system/sw/bin/systemctl", "--user", "start", "throttler-gateway.socket"]
        )
    assert calls[1:] == expected


@pytest.mark.parametrize(
    "health",
    [None, [], {}, {"inflight": 0, "queued": 0}]
    + [{"inflight": bad, "queued": 0, "admitted_holds": 0} for bad in (None, False, "0", 0.0, -1)],
)
def test_unknown_or_malformed_health_never_admits_recovery(checker, monkeypatch, health):
    calls = []

    def local(route, **kwargs):
        calls.append(route)
        return health if route == "health" else {}

    monkeypatch.setattr(checker, "local_request", local)
    with pytest.raises(ValueError), checker.idle_gateway():
        pytest.fail("unknown counters cannot establish idle gateway")
    assert calls == ["quiesce", "health", "resume"]


def test_partial_stop_failure_still_attempts_service_restoration(
    checker, monkeypatch, tmp_path, configure_recovery
):
    state = tmp_path / "holds.json"
    original = {"verified": time.time() + 3600}
    state.write_text(json.dumps(original))
    configure_recovery(state)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "stop" in command:
            raise checker.subprocess.CalledProcessError(1, command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(checker.subprocess, "run", run)
    with pytest.raises(checker.subprocess.CalledProcessError):
        checker.release_verified_holds(
            [{"message_ok": True, "bearer_id": "verified", "observed_at": time.time()}]
        )
    assert calls[-1] == [
        "/run/current-system/sw/bin/systemctl",
        "--user",
        "start",
        "throttler-gateway.socket",
    ]
    assert calls[-2][-1] == "throttler-gateway.service"
    assert json.loads(state.read_text()) == original


def test_partial_quiesce_failure_still_resumes_admission(checker, monkeypatch):
    calls = []

    def local(route, **kwargs):
        calls.append(route)
        if route == "quiesce":
            raise ValueError("response lost after admission closed")
        return {}

    monkeypatch.setattr(checker, "local_request", local)
    with pytest.raises(ValueError, match="response lost"), checker.idle_gateway():
        pytest.fail("must not probe after an uncertain quiesce response")
    assert calls == ["quiesce", "resume"]
