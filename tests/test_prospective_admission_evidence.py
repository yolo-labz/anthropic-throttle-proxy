"""Check the diagnostic artifact, not a claim that admission is implemented."""

import asyncio
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


async def test_prospective_spec_fails_only_at_the_six_measured_budget_contracts():
    root = ROOT
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "conftest",
        "-p",
        "no:cacheprovider",
        "--tb=short",
        "--color=no",
        "specs/244-prospective-admission/check_admission.py",
        cwd=root,
        env=os.environ | {"PYTHONPATH": str(root / "tests"), "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with asyncio.timeout(20):
            stdout, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    output = stdout.decode()
    assert process.returncode == 1, output
    assert re.search(r"(?m)^6 failed, 3 passed in [\d.]+s$", output), output
    failures = re.findall(r"(?m)^E +check_admission\.BudgetExceeded: (.+)$", output)
    assert Counter(failures) == {
        "output reservation: 120 > fictional budget 100": 3,
        "account concurrency: 4 > fictional budget 2": 1,
        "requests inside 60s: 4 > fictional budget 2": 1,
        "attempts including retry: 2 > fictional budget 1": 1,
    }, output
