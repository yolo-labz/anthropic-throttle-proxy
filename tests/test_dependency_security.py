"""Concrete dependency/build regressions; no scanner suppressions or stress loops."""

import re
import sys
import tomllib
from pathlib import Path

import pytest
from multidict import CIMultiDict, MultiDict
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("factory", [CIMultiDict, MultiDict])
@pytest.mark.parametrize("operation", ["reflected_union", "subtraction"])
def test_multidict_items_view_does_not_retain_operand_values(factory, operation):
    value = object()
    operand = [("untrusted-header", value)]
    view = factory({"existing-header": "existing-value"}).items()
    before = sys.getrefcount(value)
    result = operand | view if operation == "reflected_union" else view - operand
    del result
    assert sys.getrefcount(value) == before


def test_locked_multidict_is_outside_affected_range():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    package = next(p for p in lock["package"] if p["name"] == "multidict")
    assert Version(package["version"]) >= Version("6.9.1")


def test_docker_external_images_are_digest_pinned():
    external = [
        line.split()[1] if line.startswith("FROM ") else line.split("--from=", 1)[1].split()[0]
        for line in (ROOT / "Dockerfile").read_text().splitlines()
        if line.startswith("FROM ") or line.startswith("COPY --from=")
    ]
    external.extend(
        line.split("=", 1)[1]
        for line in (ROOT / "Dockerfile").read_text().splitlines()
        if line.startswith("# syntax=")
    )
    for image in external:
        if image != "builder":
            assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", image), image
