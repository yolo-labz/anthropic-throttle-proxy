"""Read-only scoped receipt; run with python meta/2683-verify-runtime.py."""

import datetime
import hashlib
import json
import pathlib
import re
import shlex
import subprocess
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
SOURCE = pathlib.Path(
    "/home/notroot/Documents/Code/yolo-labz/anthropic-throttle-proxy-324-ui-runtime-delivery"
)
REV = "45b7d0ef3321cca9c6d4e8513686efa30d889c1a"
PKG = "/nix/store/95ll5i712vsrzri85hsv675ljjr09kk2-anthropic-throttle-proxy-0.1.0"
HOME = pathlib.Path.home()
UNIT = "anthropic-throttle-proxy.service"
BASE = HOME / ".config/systemd/user" / UNIT
BEFORE = json.loads((ROOT / "2683-before-runtime.json").read_text())


def command(*args):
    # All argv are constructed below; no CLI, report or network value is executed.
    if args[0] not in {"git", "systemctl", "nix-store"}:
        raise ValueError("unsupported read-only receipt command")
    return subprocess.check_output(args, text=True)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fetch(path):
    with urllib.request.urlopen("http://127.0.0.1:8765" + path, timeout=3) as response:
        return response.read()


assert all(
    digest(pathlib.Path(row["path"]).read_bytes()) == row["sha256"]
    for row in BEFORE["persisted_chain"]
), "inherited unit/drop-in changed"
units = command("systemctl", "--user", "cat", UNIT)
effective = command(
    "systemctl",
    "--user",
    "show",
    UNIT,
    "-p",
    "ExecStart",
    "-p",
    "FragmentPath",
    "-p",
    "DropInPaths",
    "-p",
    "MainPID",
    "-p",
    "ActiveState",
    "-p",
    "SubState",
    "-p",
    "ExecMainStartTimestamp",
)
assert PKG + "/bin/anthropic-throttle-proxy" in effective
assert units.rsplit("ExecStart=", 1)[1].splitlines()[0] == PKG + "/bin/anthropic-throttle-proxy"
health = json.loads(fetch("/__throttle/health"))
assert health["build"].startswith(PKG + "/")
assert health["upstream"] == "http://127.0.0.1:1" and health["central_url"] == ""
html = fetch("/ui").decode()
for label in ("Desktop subscription", "MiMo Desktop", "weekly", "mimo:desktop-subscription"):
    assert label in html, label
report_path = HOME / ".local/state/anthropic-throttle-proxy/mimo-desktop.json"
report = json.loads(report_path.read_text())
now = datetime.datetime.now(datetime.UTC)
age = (
    now - datetime.datetime.fromisoformat(report["generatedAt"].replace("Z", "+00:00"))
).total_seconds()
assert 0 <= age < 600 and report["lanes"][0]["id"] == "mimo:desktop-subscription"
assert report_path.stat().st_mode & 0o777 == 0o600
row = re.search(r'<tr id="subscription-mimo:desktop-subscription".*?</tr>', html, re.S).group(0)
meter = report["lanes"][0]["meters"][0]
# Consumer intentionally uses :g; an integral 86% is not rendered as 86.0%.
assert f"{meter['remainingPercent']:g}%" in row, "consumer/producer sample differs"

pids = {
    unit: command("systemctl", "--user", "show", unit, "-p", "MainPID", "--value").strip()
    for unit in BEFORE["lane_pids"]
}
changed = {
    unit: {"before": BEFORE["lane_pids"][unit], "after": pid}
    for unit, pid in pids.items()
    if pid != BEFORE["lane_pids"][unit]
}
# Preserve, not waive: the bridge's recorded systemd automatic restart is outside this slice.
assert set(changed) <= {"mimo-desktop-subscription.service"}, changed
pid = command("systemctl", "--user", "show", UNIT, "-p", "MainPID", "--value").strip()
# The admitted control unit hides host PIDs; retain native read metadata instead.
proc_receipt = json.loads((ROOT / "2683-proc-cmdline.json").read_text())
assert proc_receipt["pid"] == pid
cmdline = " ".join(proc_receipt["argv"])
assert PKG in cmdline
safe_names = {
    "THROTTLE_MIMO_DESKTOP_REPORT",
    "THROTTLE_MIMO_REPORT",
    "THROTTLE_UPSTREAM",
    "THROTTLE_CENTRAL_URL",
    "THROTTLE_AUTH_PROBE",
    "THROTTLE_CREDENTIAL_RECHECK",
}
environment = {}
for entry in shlex.split(
    command("systemctl", "--user", "show", UNIT, "-p", "Environment", "--value")
):
    key, _, value = entry.partition("=")
    if key in safe_names:
        environment[key] = value
