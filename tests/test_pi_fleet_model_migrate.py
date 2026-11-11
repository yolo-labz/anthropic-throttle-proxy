"""Native branch and delivery guards; no live panes or providers."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "migrate", Path(__file__).parents[1] / "scripts/pi-fleet-model-migrate.py"
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def mock_native_pane(monkeypatch, path, footer):
    seat = {"session": "home", "pane_id": "w:p", "cwd": "/same", "transcript": str(path)}
    state = {
        "agent": "pi",
        "cwd": "/same",
        "agent_session": {"source": "herdr:pi", "value": str(path)},
    }
    monkeypatch.setattr(
        migration,
        "call",
        lambda *args: (
            json.dumps({"result": {"agent": state}})
            if "get" in args
            else "─" * 40 + "\n\n" + "─" * 40 + "\n" + footer()
        ),
    )
    return seat


def test_abandoned_branch_does_not_select_model(tmp_path):
    path = tmp_path / "session.jsonl"
    entries = [
        {
            "id": "a",
            "parentId": None,
            "type": "model_change",
            "provider": "claude-lane",
            "modelId": "claude-fable-5-2",
        },
        {
            "id": "abandoned",
            "parentId": "a",
            "type": "model_change",
            "provider": "other",
            "modelId": "wrong",
        },
        {"id": "active", "parentId": "a", "type": "thinking_level_change", "thinkingLevel": "max"},
    ]
    path.write_text("\n".join(map(json.dumps, entries)))
    assert migration.active_selection(path) == (migration.SOURCE, "max")
    entries[-1]["parentId"] = "missing"
    path.write_text("\n".join(map(json.dumps, entries)))
    with pytest.raises(ValueError, match="ambiguous"):
        migration.active_selection(path)


@pytest.mark.parametrize("draft", ["human text", "/model something", "  draft"])
def test_draft_refused(draft):
    with pytest.raises(ValueError, match="draft"):
        migration.visible_state(f"{'─' * 30}\n{draft}\n{'─' * 30}\n🧠 footer")


def test_transcript_model_mentions_are_not_footer_evidence():
    with pytest.raises(ValueError):
        migration.visible_state("claude-lane claude-fable-5-2 max")
    with pytest.raises(ValueError):
        migration.footer_selection("🧠 claude-fable-5-2 max")


@pytest.mark.parametrize("selected", [(migration.SOURCE, "max"), (("other", "model"), "max")])
def test_failed_delivery_never_blindly_resends(tmp_path, monkeypatch, selected):
    seat = {"session": "home", "pane_id": "w:p", "transcript": "/same"}
    calls = []
    monkeypatch.setattr(migration, "inspect", lambda unused: selected)

    def fail(*args):
        calls.append(args)
        raise TimeoutError

    monkeypatch.setattr(migration, "call", fail)
    first = migration.migrate(seat, tmp_path, True)
    second = migration.migrate(seat, tmp_path, True)
    assert first["status"] == second["status"] == "held"
    assert len(calls) == (1 if selected[0] == migration.SOURCE else 0)


def test_success_uses_only_native_commands_and_verifies_same_seat(tmp_path, monkeypatch):
    seat = {"session": "home", "pane_id": "w:p", "transcript": "/same"}
    selection = [migration.SOURCE, "high"]
    calls = []
    monkeypatch.setattr(migration, "inspect", lambda unused: tuple(selection))

    def deliver(*args):
        calls.append(args)
        if args[-1].startswith("/model"):
            selection[0] = migration.TARGET
        else:
            selection[1] = "max"

    monkeypatch.setattr(migration, "call", deliver)
    assert migration.migrate(seat, tmp_path, True)["status"] == "verified"
    assert [call[-1] for call in calls] == ["/model claude-lane/claude-opus-5-5", "/thinking max"]
    assert migration.migrate(seat, tmp_path, True)["status"] == "verified"
    assert len(calls) == 2


@pytest.mark.parametrize("token", ["claude-fable-5-2", "fable-5-2"])
def test_exact_native_display_token(token):
    assert migration.footer_selection(f"🧠 {token} claude-lane max") == (migration.SOURCE, "max")


@pytest.mark.parametrize(
    "footer",
    [
        "🧠 fable-5-2 opus-5-5 claude-lane max",
        "🧠 not-fable-5-2 claude-lane max",
        "🧠 fable-5-20 claude-lane max",
        "🧠 fable-5-2 claude-lane high max",
    ],
)
def test_ambiguous_or_near_miss_footer_refused(footer):
    with pytest.raises(ValueError, match="ambiguous"):
        migration.footer_selection(footer)


def test_retry_editor_boundary_and_goal_prose_are_distinct():
    text = (
        "── ⠋ Retrying (1/2) in 15s... (escape to cancel) "
        + "─" * 30
        + "\n\n"
        + "─" * 60
        + "\nπ │ 🧠 11% │ 🤖 fable-5-2 claude-lane\n🎭 orch │ ⚡ max"
        + "\n🎯 Keep work on Opus xhigh"
    )
    assert migration.footer_selection(migration.visible_state(text)) == (migration.SOURCE, "max")
    with pytest.raises(ValueError, match="draft"):
        migration.visible_state(text.replace("\n\n", "\nhuman draft\n"))


@pytest.mark.parametrize(
    "footer,accepted",
    [
        ("🧠 🤖 fable-5-2 claude-lane\n⚡ max", True),
        ("🧠 🤖 fable-5-2 opus-5-5 claude-lane\n⚡ max", False),
    ],
)
def test_pruned_parent_requires_exact_fresh_footer(tmp_path, monkeypatch, footer, accepted):
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"id": "latest", "parentId": "pruned"}))
    seat = mock_native_pane(monkeypatch, path, lambda: footer)
    if accepted:
        assert migration.inspect(seat) == (migration.SOURCE, "max")
    else:
        with pytest.raises(ValueError, match="ambiguous"):
            migration.inspect(seat)
    path.write_text(json.dumps({"id": "latest", "parentId": "latest"}))
    with pytest.raises(ValueError, match="cyclic"):
        migration.inspect(seat)


@pytest.mark.parametrize("prior_thinking", [False, True])
def test_target_high_completes_only_unattempted_thinking_phase(
    tmp_path, monkeypatch, prior_thinking
):
    import hashlib

    seat = {"session": "home", "pane_id": "w:p", "transcript": "/same"}
    digest = hashlib.sha256(b"home:w:p").hexdigest()
    receipt = tmp_path / f"{digest}.jsonl"
    commands = ["/model claude-lane/claude-opus-5-5"]
    if prior_thinking:
        commands.append("/thinking max")
    receipt.write_text(
        "\n".join(json.dumps({"status": "attempt", "command": command}) for command in commands)
        + "\n"
    )
    selected = [migration.TARGET, "high"]
    monkeypatch.setattr(migration, "inspect", lambda unused: tuple(selected))
    calls = []

    def deliver(*args):
        calls.append(args[-1])
        selected[1] = "max"

    monkeypatch.setattr(migration, "call", deliver)
    result = migration.migrate(seat, tmp_path, True)
    assert calls == ([] if prior_thinking else ["/thinking max"])
    assert result["status"] == ("held" if prior_thinking else "verified")


def test_known_native_model_uses_only_dedicated_effort_row(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "latest",
                "parentId": None,
                "type": "model_change",
                "provider": "claude-lane",
                "modelId": "claude-opus-5-5",
            }
        )
    )
    footer = ["🧠 🤖 last:fable-5-2 now: op…\n⚡ high\n🎯 goal max"]
    seat = mock_native_pane(monkeypatch, path, lambda: footer[0])
    assert migration.inspect(seat) == (migration.TARGET, "high")
    footer[0] = "🧠 🤖 last:fable-5-2 now: op…\n⚡ high max"
    with pytest.raises(ValueError, match="effort footer ambiguous"):
        migration.inspect(seat)
