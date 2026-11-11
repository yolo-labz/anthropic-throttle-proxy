import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location(
    "recover_idle", Path(__file__).with_name("recover_idle.py")
)
idle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(idle)


def test_editor_requires_empty_native_boundary():
    border = "─" * 40
    assert idle.empty_editor(f"{border}\n\n{border}\n🧠 native footer")
    assert not idle.empty_editor(f"{border}\nhuman draft\n{border}\n🧠 native footer")
    assert not idle.empty_editor(f"{border}\n\n{border}\nunknown footer")
    assert not idle.empty_editor(f"── ⠋ Auto-compacting... (escape to cancel) ──\n\n{border}\n🧠")


@pytest.mark.parametrize(
    "changed", [{"pid": 9}, {"idle": False}, {"pending": True}, {"editorBytes": 1}]
)
def test_native_admission_rejects_near_misses(changed):
    request = {"pid": 1, "nonce": "exact", "session": "/same"}
    state = {**request, "idle": True, "pending": False, "editorBytes": 0}
    assert idle.native_admission(request, state)
    assert not idle.native_admission(request, {**state, **changed})


def test_actual_linux_notification_and_terminal_timeout(tmp_path):
    watcher = idle.Changes([tmp_path], time.monotonic() + 1)
    try:
        (tmp_path / "native.jsonl").write_text("new native checkpoint")
        watcher.wait()
        watcher.deadline = time.monotonic() - 1
        with pytest.raises(TimeoutError):
            watcher.wait()
    finally:
        watcher.close()


@pytest.fixture
def recovery_seat(tmp_path, monkeypatch):
    transcript = tmp_path / "native.jsonl"
    transcript.touch()
    monkeypatch.setattr(idle.core, "ROOT", tmp_path)
    args = []
    for key, value in {
        "session": "home",
        "pane": "w:p",
        "name": "named",
        "transcript": str(transcript),
        "cwd": "/same",
        "provider": "claude-lane",
        "model": "fixed",
        "effort": "max",
        "accepted-install": "/accepted",
        "accepted-source": "/source",
    }.items():
        args.extend(["--" + key, value])
    monkeypatch.setattr(sys, "argv", ["recover_idle", *args])
    return transcript


@pytest.mark.parametrize("drift", [None, "name", "session", "cwd"])
def test_reload_without_fresh_ack_never_delivers_native_command(
    tmp_path, monkeypatch, drift, recovery_seat
):
    transcript = recovery_seat
    monkeypatch.setattr(
        idle.core,
        "agent",
        lambda *_: {
            "agent": "pi",
            "agent_status": "idle",
            "name": "named",
            "cwd": "/same",
            "agent_session": {"source": "herdr:pi", "value": str(transcript)},
        },
    )
    calls = []

    def run(command, _deadline, **_kwargs):
        calls.append(command)
        if "get" in command:
            state = idle.core.agent()
            if drift and sum("get" in call for call in calls) > 1:
                if drift == "session":
                    state["agent_session"]["value"] = "/different"
                else:
                    state[drift] = "different"
            return json.dumps({"result": {"agent": state}})
        if "prepare" in command:
            return json.dumps({"nonce": "a" * 64})
        if "read" in command:
            return idle.ACK + "\n" + "─" * 40 + "\n\n" + "─" * 40 + "\n🧠"
        if command[0] == idle.HERDR_PROMPT:
            raise idle.subprocess.CalledProcessError(10, command)
        return ""

    monkeypatch.setattr(idle, "run", run)
    with pytest.raises(
        RuntimeError, match="identity changed" if drift else "fresh successful native reload"
    ):
        idle.main()
    delivered = [c[-1] for c in calls if c[0] == idle.HERDR_PROMPT]
    assert delivered == ([] if drift else ["/reload"])
    assert not any("restart" in c or "qualify-bootstrap" in c for c in calls)


def test_native_receipt_matches_complete_identity_not_only_idle():
    request = {
        "pid": 1,
        "session": "/same",
        "provider": "claude-lane",
        "model": "fixed",
        "effort": "max",
        "install": "/old",
        "nonce": "exact",
    }
    state = {**request, "idle": True, "pending": False, "editorBytes": 0}
    for key in request:
        assert not idle.native_admission(request, {**state, key: "changed"})


def test_shutdown_requires_full_restart_reserve():
    with pytest.raises(RuntimeError, match="reserve"):
        idle.require_reserve(time.monotonic() + 599)
    idle.require_reserve(time.monotonic() + 601)


def test_second_controller_lock_refused(tmp_path):
    import os

    path = tmp_path / "same-seat.lock"
    first = idle.acquire_lock(path)
    try:
        with pytest.raises(BlockingIOError):
            idle.acquire_lock(path)
    finally:
        os.close(first)
    second = idle.acquire_lock(path)
    os.close(second)


