import json
import os
from types import SimpleNamespace

import enable_loaded_fence as candidate
import pytest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    identity = dict(
        pid=123,
        session="/s.jsonl",
        transcript="/s.jsonl",
        cwd="/work",
        provider="claude-lane",
        model="claude-opus-5-5",
        effort="max",
        install="/qualified",
        role="eng",
        name="seat",
        pane="w1:p1",
        herdrSession="home",
    )

    def write(name, body):
        path = tmp_path / name
        path.write_text(json.dumps(body))
        path.chmod(0o600)
        return path

    before = write("before.json", dict(identity=identity, prefixSha256="original"))
    refusal = write(
        "refusal.json",
        dict(
            command="core-upgrade-state",
            reason="native-context-mismatch",
            actual={**identity, "nativeContext": {"agrees": False}},
        ),
    )
    loaded = write(
        "123.compaction-fence-loaded.json",
        dict(pid=123, version=candidate.VERSION, generation="b" * 64),
    )
    os.utime(before, ns=(1, 1))
    os.utime(loaded, ns=(2, 2))
    monkeypatch.setattr(candidate.recovery.core, "ROOT", tmp_path)
    fingerprint = next(iter(candidate.FINGERPRINTS))
    monkeypatch.setattr(candidate, "source_fingerprint", lambda _: fingerprint)
    calls = []
    operation = SimpleNamespace(
        identity=identity,
        native_identity=lambda: calls.append("identity"),
        screen=lambda: "empty",
        request=lambda _: {"nonce": "a" * 64},
        deliver=lambda command: calls.append(command),
        receipt=lambda _: dict(
            nonce="a" * 64,
            pid=123,
            version=candidate.VERSION,
            fingerprint=fingerprint,
            enabled=True,
            inference=False,
        ),
    )
    monkeypatch.setattr(candidate.recovery.idle, "empty_editor", lambda screen: screen == "empty")
    return SimpleNamespace(
        before=before,
        refusal=refusal,
        loaded=loaded,
        operation=operation,
        directory=tmp_path,
        calls=calls,
    )


def run(s):
    return candidate.enable(s.operation, s.before, s.refusal, s.directory)


def test_scoped_enable_does_not_claim_context_agreement(setup):
    result = run(setup)
    assert result["fenceEnabled"] and result["historySettlementHold"]
    assert result["contextAgreementClaimed"] is False
    assert setup.calls == ["identity", "/compaction-loop-fence-enable " + "a" * 64]


@pytest.mark.parametrize("change", ["missing", "stale", "draft", "nonidle", "pin", "intent"])
def test_near_misses_never_deliver(setup, change):
    if change == "missing":
        setup.loaded.unlink()
    elif change == "stale":
        os.utime(setup.loaded, ns=(1, 1))
    elif change == "draft":
        setup.operation.screen = lambda: "human draft"
    elif change == "nonidle":
        setup.operation.native_identity = lambda: (_ for _ in ()).throw(RuntimeError("working"))
    elif change == "pin":
        setup.operation.identity = {**setup.operation.identity, "install": "/other"}
    else:
        (setup.directory / "home-w1-p1-123.intent.json").write_text("existing")
    with pytest.raises((RuntimeError, FileExistsError)):
        run(setup)
    assert not any(call.startswith("/") for call in setup.calls)


def test_missing_receipt_is_uncertain_and_never_resends(setup):
    setup.operation.receipt = lambda _: (_ for _ in ()).throw(TimeoutError("uncertain"))
    with pytest.raises(TimeoutError):
        run(setup)
    with pytest.raises(FileExistsError):
        run(setup)
    assert sum(call.startswith("/") for call in setup.calls) == 1


def test_wrong_refusal_or_missing_role_refuses(setup):
    body = json.loads(setup.refusal.read_text())
    body["actual"]["role"] = "different"
    setup.refusal.write_text(json.dumps(body))
    with pytest.raises(RuntimeError, match="exact failed"):
        run(setup)
    assert setup.calls == []
