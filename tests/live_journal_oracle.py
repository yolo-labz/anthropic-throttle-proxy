"""Read-only real journal arithmetic; only aggregates leave this module.

The independent parser never calls the production parser. The exact native-reader
sample and the UI's unexposed cached endpoint are deliberately separate proofs.
"""

import asyncio
import bisect
import json
import math
import os
import shlex
from datetime import datetime
from pathlib import Path

CAP = 2 * 1024 * 1024
NATIVE_SAMPLE = """
import json,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from anthropic_throttle_proxy import output_usage
s = output_usage.read_usage(Path(sys.argv[2]))
print(json.dumps({'sampled_at':s.sampled_at,'seen':s.gauge.seen,'stale':s.gauge.stale,
 'value':s.gauge.value,'window_s':s.gauge.window_s,'output_tokens':s.output_tokens,
 'providers':s.providers,'identity':s.identity,'reason':s.reason}))
"""


def events(path):
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        data = os.pread(stream.fileno(), CAP, max(0, before.st_size - CAP))
        if before.st_size > CAP:
            data = data.partition(b"\n")[2]
        after = os.fstat(stream.fileno())
    current = path.stat()
    signatures = {(s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) for s in (before, after, current)}
    assert len(signatures) == 1, "journal changed during independent read; unverified"
    assert data.endswith(b"\n"), "incomplete live journal; unverified"
    rows = []
    for raw in data.splitlines():
        row = json.loads(raw)
        date = datetime.fromisoformat(row["ts"])
        assert date.tzinfo is not None
        stamp, count, provider = date.timestamp(), row["output"], row["provider"]
        assert math.isfinite(stamp) and type(count) is int and 0 <= count <= 2**53
        assert isinstance(provider, str) and provider.strip()
        rows.append((stamp, count, provider))
    assert rows, "empty live journal; unverified"
    return sorted(rows), (before.st_dev, before.st_ino), len(data)


def window(rows, end):
    selected = [r for r in rows if end - 60 <= r[0] < end]
    return sum(r[1] for r in selected), sorted({r[2] for r in selected}), len(selected)


def journal_runtime(root, environment_text, exec_start):
    # Host PIDs are deliberately not visible in the admitted namespace. Bind to
    # effective systemd ExecStart + immutable wrapper, never join/bypass that namespace.
    package = root.parents[3]
    executable = package / "bin/anthropic-throttle-proxy"
    assert f"path={executable} ;" in exec_start
    environment = dict(v.split("=", 1) for v in shlex.split(environment_text) if "=" in v)
    # Never serialize the environment: only journal path routing is selected.
    home = Path(environment.get("HOME", Path.home()))
    state = Path(environment.get("PI_USAGE_STATE_DIR", home / ".local/state/pi-harness"))
    journal = Path(environment.get("THROTTLE_PI_USAGE_PATH", state / "usage.jsonl")).expanduser()
    wrapped = package / "bin/.anthropic-throttle-proxy-wrapped"
    assert str(wrapped) in executable.read_text()
    interpreter = Path(wrapped.read_text().splitlines()[0].removeprefix("#!").strip())
    assert interpreter.is_relative_to("/nix/store") and interpreter.name.startswith("python3")
    return journal, interpreter


async def command_output(*arguments):
    """Bounded owned subprocess; no shell or admission wait."""
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode()
    return stdout


async def native_read(root):
    command = (
        "/run/current-system/sw/bin/systemctl",
        "--user",
        "show",
        "anthropic-throttle-proxy.service",
        "--value",
        "-p",
    )
    environment = (await command_output(*command, "Environment")).decode()
    exec_start = (await command_output(*command, "ExecStart")).decode()
    journal, interpreter = await asyncio.to_thread(journal_runtime, root, environment, exec_start)
    native = json.loads(
        await command_output(
            str(interpreter), "-I", "-c", NATIVE_SAMPLE, str(root.parent), str(journal)
        )
    )
    assert native["seen"] and not native["stale"], native["reason"]
    assert native["window_s"] == 60
    rows, identity, read_bytes = await asyncio.to_thread(events, journal)
    end = native["sampled_at"]
    assert rows[0][0] <= end - 60, "bounded reference tail lacks common window"
    total, providers, count = window(rows, end)
    assert list(identity) == native["identity"], "live source replaced between checks"
    assert total == native["output_tokens"] and total / 60 == native["value"]
    assert providers == native["providers"]
    return {
        "scope": "real native journal / deployed reader interpreter; not synthetic inference",
        "sampled_at": end,
        "window_s": 60,
        "output_tokens": total,
        "output_tokens_per_second": total / 60,
        "completion_events": count,
        "observed_provider_labels": providers,
        "read_bytes": read_bytes,
        "source_identity_matches": True,
        "runtime_interpreter": str(interpreter),
        "interpreter_binding": "systemd ExecStart + immutable wrapper; no host namespace join",
        "coverage": "Pi on this host only; non-Pi/other-host unmeasured",
    }, journal


def compatible_rates(rows, low, high):
    """All possible fixed60s sums between two endpoint bounds, without estimation."""
    assert high >= low and rows[0][0] <= low - 60
    stamps = [r[0] for r in rows]
    prefix = [0]
    for _, count, _ in rows:
        prefix.append(prefix[-1] + count)
    ends = {low, high}
    for stamp in stamps:
        for edge in (stamp, stamp + 60):
            if low <= edge <= high:
                ends.add(edge)
                after = math.nextafter(edge, math.inf)
                if after <= high:
                    ends.add(after)
    return {
        format(
            (prefix[bisect.bisect_left(stamps, end)] - prefix[bisect.bisect_left(stamps, end - 60)])
            / 60,
            ".0f",
        )
        for end in ends
    }
