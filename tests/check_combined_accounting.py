"""Independent synthetic journal acceptance; not inference or live fleet coverage.

Uses pT's actual output_usage.read_usage(path, now=...) seam.
Only its public gauge projection is normalized, never reimplemented.
No raw operational journal is read.
"""

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

NOW = datetime(2026, 10, 7, 22, 40, tzinfo=UTC).timestamp()


def event(age, output, provider="codex-a", **extra):
    return {
        "ts": datetime.fromtimestamp(NOW - age, UTC).isoformat(),
        "provider": provider,
        "output": output,
        "input": 90000,
        "cacheRead": 40000,
        "cacheWrite": 30000,
        **extra,
    }


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    os.utime(path, (NOW, NOW))


def candidate_read(path, now):
    from anthropic_throttle_proxy import output_usage

    result = output_usage.read_usage(path, now=now)
    gauge = result.gauge
    if gauge.seen:
        assert gauge.window_s == 60, "variable common denominator"
        assert gauge.value == result.output_tokens / 60, "output-only total mismatch"
    return {
        "value": gauge.value if gauge.seen else None,
        "status": "measured" if gauge.seen else "stale" if gauge.stale else "unknown",
        "identity": result.identity,
    }


def checks(read=candidate_read):
    passed = []
    with tempfile.TemporaryDirectory(prefix="combined-qa-") as directory:
        path = Path(directory) / "usage.jsonl"
        # Old sentinel establishes a >60s file history without entering the sum.
        base = [event(120, 77), event(30, 600, duration_s=120), event(1, 1200, "zai", duration_s=2)]
        write_rows(path, base)
        view = read(path, NOW)
        assert view["value"] == 30, view
        passed.append("600+1200 outputs / common60 =30; excludes prompt/cache/durations")
        assert read(path, NOW)["value"] == 30, "repeated source refresh double-counts"
        alias = Path(directory) / "alias.jsonl"
        alias.symlink_to(path)
        again = read(alias, NOW)
        assert again["value"] == 30 and again["identity"] == view["identity"], again
        passed.append("same source/alias refreshed twice is not added twice")
        for provider in (
            "openai-codex",
            "codex-a",
            "codex-b",
            "codex-c",
            "mimo-desktop-subscription",
        ):
            write_rows(path, [event(120, 1), event(1, 600, provider)])
            assert read(path, NOW)["value"] == 10, provider
        passed.append("Codex aliases/direct Desktop count once as client events")
        write_rows(
            path,
            [
                event(120, 77),
                event(60.001, 999),
                event(60, 600),
                event(59.999, 1200),
                event(0, 999),
            ],
        )
        assert read(path, NOW)["value"] == 30, "window is [now-60, now)"
        passed.append("declared lower inclusive / upper exclusive common window")
        write_rows(path, [event(120, 77), event(90, 100)])
        idle = read(path, NOW)
        assert idle["value"] == 0 and idle["status"] in {"measured", "partial"}, idle
        passed.append("observed idle interval is measured zero")
        write_rows(path, [event(1, 600)])
        warm = read(path, NOW)
        assert warm["status"] in {"partial", "unknown"}, warm
        assert warm["value"] in {None, 10}, warm
        passed.append("warmup cannot imply complete60s or use request duration")
        for label, bad in (
            ("negative", event(1, -1)),
            ("nonfinite", event(1, float("nan"))),
            ("infinity", event(1, float("inf"))),
            ("missing output", {"ts": event(1, 0)["ts"], "provider": "zai"}),
            ("future", event(-1, 9999)),
        ):
            write_rows(path, [*base, bad])
            result = read(path, NOW)
            assert result["status"] in {"partial", "unknown"}, (label, result)
            assert result["value"] in {None, 30}, (label, result)
            passed.append(label + " cannot be complete/healthy accounting")
        write_rows(path, base)
        with path.open("a") as target:
            target.write('{"ts":"unfinished')
        os.utime(path, (NOW, NOW))
        result = read(path, NOW)
        assert result["status"] in {"partial", "unknown"}, result
        assert result["value"] in {None, 30}, result
        passed.append("partial final write never claims complete coverage")
        replacement = Path(directory) / "replacement.jsonl"
        write_rows(replacement, [event(120, 1), event(1, 600)])
        replacement.replace(path)
        assert read(path, NOW)["value"] == 10, "replacement retains obsolete event totals"
        passed.append("atomic file replacement replaces, not accumulates")
        missing = read(Path(directory) / "missing.jsonl", NOW)
        assert missing["value"] is None and missing["status"] == "unknown", missing
        passed.append("missing file is unknown, not zero")
    return {"scope": "synthetic journals / real candidate reader", "passed": passed}


async def cache_checks():
    from anthropic_throttle_proxy import output_usage

    with tempfile.TemporaryDirectory(prefix="combined-cache-qa-") as directory:
        path = Path(directory) / "usage.jsonl"
        write_rows(path, [event(120, 1), event(30, 600), event(1, 1200, "zai")])
        with (
            patch.dict(os.environ, {"THROTTLE_PI_USAGE_PATH": str(path)}),
            patch.object(output_usage, "_cache", None),
        ):
            await output_usage.refresh(now=NOW)
            initial = output_usage.cached(now=NOW)
            assert initial.gauge.seen and initial.gauge.value == 30
            await output_usage.refresh(now=NOW)
            assert output_usage.cached(now=NOW).gauge.value == 30
            # A projection may neither perform I/O nor reset evidence age.
            with patch.object(output_usage, "read_usage", side_effect=AssertionError("render I/O")):
                fresh = output_usage.cached(now=NOW + 2)
                assert fresh.gauge.value == 30 and fresh.sampled_at == NOW
                stale = output_usage.cached(now=NOW + 16)
                assert stale.gauge.stale and not stale.gauge.seen
            replacement = Path(directory) / "new.jsonl"
            write_rows(replacement, [event(120, 1), event(1, 600)])
            replacement.replace(path)
            await output_usage.refresh(now=NOW)
            new = output_usage.cached(now=NOW)
            assert new.identity != initial.identity and new.gauge.value == 10
            path.unlink()
            await output_usage.refresh(now=NOW)
            assert not output_usage.cached(now=NOW).gauge.seen, "missing journal remains healthy"
            write_rows(path, [event(180, 1), event(119, 1)])
            await output_usage.refresh(now=NOW)
            assert output_usage.cached(now=NOW).gauge.seen
            expired = output_usage.cached(now=NOW + 2)
            assert expired.gauge.stale and not expired.gauge.seen, "completion aged past120s"
    return [
        "cache-only fixed-age projection / stale16s",
        "refresh replaces and invalidates source",
        "completion119s ->121s expires even within2s of cache refresh",
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    from anthropic_throttle_proxy import output_usage

    result = checks()
    result["passed"].extend(asyncio.run(cache_checks()))
    result["reader_sha256"] = hashlib.sha256(Path(output_usage.__file__).read_bytes()).hexdigest()
    print(json.dumps(result, indent=2))
