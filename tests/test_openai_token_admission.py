"""Run the multi-process API admission oracle without credentials or network."""

import os
import subprocess
from pathlib import Path


def test_cross_worker_openai_token_admission(tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "NODE_NO_WARNINGS": "1"}
    capability = subprocess.run(
        [
            "/usr/bin/env",
            "node",
            "--input-type=module",
            "-e",
            "import {DatabaseSync} from 'node:sqlite'",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert capability.returncode == 0, "OpenAI admission tests require Node with node:sqlite"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "/usr/bin/env",
            "node",
            "--experimental-strip-types",
            "--test-reporter=tap",
            "clients/openai-token-admission/admission.test.mjs",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# pass 28" in result.stdout, result.stdout
    assert "# skipped 0" in result.stdout, result.stdout
