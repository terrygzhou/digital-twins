"""Automated PyPI build check (T013).

Builds a wheel once per suite run (PEP 517, via `python3 -m build --wheel`)
and asserts the artifact + release-timefile side-car files exist. The
`pip install` + `pip show MIT` round-trip is credential-gated and lives in
the runbook (docs/release-runbook.md, section 4/5) as a manual step; it is
deliberately NOT exercised here so this suite stays host-neutral and does
not need a venv.

Host-neutral: no host paths, no credentials, no `python3.N` pin. The
version is read from the single-sourced `digital_twins.__version__`
(AGENTS.md: version is single-sourced in `digital_twins/__init__.py`).
"""

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import digital_twins

REPO_ROOT = Path(__file__).resolve().parents[2]
WHEEL_NAME_RE = re.compile(r"^digital_twins-\d+\.\d+\.\d+-py3-none-any\.whl$")


@pytest.fixture(scope="module")
def wheel_path(tmp_path_factory):
    """Build the wheel once for the module and return its Path.

    Uses `python3 -m build --wheel` so the build runs through the PEP 517
    backend declared in pyproject.toml (hatchling) rather than a hardcoded
    tool. Output lands in a pytest-managed tmp dir so the build is
    reproducible and never touches the repo's `dist/` directory.
    """
    out_dir = tmp_path_factory.mktemp("pypi-wheel")
    cmd = [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)]
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"wheel build failed (exit {proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    wheels = sorted(out_dir.glob("digital_twins-*-py3-none-any.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def test_wheel_builds_and_name_matches(wheel_path):
    """The wheel file exists and follows the expected name pattern."""
    assert wheel_path.is_file(), f"wheel not found at {wheel_path}"
    assert WHEEL_NAME_RE.match(wheel_path.name), (
        f"wheel name {wheel_path.name!r} does not match "
        f"{'digital_twins-<v>-py3-none-any.whl'}"
    )
    # The embedded version must be the single-sourced package version.
    expected = f"digital_twins-{digital_twins.__version__}-py3-none-any.whl"
    assert wheel_path.name == expected, (
        f"wheel name {wheel_path.name!r} != expected {expected!r} "
        f"(digital_twins.__version__ = {digital_twins.__version__!r})"
    )


def test_wheel_is_a_valid_zip(wheel_path):
    """The wheel is a well-formed ZIP (PEP 427 containers are ZIP archives)."""
    assert zipfile.is_zipfile(wheel_path), f"{wheel_path} is not a valid ZIP"
    with zipfile.ZipFile(wheel_path) as zf:
        bad = zf.testzip()
        assert bad is None, f"corrupt member in wheel: {bad!r}"
        # PEP 378: a wheel must carry its .dist-info with METADATA + WHEEL.
        names = zf.namelist()
        assert any(n.endswith(".dist-info/METADATA") for n in names), (
            "wheel is missing its .dist-info/METADATA"
        )
        assert any(n.endswith(".dist-info/WHEEL") for n in names), (
            "wheel is missing its .dist-info/WHEEL"
        )


def test_license_exists_at_repo_root():
    """LICENSE ships at the repo root (PyPI requires license metadata)."""
    license_file = REPO_ROOT / "LICENSE"
    assert license_file.is_file(), f"LICENSE not found at repo root: {license_file}"
    text = license_file.read_text(encoding="utf-8")
    assert "MIT" in text, "LICENSE does not appear to be an MIT license"


def test_release_runbook_exists():
    """docs/release-runbook.md documents the manual release steps.

    T013 is RED until T014 writes this file (Test-First: RED before GREEN).
    """
    runbook = REPO_ROOT / "docs" / "release-runbook.md"
    assert runbook.is_file(), f"release runbook not found: {runbook}"
