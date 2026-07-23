from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
OPT_IN_ENV = "ASSESSMENT_COMPUTATION_RUN_DOCKER_QUALIFICATION"


@pytest.mark.skipif(
    os.environ.get(OPT_IN_ENV) != "1",
    reason=f"set {OPT_IN_ENV}=1 to build and qualify the real Docker runtime",
)
def test_real_computation_container_hardening() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/qualify_computation_container.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=720,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["schema_version"] == (
        "assessment-computation-container-qualification-v0"
    )
    assert report["passed"] is True
    assert report["container_runtime"]["network_mode"] == "none"
    assert report["container_runtime"]["read_only"] is True
    assert report["service_security"]["no_new_privileges"] == 1
    assert report["service_security"]["egress_errno"] != 0
    assert report["timeout_recovery"]["timeout_status"] == 504
    assert report["timeout_recovery"]["recovery_status"] == 200
    assert report["cross_request_isolation"]["no_surviving_request_workers"] is True
    assert len(report["cleaned_resources"]) == 3
