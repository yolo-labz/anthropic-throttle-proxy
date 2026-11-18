import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "recover_core", Path(__file__).with_name("recover_core.py")
)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


@pytest.mark.parametrize(
    "failure",
    [
        None,
        subprocess.CalledProcessError(1, "herdr", stderr=b"private"),
        TimeoutError(),
        subprocess.TimeoutExpired("herdr", 35),
    ],
)
@pytest.mark.parametrize("started", [True, False])
def test_launch_response_never_causes_duplicate_dispatch(monkeypatch, capsys, failure, started):
    args = SimpleNamespace(session="infra", pane="w:p", transcript="/native.jsonl", cwd="/original")
    calls = []

    def dispatch(*command):
        calls.append(command)
        if failure is not None:
            raise failure

    state = {
        "agent": "pi",
        "agent_status": "idle",
        "cwd": "/original",
        "agent_session": {
            "source": "herdr:pi",
            "value": "/native.jsonl" if started else "/wrong.jsonl",
        },
    }
    ticks = iter([0, 0, 31])
    monkeypatch.setattr(core, "call", dispatch)
    monkeypatch.setattr(core, "agent", lambda *unused: state)
    monkeypatch.setattr(core.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(core.time, "sleep", lambda unused: None)
    if started:
        core.dispatch_and_detect(args, "exact immutable command")
    else:
        with pytest.raises(RuntimeError, match="never retry"):
            core.dispatch_and_detect(args, "exact immutable command")
    assert calls == [
        ("herdr", "--session", "infra", "pane", "run", "w:p", "exact immutable command")
    ]
    diagnostic = capsys.readouterr().err
    assert "private" not in diagnostic
    assert "exact immutable command" not in diagnostic
    if failure is not None:
        assert json.loads(diagnostic)["event"] == "native_launch_uncertain"


def test_shell_requires_original_pid_and_cwd():
    state = {
        "shell_pid": 12,
        "foreground_processes": [{"pid": 12, "name": "zsh", "cwd": "/original"}],
    }
    assert core.shell_ready(state, 13, 12, "/original")
    for old, shell, cwd in [(12, 12, "/original"), (13, 99, "/original"), (13, 12, "/other")]:
        assert not core.shell_ready(state, old, shell, cwd)
    assert not core.shell_ready({**state, "foreground_processes": []}, 13, 12, "/original")


def test_screen_detected_idle_is_not_native_startup_completion():
    state = {"agent": "pi", "agent_status": "idle", "cwd": "/original"}
    assert not core.native_started(state, "/native.jsonl", "/original")
    state["agent_session"] = {"source": "herdr:pi", "value": "/native.jsonl"}
    assert core.native_started(state, "/native.jsonl", "/original")
    assert not core.native_started(state, "/other.jsonl", "/original")


@pytest.mark.parametrize("kind", ["context-recovery", "cross-family-model-target"])
def test_historical_startup_mutation_refused(tmp_path, kind):
    path = tmp_path / "native.jsonl"
    path.write_text(
        json.dumps({"id": "1", "parentId": None, "type": "custom", "customType": kind}) + "\n"
    )
    with pytest.raises(RuntimeError, match="startup target"):
        core.require_safe_branch(path)


def test_branch_uses_parent_lineage_and_rejects_unknown_parent(tmp_path):
    path = tmp_path / "native.jsonl"
    entries = [
        {"id": "root", "parentId": None},
        {"id": "other", "parentId": "root", "type": "custom", "customType": "context-recovery"},
        {"id": "last", "parentId": "root"},
    ]
    path.write_text("\n".join(map(json.dumps, entries)))
    core.require_safe_branch(path)
    entries[-1]["parentId"] = "missing"
    path.write_text("\n".join(map(json.dumps, entries)))
    with pytest.raises(RuntimeError, match="ambiguous"):
        core.require_safe_branch(path)


@pytest.mark.parametrize("attached", [False, True])
def test_orphan_metadata_cannot_qualify_as_preserved_work_context(tmp_path, attached):
    path = tmp_path / "native.jsonl"
    entries = [
        {"type": "session", "id": "session"},
        {
            "type": "message",
            "id": "work",
            "parentId": None,
            "message": {"role": "assistant", "content": []},
        },
        {
            "type": "custom",
            "id": "cleanup",
            "parentId": "work" if attached else None,
            "customType": "pi-artifact-cleanup",
        },
        {"type": "session_info", "id": "last", "parentId": "cleanup"},
    ]
    path.write_text("\n".join(map(json.dumps, entries)))
    if attached:
        core.require_safe_branch(path)
    else:
        with pytest.raises(RuntimeError, match="metadata-only branch"):
            core.require_safe_branch(path)


def test_genuinely_new_metadata_only_session_has_no_displaced_work(tmp_path):
    path = tmp_path / "native.jsonl"
    path.write_text(json.dumps({"type": "custom", "id": "fresh", "parentId": None}) + "\n")
    core.require_safe_branch(path)


@pytest.mark.parametrize("acknowledgement", ["ready", "blocked"])
def test_registration_waits_for_receipt_writer_and_never_repeats_start(
    tmp_path, monkeypatch, acknowledgement
):
    from types import SimpleNamespace

    args = SimpleNamespace(
        nonce="n",
        phase="restart",
        session="home",
        pane="w:p",
        name="gama-review",
        cwd="/original",
        transcript="/native.jsonl",
        provider="claude-lane",
        model="claude-fable-5-2",
        effort="max",
    )
    request = {
        "launchRoot": "/pinned",
        "targetRole": "review",
        "shellName": "zsh",
        "pid": 13,
        "shellPid": 12,
    }
    calls = []

    def fake_call(*command):
        calls.append(command)
        if "run" in command:
            (tmp_path / "n.restart.shell-ready").write_text(acknowledgement)
        return ""

    states = iter(
        [
            {"shell_pid": 12, "foreground_processes": [{"pid": 99, "name": "zsh"}]},
            {
                "shell_pid": 12,
                "foreground_processes": [{"pid": 12, "name": "zsh", "cwd": "/original"}],
            },
        ]
    )
    monkeypatch.setattr(core, "ROOT", tmp_path)
    monkeypatch.setattr(core, "call", fake_call)
    monkeypatch.setattr(core, "launcher_tool", lambda launcher, name: f"/immutable/{name}")
    monkeypatch.setattr(core, "info", lambda *unused: next(states))
    monkeypatch.setattr(core.time, "sleep", lambda unused: None)
    if acknowledgement == "blocked":
        with pytest.raises(RuntimeError, match="custom pi alias"):
            core.start_registered_repair(args, request, "/immutable/pi")
    else:
        core.start_registered_repair(args, request, "/immutable/pi")
    starts = [command for command in calls if "start" in command]
    assert len(starts) == (1 if acknowledgement == "ready" else 0)


def test_removed_expired_registration_escape_hatch(monkeypatch):
    monkeypatch.setattr("sys.argv", ["recover_core.py", "finish-registration"])
    with pytest.raises(SystemExit) as error:
        core.main()
    assert error.value.code == 2


@pytest.mark.parametrize(
    "provider,model,accepted",
    [
        ("openai-codex", "gpt-6.1-sol", True),
        ("openai-codex", "gpt-6-astra", False),
    ],
)
def test_consumed_startup_target_matches_installed_parity_semantics(
    tmp_path, provider, model, accepted
):
    path = tmp_path / "native.jsonl"
    entries = [
        {
            "id": "target",
            "parentId": None,
            "type": "custom",
            "customType": "cross-family-model-target",
            "data": {"provider": "openai-codex", "model": "gpt-6.1-sol", "thinking": "xhigh"},
        },
        {
            "id": "applied",
            "parentId": "target",
            "type": "custom",
            "customType": "cross-family-model-target-applied",
            "data": {"targetEntryId": "target", "provider": provider, "model": model},
        },
        {
            "id": "human-selection",
            "parentId": "applied",
            "type": "model_change",
            "provider": "claude-lane",
            "modelId": "claude-opus-5-5",
        },
    ]
    path.write_text("\n".join(map(json.dumps, entries)))
    if accepted:
        core.require_safe_branch(path)
    else:
        with pytest.raises(RuntimeError, match="startup target"):
            core.require_safe_branch(path)
    # An applied receipt on an abandoned branch cannot suppress a current startup target.
    entries[-1]["parentId"] = "target"
    path.write_text("\n".join(map(json.dumps, entries)))
    with pytest.raises(RuntimeError, match="startup target"):
        core.require_safe_branch(path)


@pytest.mark.parametrize("mutation", ["no-receipt", "wrong-target", "wrong-provider", "context"])
def test_startup_target_consumption_refuses_near_misses(tmp_path, mutation):
    path = tmp_path / "native.jsonl"
    target = {
        "id": "target",
        "parentId": None,
        "type": "custom",
        "customType": "cross-family-model-target",
        "data": {"provider": "openai-codex", "model": "gpt-6.1-sol", "thinking": "xhigh"},
    }
    receipt = {
        "id": "receipt",
        "parentId": "target",
        "type": "custom",
        "customType": "cross-family-model-target-applied",
        "data": {"targetEntryId": "target", "provider": "openai-codex", "model": "gpt-6.1-sol"},
    }
    entries = [target, receipt]
    if mutation == "no-receipt":
        entries = [target]
    elif mutation == "wrong-target":
        receipt["data"]["targetEntryId"] = "other"
    elif mutation == "wrong-provider":
        receipt["data"]["provider"] = "other"
    else:
        entries.append(
            {"id": "ctx", "parentId": "receipt", "type": "custom", "customType": "context-recovery"}
        )
    path.write_text("\n".join(map(json.dumps, entries)))
    with pytest.raises(RuntimeError, match="startup target"):
        core.require_safe_branch(path)


@pytest.mark.parametrize("mutation", ["missing", "leaf", "work", "append", "dangling"])
def test_context_proof_rejects_stale_native_or_disk(tmp_path, mutation):
    path = tmp_path / "context.jsonl"
    entries = [
        {"type": "session", "id": "header"},
        {"type": "message", "id": "work", "parentId": None},
    ]
    path.write_text("\n".join(map(json.dumps, entries)) + "\n")
    proof = core.disk_context(path)
    assert proof["branchEntries"] == proof["workEntries"] == 1
    assert core.require_context_agreement(path, proof) == proof
    if mutation == "missing":
        proof = None
    elif mutation in {"leaf", "work"}:
        proof["leaf" if mutation == "leaf" else "workIdDigest"] = "stale"
    else:
        with path.open("a") as output:
            output.write(
                json.dumps(
                    {
                        "type": "custom",
                        "id": "later",
                        "parentId": "work" if mutation == "append" else "absent",
                    }
                )
                + "\n"
            )
    with pytest.raises(RuntimeError, match="context"):
        core.require_context_agreement(path, proof)
