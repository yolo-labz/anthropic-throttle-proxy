import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import export_memory_context as export


@pytest.fixture
def flow(tmp_path, monkeypatch):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    guard = tmp_path / "guard"
    guard.mkdir(mode=0o700)
    monkeypatch.setattr(export.context.core, "ROOT", guard)
    monkeypatch.setattr(export, "bootstrap_guard", lambda identity: None)
    monkeypatch.setattr(export.context.idle, "empty_editor", lambda screen: True)
    rows = [
        {"type": "session", "id": "session-id", "cwd": "/project"},
        {"type": "message", "id": "user", "parentId": None, "message": {"role": "user"}},
        {"type": "message", "id": "reply", "parentId": "user", "message": {"role": "assistant"}},
    ]

    def encoded():
        return ("\n".join(json.dumps(row) for row in rows) + "\n").encode()

    transcript = tmp_path / "original.jsonl"
    transcript.write_bytes(encoded())
    expected = {
        "branchEntries": 2,
        "branchIdDigest": export.digest(["user", "reply"]),
        "workEntries": 2,
        "workIdDigest": export.digest(["user", "reply"]),
    }
    identity = dict(
        pid=123,
        transcript=str(transcript),
        cwd="/project",
        herdrSession="home",
        pane="w1:p1",
        provider="claude-lane",
        model="claude-opus-5-5",
        effort="max",
        install="/accepted",
        role="eng~",
        name="seat",
        session=str(transcript),
    )
    commands = []

    def run(command):
        commands.append(command)
        Path(command[-1].removeprefix("/export ")).write_bytes(encoded())

    operation = SimpleNamespace(
        identity=identity,
        deadline=time.monotonic() + 60,
        native_identity=lambda: None,
        screen=lambda: "screen",
        run=run,
        state_commands=[],
        request=lambda directory: {"nonce": "a" * 64},
    )
    operation.deliver = operation.state_commands.append
    operation.receipt = lambda path: {
        "nonce": "a" * 64,
        "command": "core-upgrade-state",
        "reason": "native-context-mismatch",
        "actual": {
            **identity,
            "idle": True,
            "pending": False,
            "editorBytes": 0,
            "nativeContext": {
                **expected,
                "agrees": False,
                "transcriptSha256": export.hashlib.sha256(encoded()).hexdigest(),
            },
        },
    }
    return operation, expected, directory, rows, commands, encoded


def test_native_export_once_with_unchanged_original(flow):
    operation, expected, directory, _, commands, _ = flow
    before = Path(operation.identity["transcript"]).read_bytes()
    result = export.export_once(operation, expected, directory)
    assert result["completeMemorySnapshot"] and result["originalUnchanged"]
    assert result["historySettlementHold"] and result["inference"] is False
    assert not result["missingDiskWorkIds"] and not result["conflictingDiskWorkIds"]
    assert commands[0][2:4] == ["home", "w1:p1"] and commands[0][-1].startswith("/export /")
    assert Path(operation.identity["transcript"]).read_bytes() == before
    with pytest.raises(FileExistsError):
        export.export_once(operation, expected, directory)
    assert len(commands) == 1
    assert operation.state_commands == ["/core-upgrade-state " + "a" * 64]


@pytest.mark.parametrize("uncertain", ["exit", "timeout"])
def test_uncertain_delivery_requires_actual_artifact(flow, uncertain):
    operation, expected, directory, _, commands, _ = flow
    original_run = operation.run

    def run(command):
        original_run(command)
        if uncertain == "exit":
            raise subprocess.CalledProcessError(10, "herdr-prompt")
        raise subprocess.TimeoutExpired("herdr-prompt", 40)

    operation.run = run
    result = export.export_once(operation, expected, directory)
    assert result["deliveryReportedSuccess"] is False and len(commands) == 1


@pytest.mark.parametrize("mutation", ["duplicate", "parent", "header", "type", "advanced"])
def test_raw_export_near_misses(flow, mutation):
    operation, expected, _, rows, _, encoded = flow
    original = encoded()
    if mutation == "duplicate":
        rows[-1]["id"] = "user"
    elif mutation == "parent":
        rows[-1]["parentId"] = None
    elif mutation == "header":
        rows[0]["id"] = "different"
    elif mutation == "type":
        rows[-1].pop("type")
    else:
        expected["branchIdDigest"] = "a" * 64
    with pytest.raises(RuntimeError):
        export.verify_export(original, encoded(), operation.identity, expected)


