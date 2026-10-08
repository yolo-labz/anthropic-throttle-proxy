"""Read-only actual UI/journal verification; never patch the running collector."""

import argparse
import ast
import datetime
import hashlib
import html
import importlib
import itertools
import json
import pathlib
import re
import shlex
import site
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = pathlib.Path(__file__).resolve().parent
PKG = pathlib.Path("/nix/store/939kr6a14jdl692yn675lih7n2b12d42-anthropic-throttle-proxy-0.1.0")
UNIT = "anthropic-throttle-proxy.service"
BEFORE = json.loads((ROOT / "2683-combined-before-2317.json").read_text())
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--output", type=pathlib.Path, default=ROOT / "2683-combined-runtime-verification.json"
)
options = parser.parse_args()


def command(*args):
    units = {
        UNIT,
        "mimo-desktop-subscription.service",
        "mimo-throttle-proxy.service",
        "zai-throttle-proxy.service",
        "mimo-tokenplan-shim.service",
        "mimo-desktop-quota-report.timer",
    }
    properties = {
        "ExecStart",
        "MainPID",
        "FragmentPath",
        "DropInPaths",
        "ActiveState",
        "SubState",
        "ExecMainStartTimestamp",
        "Environment",
        "UnitFileState",
        "LastTriggerUSec",
    }
    if args == ("systemctl", "--user", "cat", UNIT):
        pass
    elif len(args) >= 6 and args[:3] == ("systemctl", "--user", "show") and args[3] in units:
        remaining = args[4:]
        if remaining[-1:] == ("--value",):
            remaining = remaining[:-1]
        if len(remaining) % 2 or any(
            remaining[i] != "-p" or remaining[i + 1] not in properties
            for i in range(0, len(remaining), 2)
        ):
            raise ValueError("unsupported unit property")
    else:
        raise ValueError("unsupported read-only receipt command")
    # No shell, user-provided executable, lifecycle verb or unconstrained arguments.
    return subprocess.check_output(args, text=True, timeout=5)


# Reuse the deployed package's immutable dependency list, not a borrowed editable venv.
wrapper = (PKG / "bin/.anthropic-throttle-proxy-wrapped").read_text()
for node in ast.walk(ast.parse(wrapper)):
    if isinstance(node, ast.List):
        paths = ast.literal_eval(node)
        if paths and all(isinstance(p, str) and p.startswith("/nix/store/") for p in paths):
            for path in paths:
                site.addsitedir(path)
sys.path.insert(0, str(PKG / "lib/python3.14/site-packages"))
output_usage = importlib.import_module("anthropic_throttle_proxy.output_usage")
signals = importlib.import_module("anthropic_throttle_proxy.ui.signals")

assert pathlib.Path(output_usage.__file__).is_relative_to(PKG)
assert pathlib.Path(signals.__file__).is_relative_to(PKG)

# Fetch actual served fragments only once; keep only gauge-safe data, not account DOM.
views = []
for source in ("local", "mimo", "zai"):
    begin = time.time()
    with urlopen("http://127.0.0.1:8765/ui/stats?source=" + source, timeout=3) as response:
        body = response.read().decode()
    end = time.time()
    panel = re.search(r'<section class="tps-panel".*?</section>', body, re.S).group(0)
    assert 'data-seen="true"' in panel, "actual combined reading is unknown/stale"
    assert "Combined output throughput" in panel
    assert "Partial coverage" in panel and "non-Pi / other hosts unmeasured" in panel
    assert "accounted at completion" in panel and "exact output / 60 wall-clock seconds" in panel
    number = re.search(r'class="tps-num">([^<]+)', panel).group(1)
    peak = re.search(r"10s peak <b>([^<]+)", panel).group(1)
    scale = re.search(r"· scale ([0-9]+) tokens/s", panel).group(1)
    points = html.unescape(re.search(r'<polyline points="([^"]+)"', panel).group(1))
    providers = int(re.search(r"([0-9]+) provider labels in window", panel).group(1))
    signature = (number, peak, scale, points, providers)
    views.append(
        {"source": source, "requestStart": begin, "requestEnd": end, "signature": signature}
    )
