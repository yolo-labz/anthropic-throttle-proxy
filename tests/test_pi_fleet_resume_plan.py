import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "resume_plan", Path(__file__).parents[1] / "scripts/pi-fleet-resume-plan.py"
)
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


def write(path, entries):
    path.write_text("\n".join(map(json.dumps, entries)))
    return path


@pytest.mark.parametrize(
    "stop,expected",
    [
        ("error", "eligible_error"),
        ("aborted", "eligible_error"),
        ("toolUse", "eligible_unfinished_toolchain"),
        ("stop", "hold_completed_or_uncertain"),
    ],
)
def test_structural_outcomes_only(tmp_path, stop, expected):
    path = write(
        tmp_path / "session.jsonl",
        [
            {
                "id": "a",
                "parentId": None,
                "type": "message",
                "message": {"role": "assistant", "stopReason": stop},
            },
        ],
    )
    assert plan.disposition(path) == expected


def test_abandoned_error_does_not_resume_completed_branch(tmp_path):
    path = write(
        tmp_path / "session.jsonl",
        [
            {
                "id": "a",
                "parentId": None,
                "type": "message",
                "message": {"role": "assistant", "stopReason": "stop"},
            },
            {
                "id": "other",
                "parentId": "a",
                "type": "message",
                "message": {"role": "assistant", "stopReason": "error"},
            },
            {"id": "active", "parentId": "a", "type": "model_change"},
        ],
    )
    assert plan.disposition(path) == "hold_completed_or_uncertain"


def test_unconsumed_human_not_duplicated(tmp_path):
    path = write(
        tmp_path / "session.jsonl",
        [
            {
                "id": "a",
                "parentId": None,
                "type": "message",
                "message": {"role": "assistant", "stopReason": "error"},
            },
            {"id": "human", "parentId": "a", "type": "message", "message": {"role": "user"}},
        ],
    )
    assert plan.disposition(path) == "hold_unconsumed_human"


def test_policy_error_is_not_a_retriable_turn(tmp_path):
    path = write(
        tmp_path / "session.jsonl",
        [
            {
                "id": "a",
                "parentId": None,
                "type": "message",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "privacy policy refusal",
                },
            },
        ],
    )
    assert plan.disposition(path) == "hold_policy_hardware_or_storage"


def test_resume_timeout_never_retries_and_never_targets_working(tmp_path, monkeypatch):
    seat = {"session": "home", "pane_id": "w:p", "transcript": "/same"}
    calls = []
    monkeypatch.setattr(
        plan, "inspect", lambda *unused, **kwargs: {"disposition": "eligible_error"}
    )

    def fail(*args):
        calls.append(args)
        raise TimeoutError

    monkeypatch.setattr(plan.migration, "call", fail)
    assert plan.resume_once(seat, tmp_path)["disposition"] == "attempt_unverified_no_resend"
    assert plan.resume_once(seat, tmp_path)["disposition"] == "hold_previous_attempt_no_resend"
    assert len(calls) == 1
    seat["pane_id"] = "w:working"
    monkeypatch.setattr(
        plan, "inspect", lambda *unused, **kwargs: {"disposition": "leave_active_or_unknown"}
    )
    assert plan.resume_once(seat, tmp_path)["disposition"] == "leave_active_or_unknown"
    assert len(calls) == 1


def test_malformed_native_message_is_not_idle_evidence(tmp_path):
    path = write(
        tmp_path / "session.jsonl",
        [
            {"id": "a", "parentId": None, "type": "message", "message": None},
        ],
    )
    with pytest.raises(ValueError, match="unknown native message"):
        plan.disposition(path)


def test_pruned_history_keeps_only_proved_current_outcome(tmp_path):
    path = write(
        tmp_path / "session.jsonl",
        [
            {
                "id": "latest",
                "parentId": "pruned",
                "type": "message",
                "message": {"role": "assistant", "stopReason": "aborted"},
            },
        ],
    )
    assert plan.disposition(path) == "eligible_error"
    path = write(
        path,
        [
            {
                "id": "latest",
                "parentId": "latest",
                "type": "message",
                "message": {"role": "assistant", "stopReason": "aborted"},
            },
        ],
    )
    with pytest.raises(ValueError, match="cyclic"):
        plan.disposition(path)


@pytest.mark.parametrize(
    "stop,expected",
    [
        ("toolUse", "eligible_pending_user"),
        ("error", "eligible_pending_user"),
        ("aborted", "eligible_pending_user"),
        ("stop", "hold_unconsumed_human"),
    ],
)
def test_pending_user_opt_in_only_for_proved_interrupted_turn(tmp_path, stop, expected):
    path = write(
        tmp_path / "session.jsonl",
        [
            {
                "id": "a",
                "parentId": None,
                "type": "message",
                "message": {"role": "assistant", "stopReason": stop},
            },
            {"id": "human", "parentId": "a", "type": "message", "message": {"role": "user"}},
        ],
    )
    assert plan.disposition(path) == "hold_unconsumed_human"
    assert plan.disposition(path, resume_pending_user=True) == expected


