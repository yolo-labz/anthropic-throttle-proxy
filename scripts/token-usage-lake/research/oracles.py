"""Mechanical acceptance layers for an analysis run. Evidence, never a savings claim.

Four cheap layers, in increasing strength:

1. invariants  - declared row-level and relational rules over a verified generation.
2. replay      - a query re-run against the verified generation reproduces the same digest.
3. backtest    - a pre-registered selection rule and ONE holdout evaluation.
4. solver      - deferred until a constraint problem is declared; nothing here guesses one.

`verdict` refuses to call anything verified when a layer did not run. Missing evidence is
reported as "not_applicable", never as a pass, and an unmapped analysis stays unknown.
"""

# ruff: noqa: S608 - identifiers pass `identifier()` and are quoted; predicates and candidate
# expressions are the operator's own local spec file, never model or network input.

import hashlib
import json
import os
import re
from pathlib import Path

from snapshots import atomic_json

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BACKTEST_VERSION = 2
ADJUSTMENTS = ("holm-bonferroni", "bonferroni", "benjamini-hochberg")


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.match(value):
        raise ValueError(f"unsafe identifier: {value!r}")
    return value


def _cell(value):
    if value is None:
        return "\\N"
    if isinstance(value, float):
        return format(value, ".17g")
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def digest(connection, sql):
    """Order-insensitive canonical digest of a result set."""
    cursor = connection.execute(sql)
    columns = [column[0] for column in cursor.description]
    rows = sorted(tuple(_cell(value) for value in row) for row in cursor.fetchall())
    payload = "\n".join("\t".join(row) for row in rows)
    return {
        "sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "rows": len(rows),
        "columns": columns,
    }


def invariants(connection, rules, table=None):
    """Run declared rules. Any failure fails the layer; an empty rule set is not a pass."""
    results = []
    for rule in rules:
        kind = rule.get("kind")
        target = rule.get("table") or table
        if target is None:
            raise ValueError("rule has no table")
        identifier(target)
        if kind == "not_null":
            sql = f'SELECT count(*) FROM "{target}" WHERE "{identifier(rule["column"])}" IS NULL'
        elif kind == "unique":
            column = identifier(rule["column"])
            sql = (
                f'SELECT count(*) FROM (SELECT "{column}" FROM "{target}" '
                f"GROUP BY 1 HAVING count(*) > 1)"
            )
        elif kind == "in_range":
            column = identifier(rule["column"])
            low, high = rule.get("min"), rule.get("max")
            sql = (
                f'SELECT count(*) FROM "{target}" WHERE "{column}" IS NOT NULL '
                f'AND ({low if low is not None else "NULL"} > "{column}" '
                f'OR "{column}" > {high if high is not None else "NULL"})'
            )
        elif kind == "accepted_values":
            column = identifier(rule["column"])
            values = rule.get("values")
            if not isinstance(values, list) or not values:
                raise ValueError("accepted_values needs a non-empty values list")
            quoted = ", ".join("'" + str(value).replace("'", "''") + "'" for value in values)
            sql = f'SELECT count(*) FROM "{target}" WHERE "{column}" NOT IN ({quoted})'
        elif kind == "row_count_between":
            sql = f'SELECT count(*) FROM "{target}"'
        elif kind == "relationship":
            column = identifier(rule["column"])
            parent_column = identifier(rule["parent_column"])
            parent = identifier(rule["parent_table"])
            sql = (
                f'SELECT count(*) FROM "{target}" child WHERE child."{column}" IS NOT NULL '
                f'AND NOT EXISTS (SELECT 1 FROM "{parent}" parent '
                f'WHERE parent."{parent_column}" = child."{column}")'
            )
        elif kind == "custom_sql":
            sql = f'SELECT count(*) FROM "{target}" WHERE NOT ({rule["predicate"]})'
        else:
            raise ValueError(f"unknown rule kind: {kind!r}")
        failures = connection.execute(sql).fetchone()[0]
        if kind == "row_count_between":
            low, high = rule.get("min", 0), rule.get("max")
            passed = failures >= low and (high is None or failures <= high)
        else:
            passed = failures == 0
        results.append({"kind": kind, "table": target, "passed": passed, "violations": failures})
    status = "fail" if any(not result["passed"] for result in results) else "pass"
    if not rules:
        status = "not_applicable"
    return {"layer": "invariants", "status": status, "rules": results}


def replay(connection, sql, expected_sha256=None, label=None):
    """Re-execute a query and compare its canonical digest with the recorded one."""
    observed = digest(connection, sql)
    if expected_sha256 is None:
        return {
            "layer": "replay",
            "status": "not_applicable",
            "label": label,
            "observed": observed,
            "reason": "no recorded digest to reproduce",
        }
    status = "pass" if observed["sha256"] == expected_sha256 else "fail"
    return {
        "layer": "replay",
        "status": status,
        "label": label,
        "observed": observed,
        "expected_sha256": expected_sha256,
    }


def _validate_backtest(spec):
    identifier(spec["table"])
    identifier(spec["time_column"])
    validation, holdout = spec["validation"], spec["holdout"]
    if not (validation[0] < validation[1] <= holdout[0] < holdout[1]):
        raise ValueError("validation must precede a disjoint holdout window")
    if not spec.get("candidates"):
        raise ValueError("no candidates declared")
    if spec.get("select") not in ("max", "min"):
        raise ValueError("select must be max or min")
    if not isinstance(spec.get("trial_count"), int) or spec["trial_count"] < 1:
        raise ValueError("declare trial_count: every candidate that entered the selection rule")
    adjustment = spec.get("adjustment")
    if adjustment is not None and adjustment not in ADJUSTMENTS:
        raise ValueError(f"unknown adjustment: {adjustment!r}")
    for candidate in spec["candidates"]:
        if not isinstance(candidate.get("name"), str) or not candidate.get("expression"):
            raise ValueError("candidate needs name and expression")


