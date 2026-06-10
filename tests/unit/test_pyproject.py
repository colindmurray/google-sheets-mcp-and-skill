"""Contract tests for ``pyproject.toml`` (build unit ``pyproject``).

DESIGN §9 (LOCKED packaging contract) pins the project metadata that the rest of
the repo and both install paths depend on. These tests parse the real
``pyproject.toml`` with ``tomllib`` (no network, no mock service — this unit is
pure packaging metadata) and assert the LOCKED values exactly:

- distribution name ``google-sheets-mcp-and-skill`` / import package ``gsheets`` (src layout)
- the TWO console scripts: ``gsheets`` -> ``gsheets.cli:main`` and
  ``google-sheets-mcp`` -> ``gsheets.mcp_server:main``
- ``requires-python >= 3.11`` (NOT 3.10 — §9 floor)
- the exact runtime dependency floors (google-api-python-client, google-auth,
  google-auth-oauthlib, google-auth-httplib2, fastmcp>=2.11,<3, pydantic>=2)
  and the explicit ABSENCE of the deprecated ``oauth2client``
- dev extras include pytest>=8 + pytest-cov
- pytest ini declares the ``live`` marker (§10 live-integration gate)
- the wheel packages the src-layout ``src/gsheets`` package
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

# Repo root is two levels up from tests/unit/.
PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


@pytest.fixture(scope="module")
def pyproject() -> dict:
    with PYPROJECT_PATH.open("rb") as fh:
        return tomllib.load(fh)


@pytest.fixture(scope="module")
def project(pyproject: dict) -> dict:
    return pyproject["project"]


# --------------------------------------------------------------------------- #
# Identity: distribution name + import package (src layout)
# --------------------------------------------------------------------------- #


def test_pyproject_file_exists() -> None:
    assert PYPROJECT_PATH.is_file()


def test_pyproject_parses_as_valid_toml(pyproject: dict) -> None:
    # tomllib raising would fail the fixture; this asserts the top-level shape.
    assert "project" in pyproject
    assert "build-system" in pyproject


def test_distribution_name_is_locked(project: dict) -> None:
    assert project["name"] == "google-sheets-mcp-and-skill"


def test_version_is_pep440_string(project: dict) -> None:
    version = project["version"]
    assert isinstance(version, str)
    # Loose PEP 440-ish check: starts with a numeric release segment.
    assert re.match(r"^\d+\.\d+", version), version


# --------------------------------------------------------------------------- #
# Console scripts (the two install paths) — LOCKED §9
# --------------------------------------------------------------------------- #


def test_two_console_scripts_present(project: dict) -> None:
    scripts = project["scripts"]
    assert set(scripts) == {"gsheets", "google-sheets-mcp"}


def test_cli_console_script_target(project: dict) -> None:
    assert project["scripts"]["gsheets"] == "gsheets.cli:main"


def test_mcp_console_script_target(project: dict) -> None:
    # Flat module mcp_server.py (NOT mcp/server.py) per DESIGN §11 deviation note,
    # to avoid a local ``mcp/`` package shadowing the ``mcp`` PyPI dependency.
    assert project["scripts"]["google-sheets-mcp"] == "gsheets.mcp_server:main"


# --------------------------------------------------------------------------- #
# Python floor — LOCKED §9 (>=3.11, NOT 3.10)
# --------------------------------------------------------------------------- #


def test_requires_python_is_3_11_floor(project: dict) -> None:
    assert project["requires-python"] == ">=3.11"


def test_no_python_3_10_classifier(project: dict) -> None:
    classifiers = project.get("classifiers", [])
    assert "Programming Language :: Python :: 3.10" not in classifiers


def test_python_3_11_classifier_present(project: dict) -> None:
    classifiers = project.get("classifiers", [])
    assert "Programming Language :: Python :: 3.11" in classifiers


# --------------------------------------------------------------------------- #
# Runtime dependencies — exact floors, LOCKED §9
# --------------------------------------------------------------------------- #


def _dep_map(deps: list[str]) -> dict[str, str]:
    """Map package name -> the raw requirement string (lowercased name key)."""
    out: dict[str, str] = {}
    for raw in deps:
        # Split the leading distribution name off the version/marker spec.
        name = re.split(r"[<>=!~ ;\[]", raw, maxsplit=1)[0].strip().lower()
        out[name] = raw
    return out


REQUIRED_RUNTIME_DEPS = {
    "google-api-python-client": ">=2.190",
    "google-auth": ">=2.50",
    "google-auth-oauthlib": ">=1.3",
    "google-auth-httplib2": ">=0.2",
    "pydantic": ">=2",
}


def test_runtime_dependencies_present_with_floors(project: dict) -> None:
    deps = _dep_map(project["dependencies"])
    for name, floor in REQUIRED_RUNTIME_DEPS.items():
        assert name in deps, f"missing runtime dependency: {name}"
        assert floor in deps[name], f"{name} must declare floor {floor}; got {deps[name]!r}"


def test_fastmcp_pinned_to_v2_range(project: dict) -> None:
    deps = _dep_map(project["dependencies"])
    assert "fastmcp" in deps
    spec = deps["fastmcp"]
    # LOCKED: fastmcp>=2.11,<3 (both bounds present).
    assert ">=2.11" in spec, spec
    assert "<3" in spec, spec


def test_no_deprecated_oauth2client_dependency(project: dict) -> None:
    deps = _dep_map(project["dependencies"])
    assert "oauth2client" not in deps, "DESIGN §9: do NOT depend on deprecated oauth2client"


def test_cli_has_no_argparse_dependency(project: dict) -> None:
    """argparse is stdlib; it must never be declared as a runtime dep."""
    deps = _dep_map(project["dependencies"])
    assert "argparse" not in deps


# --------------------------------------------------------------------------- #
# Dev dependencies — PEP 735 dependency-groups (plain `uv sync` installs them)
# --------------------------------------------------------------------------- #


def test_dev_dependency_group_present(pyproject: dict) -> None:
    # PEP 735 [dependency-groups], NOT [project.optional-dependencies]: uv installs
    # dependency groups on a plain `uv sync`, so a fresh clone gets pytest without
    # needing to know about --extra flags.
    assert "dev" in pyproject.get("dependency-groups", {})
    assert "optional-dependencies" not in pyproject["project"]


def test_dev_dependency_group_includes_pytest_and_cov(pyproject: dict) -> None:
    dev = _dep_map(pyproject["dependency-groups"]["dev"])
    assert "pytest" in dev
    assert ">=8" in dev["pytest"], dev["pytest"]
    assert "pytest-cov" in dev


# --------------------------------------------------------------------------- #
# pytest ini — `live` marker (§9 / §10)
# --------------------------------------------------------------------------- #


def test_pytest_live_marker_declared(pyproject: dict) -> None:
    ini = pyproject["tool"]["pytest"]["ini_options"]
    markers = ini.get("markers", [])
    # Each marker entry is "<name>: <description>"; assert a `live` marker exists.
    names = {m.split(":", 1)[0].strip() for m in markers}
    assert "live" in names, markers


def test_pytest_testpaths_includes_tests(pyproject: dict) -> None:
    ini = pyproject["tool"]["pytest"]["ini_options"]
    assert "tests" in ini.get("testpaths", [])


# --------------------------------------------------------------------------- #
# Build backend + src-layout wheel packaging — LOCKED §9
# --------------------------------------------------------------------------- #


def test_build_system_declares_a_backend(pyproject: dict) -> None:
    build = pyproject["build-system"]
    assert build.get("build-backend")
    assert build.get("requires")


def test_wheel_packages_src_gsheets(pyproject: dict) -> None:
    """src layout: the wheel must ship the ``src/gsheets`` package.

    DESIGN §9 allows hatchling with ``[tool.hatch.build.targets.wheel]
    packages=["src/gsheets"]`` or the uv_build backend. Accept either, but require
    that the import package ``gsheets`` is sourced from ``src/``.
    """
    backend = pyproject["build-system"]["build-backend"]
    if "hatch" in backend:
        wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
        assert "src/gsheets" in wheel["packages"]
    else:
        # uv_build: src layout is the default; just assert the dir exists on disk.
        assert (PYPROJECT_PATH.parent / "src" / "gsheets").is_dir()


def test_src_gsheets_package_exists_on_disk() -> None:
    pkg = PYPROJECT_PATH.parent / "src" / "gsheets" / "__init__.py"
    assert pkg.is_file()
