"""Pure incident-snapshot falsifiers: never inspect a real process or transcript."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "incident_launch", Path(__file__).with_name("launch.py")
)
incident = importlib.util.module_from_spec(spec)
spec.loader.exec_module(incident)


def snapshot():
    entries = [
        {"id": f"w{n}", "parentId": f"w{n - 1}" if n else None, "type": "message"}
        for n in range(10)
    ]
    entries.append({"id": "74edcb64", "parentId": "w9", "type": "context_edit"})
    prefix = ("\n".join(map(json.dumps, entries)) + "\n").encode()
    suffix = [
        {
            "id": "c5582014",
            "parentId": "1fe7721e",
            "type": "custom",
            "customType": "pi-artifact-cleanup",
        },
        {
            "id": "a4bbb629",
            "parentId": "c5582014",
            "type": "custom",
            "customType": "pi-artifact-cleanup",
        },
    ]
    raw = prefix + ("\n".join(map(json.dumps, suffix)) + "\n").encode()
    evidence = {
        "originalBytes": len(prefix),
        "originalPrefixSha256": incident.sha(prefix),
        "originalBranchWorkIds": [f"w{n}" for n in range(9, -1, -1)],
    }
    return evidence, raw, prefix


def test_exact_snapshot_preserves_prefix():
    evidence, raw, prefix = snapshot()
    assert incident.original_snapshot(evidence, raw) == prefix


@pytest.mark.parametrize("mutation", ["prefix", "work", "suffix", "extra_work", "cycle"])
def test_snapshot_drift_refuses(mutation):
    evidence, raw, _ = snapshot()
    if mutation == "prefix":
        evidence["originalPrefixSha256"] = "0" * 64
    elif mutation == "work":
        evidence["originalBranchWorkIds"] = []
    elif mutation == "suffix":
        raw = raw.replace(b'"parentId": "1fe7721e"', b'"parentId": "other"')
    elif mutation == "extra_work":
        raw += b'{"id":"new","parentId":"a4bbb629","type":"message"}\n'
    else:
        prefix = raw[: evidence["originalBytes"]].replace(b'"parentId": null', b'"parentId": "w0"')
        raw = prefix + raw[evidence["originalBytes"] :]
        evidence["originalBytes"] = len(prefix)
        evidence["originalPrefixSha256"] = incident.sha(prefix)
    with pytest.raises(RuntimeError):
        incident.original_snapshot(evidence, raw)


def test_existing_equal_plan_is_never_rewritten(tmp_path):
    path = tmp_path / "plan.json"
    incident.durable_once(path, {"reviewed": True})
    before = path.stat().st_mtime_ns
    incident.durable_once(path, {"reviewed": True}, equal_ok=True)
    assert path.stat().st_mtime_ns == before
    with pytest.raises(RuntimeError):
        incident.durable_once(path, {"reviewed": False}, equal_ok=True)


def test_existing_intent_cannot_be_reused(tmp_path):
    path = tmp_path / "intent.json"
    incident.durable_once(path, {"dispatch": 1})
    with pytest.raises(RuntimeError):
        incident.durable_once(path, {"dispatch": 1})


def test_symlink_evidence_refused(tmp_path):
    target = tmp_path / "target.json"
    incident.durable_once(target, {"reviewed": True})
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(RuntimeError):
        incident.owned_json(link)


def test_repaired_parent_preserves_all_original_values():
    evidence, raw, prefix = snapshot()
    rows = [json.loads(line) for line in prefix.splitlines()]
    original = ("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n").encode()
    repaired = raw.replace(b'"parentId": "1fe7721e"', b'"parentId": null')
    proof = {
        **evidence,
        "externalParentRepair": True,
        "originalBytes": len(original),
        "originalPrefixSha256": incident.sha(original),
        "normalizedPrefixRows": len(rows),
        "normalizedPrefixSha256": incident.sha(prefix),
        "normalizedCompactIds": [row["id"] for row in rows if "id" in row],
    }
    assert incident.original_snapshot(proof, repaired) == prefix
    for changed in [
        repaired.replace(b'"parentId": null', b'"parentId": "other"', 1),
        repaired.replace(b'"id": "w0"', b'"id": "other"', 1),
    ]:
        with pytest.raises(RuntimeError):
            incident.original_snapshot(proof, changed)
    with pytest.raises(RuntimeError):
        incident.original_snapshot({**proof, "normalizedCompactIds": []}, repaired)
