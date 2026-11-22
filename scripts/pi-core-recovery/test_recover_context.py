import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location(
    "context_recovery", Path(__file__).with_name("recover_context.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def flow(tmp_path, monkeypatch):
    context = tmp_path / "context"
    guard = tmp_path / "guard"
    context.mkdir(mode=0o700)
    guard.mkdir(mode=0o700)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"id":"old"}\n')
    identity = dict(
        pid=123,
        herdrSession="home",
        pane="w1:p1",
        name="seat",
        transcript=str(transcript),
        session=str(transcript),
        cwd="/project",
        provider="claude-lane",
        model="claude-opus-5-5",
        effort="max",
        install="/accepted",
        role="eng~",
        targetLeaf="target",
    )
    monkeypatch.setattr(module, "ROOT", context)
    monkeypatch.setattr(module.core, "ROOT", guard)
    monkeypatch.setattr(module.idle, "Changes", lambda *args: None)
    monkeypatch.setattr(
        module.idle, "reload_editor_barrier", lambda deliver, wait, screen: deliver("/reload")
    )
    generations = {}

    def loaded(path, *args):
        generations[path] = generations.get(path, 0) + 1
        return None if generations[path] == 1 else "a" * 64

    monkeypatch.setattr(module, "loaded", loaded)
    recovery = module.Recovery(identity, time.monotonic() + 120)
    recovery.native_identity = lambda: None
    recovery.screen = lambda: "synthetic"
    recovery.until = lambda predicate: predicate() or (_ for _ in ()).throw(TimeoutError())
    commands = []
    receipts = {}

    def deliver(command):
        commands.append(command)
        if command == "/reload":
            return
        name, nonce = command.split()
        directory = context if name == "/context-branch-restore" else guard
        request = json.loads((directory / f"{nonce}.request.json").read_text())
        if name == "/core-upgrade-state":
            receipts["state"] = {**request, "idle": True, "pending": False, "editorBytes": 0}
        elif name == "/context-branch-restore":
            receipts["restored"] = dict(
                nonce=nonce,
                previousLeaf="old",
                targetLeaf="target",
                persistedLeaf="audit",
                originalSha256=request["transcriptSha256"],
                before=request,
                after=request,
            )
        else:
            receipts["compaction-fence"] = dict(
                nonce=nonce,
                pid=123,
                enabled=True,
                inference=False,
                version="prepared-dispatch-fence-v1",
            )

    recovery.deliver = deliver
    recovery.receipt = lambda path: receipts[path.name.split(".")[-2]]
    return recovery, commands, receipts


def test_once_full_flow_and_no_inference(flow):
    recovery, commands, _ = flow
    result = recovery.execute()
    assert result["restored"] and result["fenceEnabled"] and result["inference"] is False
    assert [c.split()[0] for c in commands] == [
        "/reload",
        "/core-upgrade-state",
        "/context-branch-restore",
        "/compaction-loop-fence-enable",
    ]
    with pytest.raises(FileExistsError):
        recovery.execute()
    assert len(commands) == 4


def test_missing_specific_load_stops_before_novel_slash(flow, monkeypatch):
    recovery, commands, _ = flow
    monkeypatch.setattr(module, "loaded", lambda *args: None)
    with pytest.raises(TimeoutError):
        recovery.execute()
    assert commands == ["/reload"]


def test_pending_native_state_stops_before_restore(flow):
    recovery, commands, receipts = flow
    receipt = recovery.receipt
    recovery.receipt = lambda path: {**receipt(path), "pending": True}
    with pytest.raises(RuntimeError, match="idle identity"):
        recovery.execute()
    assert len(commands) == 2
    assert "restored" not in receipts


def test_wrong_restore_identity_does_not_enable_fence(flow):
    recovery, commands, _ = flow
    receipt = recovery.receipt

    def drift(path):
        body = receipt(path)
        return {**body, "after": {"pid": -1}} if path.name.endswith("restored.json") else body

    recovery.receipt = drift
    with pytest.raises(RuntimeError, match="restoration proof"):
        recovery.execute()
    assert len(commands) == 3


def test_fence_only_preserves_normal_context(flow):
    recovery, commands, _ = flow
    del recovery.identity["targetLeaf"]
    result = recovery.execute()
    assert result["restored"] is False and result["fenceEnabled"] is True
    assert all(not c.startswith("/context-branch") for c in commands)


def test_private_marker_cannot_overwrite(tmp_path):
    marker = tmp_path / "once.json"
    module.write_once(marker, {"first": True})
    with pytest.raises(FileExistsError):
        module.write_once(marker, {"first": False})
    assert module.owned_json(marker) == {"first": True}


@pytest.mark.parametrize("marker", ["context-recovery", "cross-family-model-target"])
def test_unsafe_startup_replay_refuses_before_any_intent_or_delivery(flow, marker):
    recovery, commands, _ = flow
    del recovery.identity["targetLeaf"]
    entries = [
        {"type": "message", "id": "work", "parentId": None},
        {
            "type": "custom",
            "id": "replay",
            "parentId": "work",
            "customType": marker,
            "data": {"provider": "claude-lane", "model": "claude-fable-5-2", "thinking": "high"},
        },
    ]
    Path(recovery.identity["transcript"]).write_text("\n".join(map(json.dumps, entries)))
    with pytest.raises(RuntimeError, match="startup target"):
        recovery.execute()
    assert commands == []
    assert list(module.ROOT.iterdir()) == []
    assert list(module.core.ROOT.iterdir()) == []


def test_fence_only_accepts_work_bearing_custom_root_without_navigation(flow):
    recovery, commands, _ = flow
    del recovery.identity["targetLeaf"]
    entries = [
        {"type": "custom", "id": "split", "parentId": None, "customType": "pi-artifact-cleanup"},
        {"type": "message", "id": "new-work", "parentId": "split"},
    ]
    path = Path(recovery.identity["transcript"])
    original = "\n".join(map(json.dumps, entries))
    path.write_text(original)
    result = recovery.execute()
    assert result["fenceEnabled"] and not result["restored"]
    assert [c.split()[0] for c in commands] == [
        "/reload",
        "/core-upgrade-state",
        "/compaction-loop-fence-enable",
    ]
    assert path.read_text() == original