assert len({v["signature"] for v in views}) == 1, (
    "requests crossed a cache refresh; no same-cache assertion"
)

journal = pathlib.Path.home() / ".local/state/pi-harness/usage.jsonl"
data, identity = output_usage._read_tail(journal, output_usage.MAX_READ_BYTES)
read_at = time.time()
assert data.endswith(b"\n")
records = [output_usage._completion(line, read_at) for line in data.splitlines()]
low = min(v["requestStart"] for v in views) - output_usage.CACHE_STALE_S
high = max(v["requestEnd"] for v in views)
assert min(stamp for stamp, _, _ in records) <= low - output_usage.WINDOW_S

# The UI does not export sampled_at. Reconstruct every distinct possible SAME
# trailing60s bucket partition in its accepted cache-age interval. This is exact
# integer-output arithmetic over real journal events, not estimated tokens or a
# comparison to a differently-timed checkpoint. Never claim an unexposed microsecond.
boundaries = {low, high}
for stamp, _, _ in records:
    for offset in range(0, output_usage.WINDOW_S + 1, 10):
        point = stamp + offset
        if low < point < high:
            boundaries.add(point)
boundaries = sorted(boundaries)
matches = []
for left, right in itertools.pairwise(boundaries):
    sampled_at = (left + right) / 2
    counts = [0] * 6
    providers = set()
    for stamp, tokens, provider in records:
        if sampled_at - output_usage.WINDOW_S <= stamp < sampled_at:
            counts[min(5, int((stamp - (sampled_at - 60)) / 10))] += tokens
            providers.add(provider)
    gauge = signals.tps_gauge([[count, 0] for count in counts])._replace(value=sum(counts) / 60)
    signature = (
        f"{gauge.value:.0f}",
        f"{gauge.peak:.0f}",
        f"{gauge.scale:.0f}",
        gauge.spark.points,
        len(providers),
    )
    if signature == views[0]["signature"]:
        matches.append(
            {
                "sampledAtLowerInclusive": left,
                "sampledAtUpperExclusive": right,
                "windowSeconds": 60,
                "reportedOutputTokens": sum(counts),
                "exactTokensPerWallSecond": sum(counts) / 60,
                "tenSecondOutputBuckets": counts,
                "providerLabelCount": len(providers),
            }
        )
assert matches, "actual served gauge has no matching common60s journal window"

unit_text = command("systemctl", "--user", "cat", UNIT)
effective = command(
    "systemctl",
    "--user",
    "show",
    UNIT,
    "-p",
    "ExecStart",
    "-p",
    "MainPID",
    "-p",
    "FragmentPath",
    "-p",
    "DropInPaths",
    "-p",
    "ActiveState",
    "-p",
    "SubState",
    "-p",
    "ExecMainStartTimestamp",
)
assert unit_text.rsplit("ExecStart=", 1)[1].splitlines()[0] == str(
    PKG / "bin/anthropic-throttle-proxy"
)
assert str(PKG / "bin/anthropic-throttle-proxy") in effective
with urlopen("http://127.0.0.1:8765/__throttle/health", timeout=3) as response:
    health = json.load(response)
assert health["build"].startswith(str(PKG) + "/")
assert health["upstream"] == "http://127.0.0.1:1" and health["central_url"] == ""
pids = {
    unit: command("systemctl", "--user", "show", unit, "-p", "MainPID", "--value").strip()
    for unit in BEFORE["currentPidBaseline"]
}
assert all(pids[u] == pid for u, pid in BEFORE["currentPidBaseline"].items() if u != UNIT)
for row in BEFORE["persistedChain"]:
    path = pathlib.Path(row["path"])
    assert str(path.resolve()) == row["target"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]

