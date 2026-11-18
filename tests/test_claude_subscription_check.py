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
    assert request.headers["Authorization"] == "Bearer test-subscription-secret"
    assert not any(key.lower() == "x-api-key" for key in request.headers)
    assert "test-subscription-secret" not in json.dumps(result)
    assert "set-cookie" not in result["headers"]


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


def test_retired_release_cli_refuses_before_credentials_or_network(checker, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "check",
            "--model",
            "claude-opus-5-5",
            "--account",
            "a:/missing",
            "--release-verified-holds",
        ],
    )
    monkeypatch.setattr(checker, "check", lambda *a: pytest.fail("must not send a probe"))
    monkeypatch.setattr(
        checker, "local_request", lambda *a, **k: pytest.fail("must not mutate admission")
    )
    with pytest.raises(SystemExit) as result:
        checker.main()
    assert result.value.code == 2


@pytest.mark.parametrize("entry", ["idle_gateway", "release_verified_holds", "run_checks"])
def test_all_retired_release_entrypoints_have_no_effects(checker, monkeypatch, entry):
    monkeypatch.setattr(checker, "check", lambda *a: pytest.fail("must not send a probe"))
    monkeypatch.setattr(
        checker, "local_request", lambda *a, **k: pytest.fail("must not mutate admission")
    )
    args = {
        "idle_gateway": ((), {}),
        "release_verified_holds": (([{"message_ok": True}],), {}),
        "run_checks": (([("a", Path("/missing"))], "claude-opus-5-5"), {"release": True}),
    }
    positional, named = args[entry]
    with pytest.raises(ValueError, match="disabled"):
        getattr(checker, entry)(*positional, **named)


@pytest.mark.parametrize("route", ["quiesce", "resume", "health"])
def test_mutating_local_operations_are_disabled(checker, monkeypatch, route):
    monkeypatch.setattr(
        checker.http.client,
        "HTTPConnection",
        lambda *a, **k: pytest.fail("must not contact gateway"),
    )
    with pytest.raises(ValueError, match="disabled"):
        checker.local_request(route, post=True)


def test_explicit_readonly_diagnostic_still_checks_each_account(checker, monkeypatch, capsys):
    checked = []

    def check(label, path, model):
        checked.append(label)
        return {"label": label, "message_ok": label == "second"}

    monkeypatch.setattr(checker, "check", check)
    assert (
        checker.run_checks(
            [("first", Path("/a")), ("second", Path("/b"))], "claude-opus-5-5", release=False
        )
        == 1
    )
    assert checked == ["first", "second"]
    assert len(capsys.readouterr().out.splitlines()) == 2
