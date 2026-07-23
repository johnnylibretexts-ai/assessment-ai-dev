from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPUTE_PACKAGES = {"sympy", "pint", "ucumvert"}


def test_networked_app_imports_without_loading_compute_dependencies() -> None:
    script = f"""
import builtins
import sys

sys.path.insert(0, {str(ROOT)!r})
attempted = []
original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".", 1)[0]
    if root in {sorted(COMPUTE_PACKAGES)!r}:
        attempted.append(name)
        raise ImportError("compute-only dependency blocked by isolation test")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
import app.schemas
import app.main
import app.computation
import evaluation.cli

assert attempted == [], attempted
assert not (set({sorted(COMPUTE_PACKAGES)!r}) & set(sys.modules))
assert "app.computation_runtime" not in sys.modules
assert app.computation._sympy is None
assert app.computation._pint is None
assert app.computation._ucumvert is None
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_compute_dependencies_are_sidecar_only_package_extras() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    base_dependencies = {
        dependency.split("[", 1)[0].split("=", 1)[0].split("<", 1)[0].casefold()
        for dependency in project["project"]["dependencies"]
    }
    compute_dependencies = {
        dependency.split("=", 1)[0].casefold()
        for dependency in project["project"]["optional-dependencies"]["compute"]
    }
    app_dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compute_dockerfile = (ROOT / "Dockerfile.compute").read_text(encoding="utf-8")

    assert not (COMPUTE_PACKAGES & base_dependencies)
    assert compute_dependencies == COMPUTE_PACKAGES
    assert "pip install ." in app_dockerfile
    assert ".[compute]" not in app_dockerfile
    assert "'.[compute]'" in compute_dockerfile