def test_original_disk_work_missing_is_reported_without_false_reconciliation(flow):
    operation, expected, _, rows, _, encoded = flow
    exported = encoded()
    rows.append(
        {"type": "message", "id": "disk-only", "parentId": "reply", "message": {"role": "user"}}
    )
    result = export.verify_export(encoded(), exported, operation.identity, expected)
    assert result["missingDiskWorkIds"] == ["disk-only"] and result["historySettlementHold"]


def test_advanced_native_context_records_hold_without_resend(flow):
    operation, expected, directory, _, commands, _ = flow
    expected["branchIdDigest"] = "a" * 64
    with pytest.raises(RuntimeError, match="advanced"):
        export.export_once(operation, expected, directory)
    receipt = next(export.context.core.ROOT.glob("*.result.json"))
    assert json.loads(receipt.read_text())["resendPermitted"] is False
    assert len(commands) == 1


def test_busy_identity_prevents_any_command(flow):
    operation, expected, directory, _, commands, _ = flow
    operation.native_identity = lambda: (_ for _ in ()).throw(RuntimeError("busy"))
    with pytest.raises(RuntimeError, match="busy"):
        export.export_once(operation, expected, directory)
    assert not commands


def test_original_advancing_during_export_is_held(flow):
    operation, expected, directory, _, commands, _ = flow
    original_run = operation.run

    def run(command):
        original_run(command)
        with Path(operation.identity["transcript"]).open("ab") as output:
            output.write(b"\n")

    operation.run = run
    with pytest.raises(RuntimeError, match="original transcript advanced"):
        export.export_once(operation, expected, directory)
    assert len(commands) == 1


def test_file_copy_of_reparented_disk_cannot_pass_as_memory_snapshot(flow):
    operation, expected, _, rows, _, encoded = flow
    original = encoded()
    rows[-1]["parentId"] = None
    with pytest.raises(RuntimeError, match="linear"):
        export.verify_export(original, encoded(), operation.identity, expected)


def test_dangling_disk_metadata_still_preserves_native_snapshot(flow):
    operation, expected, _, rows, _, encoded = flow
    exported = encoded()
    rows.append({"type": "custom", "id": "dangling", "parentId": "missing", "customType": "audit"})
    result = export.verify_export(encoded(), exported, operation.identity, expected)
    assert result["completeMemorySnapshot"] and result["historySettlementHold"]
    assert result["diskAncestryValid"] is False and result["originalDiskActiveWork"] is None
    assert result["originalDiskRawWork"] == 2 and result["sharedRawContentMatches"]
    assert result["nativeRawContentEqualityClaimed"] is False


@pytest.mark.parametrize("kind", ["file", "symlink", "ancestor", "mode"])
def test_private_output_path_near_misses(flow, tmp_path, kind):
    operation, expected, directory, _, commands, _ = flow
    if kind == "file":
        directory = tmp_path / "regular-file"
        directory.write_text("not a directory")
    elif kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(directory)
        directory = link
    elif kind == "ancestor":
        link = tmp_path / "ancestor"
        link.symlink_to(tmp_path)
        directory = link / "private"
    else:
        directory.chmod(0o755)
    with pytest.raises(RuntimeError):
        export.export_once(operation, expected, directory)
    assert not commands


def test_alternate_directory_cannot_send_export_twice(flow, tmp_path):
    operation, expected, directory, _, commands, _ = flow
    export.export_once(operation, expected, directory)
    alternate = tmp_path / "alternate"
    alternate.mkdir(mode=0o700)
    with pytest.raises(FileExistsError):
        export.export_once(operation, expected, alternate)
    assert len(commands) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "claude-fable-5-2"),
        ("effort", "xhigh"),
        ("pending", True),
        ("idle", False),
        ("editorBytes", 1),
        ("pid", 999),
    ],
)
def test_fresh_native_state_near_misses_prevent_export(flow, field, value):
    operation, expected, directory, _, commands, _ = flow
    original_receipt = operation.receipt

    def receipt(path):
        body = original_receipt(path)
        body["actual"][field] = value
        return body

    operation.receipt = receipt
    with pytest.raises(RuntimeError, match="not proven"):
        export.export_once(operation, expected, directory)
    assert not commands and len(operation.state_commands) == 1