@pytest.mark.parametrize("failure", ["exit10", "timeout"])
@pytest.mark.parametrize("proof", [True, False])
@pytest.mark.parametrize("initially_working", [True, False])
def test_uncertain_native_delivery_requires_actual_receipts(
    tmp_path, monkeypatch, capsys, failure, proof, recovery_seat, initially_working
):
    transcript = recovery_seat

    class BoundedReceiptWait:
        def __init__(self, _paths, deadline):
            self.deadline = deadline

        def wait(self):
            raise TimeoutError("missing native receipt")

        def close(self):
            pass

    monkeypatch.setattr(idle, "Changes", BoundedReceiptWait)
    calls = []
    requests = []
    ack = False
    waited = False

    def run(command, _deadline, **_kwargs):
        nonlocal ack, waited
        calls.append(command)
        if "wait" in command:
            assert initially_working and not waited
            waited = True
            return "{}"
        if "get" in command:
            return json.dumps(
                {
                    "result": {
                        "agent": {
                            "agent": "pi",
                            "agent_status": "working"
                            if initially_working and not waited
                            else "idle",
                            "name": "named",
                            "cwd": "/same",
                            "agent_session": {"source": "herdr:pi", "value": str(transcript)},
                        }
                    }
                }
            )
        if "read" in command:
            return (idle.ACK if ack else "") + "\n" + "─" * 40 + "\n\n" + "─" * 40 + "\n🧠"
        if "prepare" in command:
            n = len(requests) + 1
            request = {
                "nonce": str(n) * 64,
                "pid": n,
                "install": "/old" if n == 1 else "/accepted",
                "session": str(transcript),
                "role": "eng~",
            }
            requests.append(request)
            (tmp_path / f"{request['nonce']}.request.json").write_text(json.dumps(request))
            return json.dumps(request)
        if command[0] == idle.HERDR_PROMPT:
            text = command[-1]
            if text == "/reload":
                ack = True
            else:
                kind = "ready" if text.startswith("/core-upgrade-ready") else "state"
                request = requests[-1]
                receipt = {**request, "idle": True, "pending": False, "editorBytes": 0}
                if proof:
                    (tmp_path / f"{request['nonce']}.{kind}.json").write_text(json.dumps(receipt))
            if failure == "exit10":
                raise idle.subprocess.CalledProcessError(10, command)
            raise idle.subprocess.TimeoutExpired(command, 40)
        return "{}"

    monkeypatch.setattr(idle, "run", run)
    if not proof:
        with pytest.raises(TimeoutError, match="missing native receipt"):
            idle.main()
        assert not any("restart" in c or "qualify-bootstrap" in c for c in calls)
        return
    idle.main()
    captured = capsys.readouterr()
    diagnostics = [json.loads(line) for line in captured.err.splitlines()]
    assert len(diagnostics) == 4
    assert all(
        item
        == {
            "event": "native_delivery_uncertain",
            "exceptionType": "CalledProcessError" if failure == "exit10" else "TimeoutExpired",
            "returncode": 10 if failure == "exit10" else None,
        }
        for item in diagnostics
    )
    result = json.loads(captured.out)
    assert result["verified"] and not result["promptSent"]
    delivered = [c[-1] for c in calls if c[0] == idle.HERDR_PROMPT]
    assert delivered == [
        "/reload",
        "/core-upgrade-state " + "1" * 64,
        "/core-upgrade-ready " + "1" * 64,
        "/core-upgrade-state " + "2" * 64,
    ]
    assert sum("restart" in c for c in calls) == 1
    assert sum("wait" in c for c in calls) == int(initially_working)


@pytest.mark.parametrize("status", [0, 7])
def test_operational_child_output_and_failure(status):
    command = [sys.executable, "-c", f"print('native-proof'); raise SystemExit({status})"]
    if status:
        with pytest.raises(idle.subprocess.CalledProcessError) as caught:
            idle.run(command, time.monotonic() + 2)
        assert caught.value.returncode == status
    else:
        assert idle.run(command, time.monotonic() + 2) == "native-proof\n"


def test_operational_timeout_reaps_actual_child(tmp_path):
    marker = tmp_path / "child-pid"
    command = [
        sys.executable,
        "-c",
        "import os,time; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(5)",
    ]
    with pytest.raises(idle.subprocess.TimeoutExpired):
        idle.run(command, time.monotonic() + 2, cap=0.2)
    assert marker.exists()
    assert not Path("/proc", marker.read_text()).exists()


def test_operational_executable_is_allowlisted():
    with pytest.raises(ValueError, match="unsupported operational executable"):
        idle.run(["/unapproved/executable"], time.monotonic() + 2)
