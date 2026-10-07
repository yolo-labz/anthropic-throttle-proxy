"""Planted report failures must be red; none may manufacture a successful receipt."""

import json
import runpy
import time
from pathlib import Path

import pytest

CHECKER = runpy.run_path(str(Path(__file__).parents[1] / "scripts/check-sonar-report.py"))


@pytest.fixture
def coverage_case(tmp_path):
    source = tmp_path / "module.py"
    source.write_text("value = 1\n")
    report = tmp_path / "coverage.xml"
    xml = (
        f'<coverage timestamp="{int(time.time() * 1000)}" lines-valid="1" '
        'lines-covered="1" line-rate="1"><sources><source>.</source></sources>'
        '<packages><package><classes><class filename="module.py"><lines>'
        '<line number="1" hits="1"/></lines></class></classes></package></packages></coverage>'
    )
    report.write_text(xml)
    return report, tmp_path, {source}, int(time.time()) - 1, xml


def test_complete_report(coverage_case):
    report, root, inventory, start, _ = coverage_case
    assert CHECKER["inspect_coverage"](report, root, inventory, start) == {
        "python_files": 1,
        "lines_valid": 1,
        "lines_covered": 1,
    }


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('lines-valid="1"', 'lines-valid="0"'),
        ('lines-covered="1"', 'lines-covered="0"'),
        ('line-rate="1"', 'line-rate="0"'),
        ('line-rate="1"', 'line-rate="nan"'),
        ('filename="module.py"', 'filename="absent.py"'),
        ('number="1"', 'number="2"'),
        ('hits="1"', 'hits="-1"'),
        ('hits="1"', 'hits="0"'),
        ("<source>.</source>", "<source>/outside</source>"),
        ("<sources><source>.</source></sources>", "<sources/>"),
        ("<classes>", '<classes><class filename="module.py"/>'),
        ("<coverage ", "<not-coverage "),
        ('<line number="1" hits="1"/>', ""),
        ("<packages>", "<packages"),
        ("<coverage ", '<!DOCTYPE coverage [<!ENTITY x "secret">]><coverage '),
    ],
)
def test_bad_report_is_red(coverage_case, before, after):
    report, root, inventory, start, xml = coverage_case
    report.write_text(xml.replace(before, after))
    with pytest.raises((ValueError, CHECKER["ExpatError"])):
        CHECKER["inspect_coverage"](report, root, inventory, start)


@pytest.mark.parametrize("rate", ["0.6667", "0.6668"])
def test_coverage_py_four_significant_digits(coverage_case, rate):
    report, root, inventory, start, xml = coverage_case
    (root / "module.py").write_text("a = 1\nb = 2\nc = 3\n")
    xml = xml.replace('lines-valid="1"', 'lines-valid="3"')
    xml = xml.replace('lines-covered="1"', 'lines-covered="2"')
    xml = xml.replace('line-rate="1"', f'line-rate="{rate}"')
    xml = xml.replace(
        '<line number="1" hits="1"/>',
        '<line number="1" hits="1"/><line number="2" hits="1"/><line number="3" hits="0"/>',
    )
    report.write_text(xml)
    if rate == "0.6668":
        with pytest.raises(ValueError, match="rate"):
            CHECKER["inspect_coverage"](report, root, inventory, start)
    else:
        assert CHECKER["inspect_coverage"](report, root, inventory, start)["lines_covered"] == 2


def test_stale_report_is_red(coverage_case):
    report, root, inventory, start, _ = coverage_case
    with pytest.raises(ValueError, match="stale"):
        CHECKER["inspect_coverage"](report, root, inventory, start + 60)


def test_missing_python_file_is_red(coverage_case):
    report, root, inventory, start, _ = coverage_case
    with pytest.raises(ValueError, match="missing"):
        CHECKER["inspect_coverage"](report, root, inventory | {root / "other.py"}, start)


@pytest.mark.parametrize("failure", ["missing", "stale", "revision", "dirty"])
def test_cli_failure_does_not_emit_receipt(coverage_case, monkeypatch, failure):
    report, root, _, start, _ = coverage_case
    (root / "sonar-project.properties").write_text("sonar.sources=.\n")
    output = root / "receipt.json"
    if failure == "missing":
        report.unlink()
    if failure == "stale":
        start += 60

    monkeypatch.setitem(
        CHECKER["check"].__globals__,
        "git_data",
        lambda: (root, "a" * 40, " M module.py" if failure == "dirty" else "", ["module.py"]),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "checker",
            "--report",
            str(report),
            "--revision",
            "b" * 40 if failure == "revision" else "a" * 40,
            "--not-before",
            str(start),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as result:
        CHECKER["main"]()
    assert result.value.code == 1
    assert not output.exists()


def test_cli_success_emits_hashes(coverage_case, monkeypatch):
    report, root, _, start, _ = coverage_case
    (root / "sonar-project.properties").write_text("# sources\nsonar.sources=.\n")
    output = root / "receipt.json"

    monkeypatch.setitem(
        CHECKER["check"].__globals__, "git_data", lambda: (root, "a" * 40, "", ["module.py"])
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "checker",
            "--report",
            str(report),
            "--revision",
            "a" * 40,
            "--not-before",
            str(start),
            "--output",
            str(output),
        ],
    )
    CHECKER["main"]()
    receipt = json.loads(output.read_text())
    assert receipt["revision"] == "a" * 40
    assert len(receipt["coverage_sha256"]) == 64
    assert len(receipt["source_sha256"]["module.py"]) == 64