def test_pending_user_override_preserves_policy_gate(tmp_path):
    path = write(
        tmp_path / "session.jsonl",
        [
            {
                "id": "a",
                "parentId": None,
                "type": "message",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "privacy refusal",
                },
            },
            {"id": "human", "parentId": "a", "type": "message", "message": {"role": "user"}},
        ],
    )
    assert plan.disposition(path, True) == "hold_policy_hardware_or_storage"


def test_pending_user_opt_in_passes_fresh_inspection_and_once_gate(tmp_path, monkeypatch):
    seat = {"session": "home", "pane_id": "w:p", "transcript": "/same"}
    calls = []
    monkeypatch.setattr(
        plan,
        "inspect",
        lambda unused, pending=False, visible=False, **kwargs: {
            "disposition": "eligible_pending_user" if pending else "hold_unconsumed_human"
        },
    )
    monkeypatch.setattr(plan.migration, "call", lambda *args: calls.append(args))
    assert plan.resume_once(seat, tmp_path)["disposition"] == "hold_unconsumed_human"
    assert plan.resume_once(seat, tmp_path, True)["disposition"] == "submitted_once"
    assert (
        plan.resume_once(seat, tmp_path, True)["disposition"] == "hold_previous_attempt_no_resend"
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error,expected",
    [
        ("Error: This operation was aborted", "eligible_visible_abort"),
        ("Old quote Error: This operation was aborted", "hold_no_assistant"),
        ("Error: This operation was aborted\nprivacy refusal", "hold_no_assistant"),
        ("", "hold_no_assistant"),
    ],
)
def test_visible_abort_opt_in_requires_exact_standalone_safe_error(monkeypatch, error, expected):
    seat = {"session": "home", "pane_id": "w:p", "transcript": "/same"}
    monkeypatch.setattr(plan.migration, "inspect", lambda unused: (plan.migration.TARGET, "max"))
    monkeypatch.setattr(plan, "disposition", lambda *unused: "hold_no_assistant")
    monkeypatch.setattr(
        plan.migration,
        "call",
        lambda *args: (
            json.dumps({"result": {"agent": {"agent_status": "idle"}}}) if "get" in args else error
        ),
    )
    assert plan.inspect(seat)["disposition"] == "hold_no_assistant"
    assert plan.inspect(seat, resume_visible_abort=True)["disposition"] == expected


@pytest.mark.parametrize(
    "observed,expected,eligible",
    [("high", "high", True), ("max", "high", False), ("high", "max", False), ("high", None, False)],
)
def test_recovery_preserves_exact_previously_verified_effort(
    monkeypatch, observed, expected, eligible
):
    seat = {"session": "home", "pane_id": "w:p", "transcript": "/same"}
    monkeypatch.setattr(plan.migration, "inspect", lambda unused: (plan.migration.TARGET, observed))
    monkeypatch.setattr(plan, "disposition", lambda *unused: "eligible_error")
    monkeypatch.setattr(
        plan.migration,
        "call",
        lambda *args: (
            json.dumps({"result": {"agent": {"agent_status": "idle"}}}) if "get" in args else ""
        ),
    )
    assert (
        plan.inspect(seat, expected_effort=expected)["disposition"].startswith("eligible_")
        is eligible
    )


def test_nondefault_effort_is_rechecked_and_recorded_without_selection_commands(
    tmp_path, monkeypatch
):
    seat = {"session": "home", "pane_id": "w:p", "transcript": "/same"}
    expected_efforts, commands = [], []

    def inspect(*args, expected_effort="max"):
        expected_efforts.append(expected_effort)
        return {"disposition": "eligible_error"}

    monkeypatch.setattr(plan, "inspect", inspect)
    monkeypatch.setattr(plan.migration, "call", lambda *args: commands.append(args))
    assert (
        plan.resume_once(seat, tmp_path, expected_effort="high")["disposition"] == "submitted_once"
    )
    assert expected_efforts == ["high", "high"]
    assert commands == [("prompt", "-s", "home", "w:p", "Continue the work")]
    assert json.loads(next(tmp_path.glob("*.attempt.json")).read_text())["expectedEffort"] == "high"
    assert (
        plan.resume_once(seat, tmp_path, expected_effort="high")["disposition"]
        == "hold_previous_attempt_no_resend"
    )
