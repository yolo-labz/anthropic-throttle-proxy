"""One client accounting domain, exact counts / one wall-clock minute."""

import json
from datetime import UTC, datetime

import pytest

from anthropic_throttle_proxy import output_usage

NOW = 1000.0


def event(ts, output=0, provider="codex-a", **extra):
    return {
        "ts": datetime.fromtimestamp(ts, UTC).isoformat(),
        "provider": provider,
        "output": output,
        **extra,
    }


def journal(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def ready(path):
    return journal(path, [event(900), event(970, 600), event(980, 1200, "zai")])


def test_two_concurrent_sources_share_one_denominator(tmp_path):
    rows = [event(900), event(970, 600, duration_s=2), event(980, 1200, "zai", duration_s=40)]
    result = output_usage.read_usage(journal(tmp_path / "usage.jsonl", rows), now=NOW)
    assert result.gauge.seen
    assert result.output_tokens == 1800
    assert result.gauge.value == 30
    assert result.gauge.window_s == 60
    assert set(result.providers) == {"codex-a", "zai"}
    assert not result.reason


def test_half_open_window_boundaries_and_output_only(tmp_path):
    rows = [
        event(900, 999),
        event(939.999, 999),
        event(940, 600, input=999999, cacheRead=999999),
        event(999.999, 1200, "mimo-desktop-subscription"),
        event(NOW, 999),
    ]
    result = output_usage.read_usage(journal(tmp_path / "usage.jsonl", rows), now=NOW)
    assert result.gauge.seen
    assert result.output_tokens == 1800
    assert result.gauge.value == 30


def test_complete_idle_window_is_measured_zero(tmp_path):
    result = output_usage.read_usage(
        journal(tmp_path / "usage.jsonl", [event(900, 123), event(930, 456)]), now=NOW
    )
    assert result.gauge.seen
    assert result.gauge.value == 0
    assert not result.reason


@pytest.mark.parametrize("case", ["missing", "empty", "warmup", "stale", "partial"])
def test_unverified_coverage_is_not_measured_zero(tmp_path, case):
    path = tmp_path / "usage.jsonl"
    if case == "empty":
        path.write_text("")
    elif case == "warmup":
        journal(path, [event(980, 600)])
    elif case == "stale":
        journal(path, [event(700, 600)])
    elif case == "partial":
        ready(path)
        with path.open("a") as file:
            file.write('{"ts":')
    result = output_usage.read_usage(path, now=NOW)
    assert not result.gauge.seen
    assert result.reason
    if case == "stale":
        assert result.gauge.stale


@pytest.mark.parametrize(
    "change",
    [
        {"output": -1},
        {"output": float("nan")},
        {"output": float("inf")},
        {"output": True},
        {"output": 1.5},
        {"output": "600"},
        {"output": 2**53 + 1},
        {"provider": None},
        {"provider": ""},
        {"ts": "not a timestamp"},
        {"ts": "1970-01-01T00:16:20"},
        {"ts": float("nan")},
        {"ts": datetime.fromtimestamp(1001, UTC).isoformat()},
    ],
)
def test_invalid_or_future_events_cannot_fabricate_coverage(tmp_path, change):
    row = {**event(980, 1200), **change}
    result = output_usage.read_usage(journal(tmp_path / "usage.jsonl", [event(900), row]), now=NOW)
    assert not result.gauge.seen
    assert result.reason


@pytest.mark.parametrize("raw", ['{"ts":broken}\n', "[]\n", "null\n", '{"output":123}\n'])
def test_unsupported_or_malformed_rows_are_unknown(tmp_path, raw):
    path = ready(tmp_path / "usage.jsonl")
    with path.open("a") as file:
        file.write(raw)
    assert not output_usage.read_usage(path, now=NOW).gauge.seen


def test_truncated_tail_needs_the_common_window_boundary(tmp_path):
    path = journal(tmp_path / "usage.jsonl", [event(900), event(980, 600), event(990, 1200)])
    result = output_usage.read_usage(path, now=NOW, max_bytes=130)
    assert not result.gauge.seen
    assert "window" in result.reason
    assert output_usage.read_usage(path, now=NOW, max_bytes=4096).gauge.value == 30


def test_bounded_tail_drops_only_its_incomplete_leading_line(tmp_path):
    path = journal(tmp_path / "usage.jsonl", [event(700)] * 100 + [event(900), event(980, 600)])
    result = output_usage.read_usage(path, now=NOW, max_bytes=4096)
    assert result.gauge.seen
    assert result.gauge.value == 10


def test_same_file_alias_has_same_identity_and_no_accumulation(tmp_path):
    path = ready(tmp_path / "usage.jsonl")
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(path)
    first = output_usage.read_usage(path, now=NOW)
    repeated = output_usage.read_usage(alias, now=NOW)
    assert repeated.identity == first.identity
    assert repeated.gauge.value == first.gauge.value == 30


async def test_refresh_replaces_snapshot_and_replacement_invalidates_old_reading(
    tmp_path, monkeypatch
):
    path = ready(tmp_path / "usage.jsonl")
    monkeypatch.setenv("THROTTLE_PI_USAGE_PATH", str(path))
    monkeypatch.setattr(output_usage, "_cache", None)
    await output_usage.refresh(now=NOW)
    first = output_usage.cached(now=NOW)
    await output_usage.refresh(now=NOW)
    assert output_usage.cached(now=NOW).gauge.value == 30
    replacement = journal(tmp_path / "replacement.jsonl", [event(990, 999)])
    replacement.replace(path)
    await output_usage.refresh(now=NOW)
    result = output_usage.cached(now=NOW)
    assert result.identity != first.identity
    assert not result.gauge.seen
    assert "window" in result.reason


async def test_cached_projection_does_not_read_or_refresh_age(tmp_path, monkeypatch):
    monkeypatch.setenv("THROTTLE_PI_USAGE_PATH", str(ready(tmp_path / "usage.jsonl")))
    monkeypatch.setattr(output_usage, "_cache", None)
    await output_usage.refresh(now=NOW)

    def forbidden(*args, **kwargs):
        pytest.fail("render attempted journal I/O")

    monkeypatch.setattr(output_usage, "read_usage", forbidden)
    assert output_usage.cached(now=NOW + 2).gauge.value == 30
    assert output_usage.cached(now=NOW + 2).sampled_at == NOW
    stale = output_usage.cached(now=NOW + 16)
    assert stale.gauge.stale
    assert not stale.gauge.seen
    assert "cache stale" in stale.reason


def test_replacement_during_read_cannot_publish_previous_identity(tmp_path, monkeypatch):
    from pathlib import Path

    path = ready(tmp_path / "usage.jsonl")
    replacement = journal(tmp_path / "replacement.jsonl", [event(990, 999)])
    original_stat = Path.stat

    def changed(file, *args, **kwargs):
        if file == path:
            replacement.replace(path)
        return original_stat(file, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", changed)
    result = output_usage.read_usage(path, now=NOW)
    assert not result.gauge.seen
    assert "changed while sampling" in result.reason


@pytest.mark.parametrize("bound", [0, -1, True])
def test_read_bound_cannot_trigger_unbounded_io(tmp_path, bound):
    result = output_usage.read_usage(ready(tmp_path / "usage.jsonl"), now=NOW, max_bytes=bound)
    assert not result.gauge.seen
    assert "read bound" in result.reason


def test_empty_cache_and_backward_clock_are_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(output_usage, "_cache", None)
    assert not output_usage.cached(now=NOW).gauge.seen
    monkeypatch.setattr(
        output_usage, "_cache", output_usage.read_usage(ready(tmp_path / "usage.jsonl"), now=NOW)
    )
    assert not output_usage.cached(now=NOW - 1).gauge.seen


def test_default_clock_is_used_without_refreshing_the_cached_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(output_usage.time, "time", lambda: NOW)
    result = output_usage.read_usage(ready(tmp_path / "usage.jsonl"))
    assert result.gauge.value == 30
    assert result.sampled_at == NOW
    monkeypatch.setattr(output_usage, "_cache", result)
    assert output_usage.cached().sampled_at == NOW


def test_fresh_mtime_does_not_refresh_old_completion_evidence(tmp_path):
    path = journal(tmp_path / "usage.jsonl", [event(700, 123)])
    path.touch()
    result = output_usage.read_usage(path, now=NOW)
    assert result.gauge.stale
    assert not result.gauge.seen
