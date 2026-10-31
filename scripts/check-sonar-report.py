"""Fail closed on stale, empty or unmatched Python coverage before Sonar publication."""

import argparse
import hashlib
import json
import math
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.parsers.expat import ExpatError, ParserCreate


def git_data() -> tuple[Path, str, str, list[str]]:
    # Static commands only; /bin/sh is available on both NixOS and the CI runner.
    root = subprocess.check_output(
        ["/bin/sh", "-c", "git rev-parse --show-toplevel"], text=True
    ).strip()
    head = subprocess.check_output(["/bin/sh", "-c", "git rev-parse HEAD"], text=True).strip()
    dirty = subprocess.check_output(
        ["/bin/sh", "-c", "git status --porcelain --untracked-files=no"], text=True
    ).strip()
    paths = subprocess.check_output(["/bin/sh", "-c", "git ls-files"], text=True).splitlines()
    return Path(root).resolve(), head, dirty, paths


def source_roots(root: Path) -> list[str]:
    properties = dict(
        line.split("=", 1)
        for line in (root / "sonar-project.properties").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    )
    return properties["sonar.sources"].split(",")


def class_coverage(
    entry: ET.Element, root: Path, sources: list[Path], expected: set[Path]
) -> tuple[Path, int, int]:
    filename = entry.attrib["filename"]
    candidates = {path / filename for path in [root, *sources]}
    matches = {path.resolve() for path in candidates} & expected
    if len(matches) != 1:
        raise ValueError(f"unmatched or ambiguous coverage filename: {filename}")
    path = matches.pop()
    line_limit = len(path.read_text().splitlines())
    numbers: set[int] = set()
    covered = 0
    for line in entry.findall("./lines/line"):
        number, hits = int(line.attrib["number"]), int(line.attrib["hits"])
        if not 0 < number <= line_limit or number in numbers or hits < 0:
            raise ValueError(f"invalid coverage line: {filename}:{number}")
        numbers.add(number)
        covered += hits > 0
    return path, len(numbers), covered


def inspect_coverage(
    report: Path, root: Path, expected: set[Path], not_before: int
) -> dict[str, int]:
    def reject_dtd(*_args: object) -> None:
        raise ValueError("DTDs are not allowed in coverage reports")

    builder = ET.TreeBuilder()
    parser = ParserCreate()
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = reject_dtd
    parser.Parse(report.read_bytes(), True)
    document = builder.close()
    if document.tag != "coverage":
        raise ValueError("not a coverage report")
    timestamp = int(document.attrib["timestamp"])
    if not not_before * 1000 <= timestamp <= (time.time() + 5) * 1000:
        raise ValueError("coverage timestamp is stale or in the future")
    sources = [
        (root / (node.text or "")).resolve() for node in document.findall("./sources/source")
    ]
    if not sources or any(not path.is_relative_to(root) for path in sources):
        raise ValueError("coverage sources missing or outside checkout")
    seen: set[Path] = set()
    valid = covered = 0
    for entry in document.findall("./packages/package/classes/class"):
        path, file_valid, file_covered = class_coverage(entry, root, sources, expected)
        if path in seen:
            raise ValueError(f"duplicate coverage filename: {path.relative_to(root)}")
        seen.add(path)
        valid += file_valid
        covered += file_covered
    if seen != expected:
        missing = sorted(str(path.relative_to(root)) for path in expected - seen)
        raise ValueError(f"Python files missing from coverage: {missing}")
    if not valid or not covered:
        raise ValueError("empty or zero-hit coverage report")
    if valid != int(document.attrib["lines-valid"]) or covered != int(
        document.attrib["lines-covered"]
    ):
        raise ValueError("coverage summary does not match line entries")
    rate = float(document.attrib["line-rate"])
    # coverage.py emits four significant digits, not an unrounded fraction.
    if not math.isfinite(rate) or rate != float(f"{covered / valid:.4g}"):
        raise ValueError("coverage rate does not match line entries")
    return {"python_files": len(seen), "lines_valid": valid, "lines_covered": covered}


def check(report: Path, revision: str, not_before: int) -> dict:
    root, head, dirty, tracked = git_data()
    if head != revision:
        raise ValueError("checkout revision does not match requested revision")
    if dirty:
        raise ValueError("tracked checkout is dirty")
    roots = source_roots(root)
    paths = [
        path
        for path in tracked
        if any(folder in (".", path) or Path(folder) in Path(path).parents for folder in roots)
    ]
    inventory = {root / path for path in paths if path.endswith(".py")}
    if not inventory:
        raise ValueError("no tracked Python scan sources")
    result = inspect_coverage(report, root, inventory, not_before)
    return {
        "revision": revision,
        "coverage_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "coverage": result,
        "source_sha256": {
            path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("coverage.xml"))
    parser.add_argument("--revision", required=True)
    parser.add_argument("--not-before", type=int, required=True, help="test start, Unix seconds")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = check(args.report, args.revision, args.not_before)
    except (OSError, ValueError, KeyError, ExpatError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Sonar report preflight FAILED: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Sonar report preflight OK: {receipt['revision']} {receipt['coverage']}")


if __name__ == "__main__":
    main()