allowed = {
    "THROTTLE_PI_USAGE_PATH",
    "THROTTLE_MIMO_DESKTOP_REPORT",
    "THROTTLE_MIMO_REPORT",
    "THROTTLE_UPSTREAM",
    "THROTTLE_CENTRAL_URL",
}
environment = {}
for entry in shlex.split(
    command("systemctl", "--user", "show", UNIT, "-p", "Environment", "--value")
):
    key, _, value = entry.partition("=")
    if key in allowed:
        environment[key] = value
assert environment["THROTTLE_PI_USAGE_PATH"] == str(journal)
report_path = pathlib.Path(environment["THROTTLE_MIMO_DESKTOP_REPORT"])
assert environment["THROTTLE_MIMO_REPORT"] != str(report_path)
report = json.loads(report_path.read_text())
report_age = (
    time.time()
    - datetime.datetime.fromisoformat(report["generatedAt"].replace("Z", "+00:00")).timestamp()
)
assert 0 <= report_age < 600 and report_path.stat().st_mode & 0o777 == 0o600
assert report["lanes"][0]["id"] == "mimo:desktop-subscription"

with urlopen("http://127.0.0.1:8765/ui", timeout=3) as response:
    full_page = response.read().decode()
assert "mimo:desktop-subscription" in full_page and "Desktop subscription" in full_page
css_url = re.search(r'href="(/ui/static/style.css[^" ]*)"', full_page).group(1)
with urlopen("http://127.0.0.1:8765" + css_url, timeout=3) as response:
    css = response.read()
assert (
    hashlib.sha256(css).hexdigest()
    == hashlib.sha256(
        (
            PKG / "lib/python3.14/site-packages/anthropic_throttle_proxy/ui/static/style.css"
        ).read_bytes()
    ).hexdigest()
)

receipt = {
    "recordedAt": datetime.datetime.now(datetime.UTC).isoformat(),
    "acceptedRevision": "c4bd3ce6f203220683e18504a629343d5d53b12b",
    "package": str(PKG),
    "effective": effective,
    "health": {
        key: health.get(key)
        for key in ("build", "upstream", "central_url", "inflight", "queue_mode")
    },
    "selectedSystemdEnvironment": environment,
    "currentPids": pids,
    "allSiblingPidsPreserved": True,
    "inheritedUnitAndQuotaBytesPreserved": True,
    "servedSourceChoices": views,
    "sameCachedGaugeAcrossChoices": True,
    "matchingSameWindowIntervals": matches,
    "windowBinding": (
        "Actual sampled_at is not exported. Exact real-output numerator, peak, scale, "
        "six-bucket spark and provider count match these possible cached60s intervals; "
        "no claim of an unexposed precise timestamp."
    ),
    "journal": {
        "path": str(journal),
        "device": identity[0],
        "inode": identity[1],
        "boundedBytes": len(data),
    },
    "quota": {
        "generatedAt": report["generatedAt"],
        "ageSeconds": round(report_age, 2),
        "mode": "0600",
        "status": report["lanes"][0]["status"],
    },
    "timer": command(
        "systemctl",
        "--user",
        "show",
        "mimo-desktop-quota-report.timer",
        "-p",
        "ActiveState",
        "-p",
        "UnitFileState",
        "-p",
        "LastTriggerUSec",
    ),
    "servedCssSha256": hashlib.sha256(css).hexdigest(),
    "browserAcceptance": "Independent admitted pW packet; no browser launched by this receipt.",
    "privacy": (
        "No raw journal, costs, seats, user/account DOM or credentials retained; "
        "no inference or running collector mutation."
    ),
}
options.output.write_text(json.dumps(receipt, indent=2) + "\n")
print("PASS actual combined cached gauge:", views[0]["signature"][0], "tokens/s")
print(
    "PASS exact common60s output/bucket/geometry/provider arithmetic:",
    len(matches),
    "matching time intervals",
)
print("PASS persisted/effective/imported/served CSS, quota and every current sibling PID")
