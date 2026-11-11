import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "recover_core", Path(__file__).with_name("recover_core.py")
)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


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