def _trial_ledger(path, spec_sha256, trial_count):
    """Count every specification that entered the selection rule, including abandoned ones.

    Silent iteration is the failure mode this catches: a second run that still claims one trial
    is refused, so the denominator cannot shrink after seeing validation results.
    """
    observed = 0
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                observed += 1
    if observed + 1 > trial_count:
        raise ValueError(
            f"trial ledger records {observed} prior trial(s); declare trial_count >= {observed + 1}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"spec_sha256": spec_sha256, "trial": observed + 1}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return observed + 1


def backtest(connection, spec, receipt_path, epoch=None):
    """Select on validation only, then read the holdout exactly once.

    The receipt is written before the result is returned and a second call with the same
    specification is refused, because a reused holdout is no longer a holdout.
    """
    _validate_backtest(spec)
    materialized = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    spec_sha256 = hashlib.sha256(materialized.encode()).hexdigest()
    path = Path(receipt_path)
    if path.exists():
        previous = json.loads(path.read_text())
        if previous.get("spec_sha256") == spec_sha256:
            raise RuntimeError("holdout already used for this specification")
    trials = path.with_suffix(".trials.jsonl")
    trial = _trial_ledger(trials, spec_sha256, spec["trial_count"])
    multiplicity = (
        "single_pre_registered" if spec["trial_count"] == 1 else spec.get("adjustment", "none")
    )
    if spec["trial_count"] > 1 and not spec.get("adjustment"):
        multiplicity = "unadjusted"
    low, high = spec["validation"]
    column = f'"{spec["time_column"]}"'
    table = f'"{spec["table"]}"'
    scored = []
    for candidate in spec["candidates"]:
        sql = (
            f"SELECT {candidate['expression']} FROM {table} "
            f"WHERE {column} >= {low} AND {column} < {high}"
        )
        scored.append(
            {"name": candidate["name"], "validation": connection.execute(sql).fetchone()[0]}
        )
    ranked = sorted(
        scored,
        key=lambda row: (row["validation"] is None, row["validation"], row["name"]),
        reverse=spec["select"] == "max",
    )
    chosen = ranked[0]
    if chosen["validation"] is None:
        raise ValueError("selection produced no measurable value; refusing to read the holdout")
    expression = next(
        candidate["expression"]
        for candidate in spec["candidates"]
        if candidate["name"] == chosen["name"]
    )
    low, high = spec["holdout"]
    holdout_sql = f"SELECT {expression} FROM {table} WHERE {column} >= {low} AND {column} < {high}"
    holdout_value = connection.execute(holdout_sql).fetchone()[0]
    receipt = {
        "version": BACKTEST_VERSION,
        "spec_sha256": spec_sha256,
        "spec": spec,
        "chosen": chosen["name"],
        "validation": chosen["validation"],
        "holdout": holdout_value,
        "holdout_used_once": True,
        "trial": trial,
        "trial_count": spec["trial_count"],
        "multiplicity": multiplicity,
        "epoch": epoch,
        "note": (
            "exploratory: one holdout read, trial_count declared and ledgered, no realized-savings "
            "claim"
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, receipt)
    return {
        "layer": "backtest",
        "status": "pass" if holdout_value is not None else "not_applicable",
        "chosen": chosen["name"],
        "validation": chosen["validation"],
        "holdout": holdout_value,
        "trial_count": spec["trial_count"],
        "multiplicity": multiplicity,
        "receipt": str(path),
    }


# ponytail: one holdout read, no sequential design, and the adjustment is a declared method name
# rather than a computed adjusted p-value (this layer reports point estimates, not p-values).
# Ceiling: a single pre-registered comparison per specification, with the trial count proven by
# the ledger. Upgrade: alpha spending or a sequential test when a second family is needed.
def verdict(layers, verification=None):
    """Aggregate layer outcomes without ever inflating what was checked.

    `verification` is a serving receipt: the strongest label requires a DEEP receipt, so a cheap
    cached revalidation can never promote a result to `fully_verified`.
    """
    statuses = {layer["layer"]: layer["status"] for layer in layers}
    failed = [name for name, status in statuses.items() if status == "fail"]
    passed = [name for name, status in statuses.items() if status == "pass"]
    skipped = [name for name, status in statuses.items() if status == "not_applicable"]
    unadjusted = [layer["layer"] for layer in layers if layer.get("multiplicity") == "unadjusted"]
    deep = (verification or {}).get("mode") == "deep"
    accepted = not failed and bool(passed)
    return {
        "layers": statuses,
        "accepted_by_mechanical_evidence": accepted,
        "fully_verified": accepted and not skipped and not unadjusted and deep,
        "failed": failed,
        "passed": passed,
        "not_applicable": skipped,
        "unadjusted": unadjusted,
        "deep_verification": deep,
        "realized_savings": None,
        "note": (
            "accepted means at least one independent layer passed and none failed; "
            "fully_verified additionally requires every layer to have run, no unadjusted "
            "multiplicity, and a deep verification receipt"
        ),
    }