assert environment["THROTTLE_MIMO_DESKTOP_REPORT"] == str(report_path)
assert environment.get("THROTTLE_MIMO_REPORT") != str(report_path)

module = pathlib.Path(health["build"])
paths = [
    module / "desktop_report.py",
    module / "lanes.py",
    *(module / "ui").rglob("*.py"),
    *(module / "ui/templates").rglob("*.html"),
    module / "ui/static/style.css",
]
fingerprints = []
for path in sorted(paths):
    relative = "src/anthropic_throttle_proxy/" + str(path.relative_to(module))
    exact = command("git", "-C", str(SOURCE), "show", f"{REV}:{relative}").encode()
    sha = digest(path.read_bytes())
    assert digest(exact) == sha, relative
    fingerprints.append({"sourcePath": relative, "sha256": sha})
css_url = re.search(r'href="(/ui/static/style.css[^" ]*)"', html).group(1)
css_sha = digest(fetch(css_url))
assert css_sha == digest((module / "ui/static/style.css").read_bytes())
chain = [
    {"path": str(path), "target": str(path.resolve()), "sha256": digest(path.read_bytes())}
    for path in [BASE, *sorted((BASE.parent / (BASE.name + ".d")).glob("*.conf"))]
]
roots = {
    name: command("nix-store", "--query", "--roots", path).splitlines()
    for name, path in {
        "newPackage": PKG,
        "rollbackPackage": (
            "/nix/store/jmak9s5hhv33bp53npypcnlkcrjn2cxz-anthropic-throttle-proxy-0.1.0"
        ),
        "override": str(
            (BASE.parent / (BASE.name + ".d") / "99-ui-runtime-2026-10-07.conf").resolve()
        ),
        "producerUnit": str((BASE.parent / "mimo-desktop-quota-report.service").resolve()),
    }.items()
}
assert all(roots.values())
receipt = {
    "recordedAt": now.isoformat(),
    "sourceRevision": REV,
    "filteredHash": "sha256-wpx/L5yiv2vo9rNndRIcvur58MYp7Rkke4LivnWXJrY=",
    "persistedChain": chain,
    "effective": effective,
    "cmdlineNativeRead": proc_receipt,
    "selectedSystemdEnvironment": environment,
    "health": {
        key: health.get(key)
        for key in ("build", "upstream", "central_url", "inflight", "served", "queue_mode")
    },
    "report": report,
    "reportAgeSeconds": round(age, 2),
    "reportMode": "0600",
    "lanePids": pids,
    "unrelatedPidDiscontinuity": changed,
    "pidDiscontinuityEvidence": (
        "2683-desktop-pid-change.json: systemd scheduled restart 19:08:30 "
        "after UI activation 19:07:13; cause not established, "
        "no bridge restart issued by runtime seat."
    ),
    "producer": command(
        "systemctl",
        "--user",
        "show",
        "mimo-desktop-quota-report.service",
        "-p",
        "ExecStart",
        "-p",
        "ExecMainStartTimestamp",
        "-p",
        "ExecMainExitTimestamp",
        "-p",
        "ExecMainStatus",
        "-p",
        "Result",
        "-p",
        "CPUQuotaPerSecUSec",
        "-p",
        "MemoryMax",
        "-p",
        "TasksMax",
        "-p",
        "ProtectHome",
        "-p",
        "StateDirectory",
    ),
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
        "-p",
        "NextElapseUSecMonotonic",
    ),
    "gcRoots": roots,
    "sourceFingerprints": fingerprints,
    "uiHtmlSha256": digest(html.encode()),
    "servedCssSha256": css_sha,
    "actualUiAssertions": [
        "independent Desktop weekly row agrees with producer remainingPercent",
        "distinct monthly/weekly report environment",
        "served CSS agrees with final immutable source",
    ],
    "browserAcceptance": "Separate pW receipt required; not claimed by this script.",
}
(ROOT / "2683-runtime-activated-verification.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(
    f"PASS: persisted/effective/imported/cmdline and {len(fingerprints)} source files match {REV}"
)
print(
    f"PASS: actual /ui matches {meter['remainingPercent']}% weekly remaining; sample {age:.1f}s old"
)
print(
    "PASS: rollback/active roots and closed upstream retained; Token Plan/Z.AI/shim PIDs unchanged"
)
print("Desktop PID discontinuity retained:", changed)
