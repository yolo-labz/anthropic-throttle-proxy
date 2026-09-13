"""Read-only desktop delivery check; unit tests/PR CI are not activation proof."""

import argparse
import json
import os
from datetime import UTC, datetime
from html import escape
from http.client import HTTPConnection
from math import isfinite
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--port", type=int, default=8765)
parser.add_argument(
    "--report",
    type=Path,
    default=Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    / "throttle-lanes.json",
)
parser.add_argument("--label", default="Codex C — Pro")
parser.add_argument("--icon", default="⚡")
args = parser.parse_args()

# Exceptions intentionally fail the process: unreadable/unknown is not success.
report = json.loads(args.report.read_text(encoding="utf-8"))
connection = HTTPConnection("127.0.0.1", args.port, timeout=5)
try:
    connection.request("GET", "/ui/stats")
    response = connection.getresponse()
    if response.status != 200:
        raise RuntimeError("Dashboard did not return HTTP 200")
    html = response.read().decode("utf-8")
finally:
    connection.close()
observed = datetime.fromisoformat(report["generatedAt"].replace("Z", "+00:00"))
age = (datetime.now(UTC) - observed).total_seconds()
lane = next((row for row in report["lanes"] if row.get("id") == "codex:c"), {})
checks = {
    "report_fresh": 0 <= age <= 2 * report["intervalSeconds"],
    "codex_c_measured": lane.get("kind") == "codex"
    and lane.get("status") != "unknown"
    and any(
        type(m.get("usedPercent")) in (int, float) and isfinite(m["usedPercent"])
        for m in lane.get("meters", [])
    ),
    "codex_c_rendered": "codex:c" in html,
    "configured_label_rendered": escape(args.label) in html,
    "configured_icon_rendered": escape(args.icon) in html,
}
print(json.dumps(checks, ensure_ascii=False))
raise SystemExit(0 if all(checks.values()) else 1)
