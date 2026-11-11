import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location(
    "resume_recovered", Path(__file__).with_name("resume_recovered.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def transcript(reason="toolUse", pending=False):
    entries = [
        dict(type="message", id="u", parentId=None, message={"role": "user"}),
        dict(
            type="message",
            id="a",
            parentId="u",
            message={"role": "assistant", "stopReason": reason},
        ),
        dict(type="message", id="t", parentId="a", message={"role": "toolResult"}),
        dict(type="custom", id="audit", parentId="t", customType="pi-context-branch-recovery"),
    ]
    if pending:
        entries.append(
            dict(type="message", id="pending", parentId="audit", message={"role": "user"})
        )
    return ("\n".join(map(json.dumps, entries)) + "\n").encode()


@pytest.mark.parametrize("reason", ["error", "aborted", "toolUse"])
def test_structural_unfinished(reason):
    assert module.unfinished(transcript(reason))["stopReason"] == reason


@pytest.mark.parametrize(
    "raw",
    [
        transcript("stop"),
        transcript(pending=True),
        b'{"type":"custom","id":"a","parentId":null}\n',
        b'{"type":"message","id":"a","parentId":"a","message":{"role":"assistant"}}\n',
        b'{"type":"message","id":"a","parentId":"missing","message":{"role":"assistant"}}\n',
    ],
)
def test_no_completed_pending_or_unknown_work(raw):
    with pytest.raises(RuntimeError):
        module.unfinished(raw)


def test_uncertain_prompt_sent_once_only(tmp_path, monkeypatch):
    directory = tmp_path / "receipts"
    directory.mkdir(mode=0o700)
    source = tmp_path / "session.jsonl"
    source.write_bytes(transcript())
    identity = dict(
        pid=123,
        herdrSession="home",
        pane="w1:p1",
        transcript=str(source),
        session=str(source),
        cwd="/project",
        provider="claude-lane",
        model="claude-opus-5-5",
        effort="max",
        install="/accepted",
        role="eng~",
    )
    request = {**identity, "nonce": "a" * 64}
    sent = []
    monkeypatch.setattr(module, "controller_proof", lambda *args: None)

    def run(arguments):
        sent.append(arguments)
        raise subprocess.TimeoutExpired(arguments, 1)

    recovery = SimpleNamespace(
        identity=identity,
        native_identity=lambda: None,
        screen=lambda: "─" * 30 + "\n\n" + "─" * 30 + "\n🧠",
        request=lambda directory: request,
        deliver=lambda command: None,
        receipt=lambda path: {**request, "idle": True, "pending": False, "editorBytes": 0},
        run=run,
    )
    result = module.resume_once(recovery, {"fenceNonce": "b" * 64}, directory)
    assert result["disposition"] == "uncertain_no_resend"
    assert len(sent) == 1
    with pytest.raises(FileExistsError):
        module.resume_once(recovery, {"fenceNonce": "b" * 64}, directory)
    assert len(sent) == 1


def test_false_controller_proof_cannot_dispatch():
    with pytest.raises(RuntimeError, match="successful recovery"):
        module.controller_proof(
            {"pid": 123, "pane": "w1:p1"}, {"pid": 123, "pane": "w1:p1", "fenceEnabled": False}
        )
