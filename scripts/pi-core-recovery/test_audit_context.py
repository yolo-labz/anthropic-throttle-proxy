import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "audit_context", Path(__file__).with_name("audit_context.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def audit(tmp_path):
    path = tmp_path / "session.jsonl"
    entries = [
        dict(type="session", id="session-id"),
        dict(type="message", id="work", parentId=None),
        dict(type="custom", id="detached", parentId=None),
    ]
    original = ("\n".join(map(json.dumps, entries)) + "\n").encode()
    path.write_bytes(original)
    baseline = dict(transcript=str(path), rawEntries=3, sha256=hashlib.sha256(original).hexdigest())
    identity = dict(
        native=dict(session=str(path)), contextDetached=True, herdrSession="home", pane="w1:p1"
    )
    return path, original, baseline, identity, {("home", "w1:p1"): "work"}


def append(path, entry):
    with path.open("a") as output:
        output.write(json.dumps(entry) + "\n")


def test_header_id_is_not_a_branch_entry_and_restored_work_is_proven(audit):
    path, _, baseline, identity, targets = audit
    append(
        path,
        dict(type="custom", id="proof", parentId="work", customType="pi-context-branch-recovery"),
    )
    assert module.check(baseline, identity, targets)["originalWorkMessages"] == 1


@pytest.mark.parametrize(
    "failure", ["missing_work", "missing_audit", "dangling", "duplicate", "prefix"]
)
def test_near_miss_cannot_claim_original_context(audit, failure):
    path, original, baseline, identity, targets = audit
    entry = dict(
        type="custom", id="proof", parentId="work", customType="pi-context-branch-recovery"
    )
    if failure == "missing_work":
        entry["parentId"] = "detached"
    elif failure == "missing_audit":
        entry["customType"] = "unrelated"
    elif failure == "dangling":
        entry["parentId"] = "missing"
    elif failure == "duplicate":
        entry["id"] = "work"
    elif failure == "prefix":
        path.write_bytes(original.replace(b"session-id", b"changed-id"))
    append(path, entry)
    with pytest.raises(ValueError):
        module.check(baseline, identity, targets)