def test_stale_or_foreign_state_nonce_prevents_export(flow):
    operation, expected, directory, _, commands, _ = flow
    original_receipt = operation.receipt
    operation.receipt = lambda path: {**original_receipt(path), "nonce": "b" * 64}
    with pytest.raises(RuntimeError, match="not proven"):
        export.export_once(operation, expected, directory)
    assert not commands


def test_valid_partial_prefix_is_not_read_before_native_completion(flow, monkeypatch):
    operation, expected, directory, rows, commands, encoded = flow
    artifact = []

    def run(command):
        commands.append(command)
        path = Path(command[-1].removeprefix("/export "))
        artifact.append(path)
        path.write_text(json.dumps(rows[0]) + "\n")

    original_wait = export.wait_closed

    def complete(changes, path):
        assert len(artifact[0].read_text().splitlines()) == 1
        artifact[0].write_bytes(encoded())
        original_wait(changes, path)

    operation.run = run
    monkeypatch.setattr(export, "wait_closed", complete)
    result = export.export_once(operation, expected, directory)
    assert result["nativeExportCompletionObserved"] and result["workEntries"] == 2
    assert len(commands) == 1


@pytest.mark.parametrize("failure", ["missing-close", "missing-artifact"])
def test_incomplete_export_proof_holds_without_resend(flow, monkeypatch, failure):
    operation, expected, directory, _, commands, _ = flow
    if failure == "missing-close":
        monkeypatch.setattr(
            export,
            "wait_closed",
            lambda *args: (_ for _ in ()).throw(TimeoutError("no completion")),
        )
    else:
        operation.run = commands.append
        monkeypatch.setattr(export, "wait_closed", lambda *args: None)
    with pytest.raises((RuntimeError, TimeoutError, FileNotFoundError)):
        export.export_once(operation, expected, directory)
    receipt = next(export.context.core.ROOT.glob("*.result.json"))
    assert json.loads(receipt.read_text())["resendPermitted"] is False and len(commands) == 1


@pytest.mark.parametrize(
    "mask,name,closed",
    [(8, "exact.jsonl", True), (2, "exact.jsonl", False), (8, "other.jsonl", False)],
)
def test_close_write_must_match_exact_artifact(mask, name, closed):
    name_bytes = name.encode() + b"\0"
    event = export.struct.pack("iIII", 1, mask, 0, len(name_bytes)) + name_bytes
    assert export.closed_artifact(event, "exact.jsonl") is closed


def test_completion_event_overflow_is_a_hold():
    with pytest.raises(RuntimeError, match="overflow"):
        export.closed_artifact(export.struct.pack("iIII", -1, 0x4000, 0, 0), "exact.jsonl")


def test_delayed_native_export_uses_remaining_operation_deadline(monkeypatch):
    name = b"exact.jsonl\0"
    event = export.struct.pack("iIII", 1, 8, 0, len(name)) + name
    monkeypatch.setattr(export.time, "monotonic", lambda: 100)

    def select_event(readers, writers, errors, timeout):
        # The real pilot wrote 55.85s after intent, after the former 20s wait expired.
        return (readers, [], []) if timeout >= 38 else ([], [], [])

    monkeypatch.setattr(export.context.idle.select, "select", select_event)
    monkeypatch.setattr(export.os, "read", lambda fd, size: event)
    export.wait_closed(SimpleNamespace(fd=123, deadline=170), Path("exact.jsonl"))


def test_export_completion_never_extends_operation_deadline(monkeypatch):
    monkeypatch.setattr(export.time, "monotonic", lambda: 100)
    observed = []

    def no_event(readers, writers, errors, timeout):
        observed.append(timeout)
        return [], [], []

    monkeypatch.setattr(export.context.idle.select, "select", no_event)
    with pytest.raises(TimeoutError, match="close-write"):
        export.wait_closed(SimpleNamespace(fd=123, deadline=107), Path("exact.jsonl"))
    assert observed == [7]
