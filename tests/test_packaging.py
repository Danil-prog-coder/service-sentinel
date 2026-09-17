"""requirements*.txt must stay in sync with pyproject.toml (the source of truth)."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_requirements(name: str) -> list[str]:
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    specs = []
    for line in lines:
        text = line.split("#", 1)[0].strip()
        if text and not text.startswith("-"):  # skip "-r requirements.txt"
            specs.append(text)
    return specs


def pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_runtime_requirements_match_pyproject() -> None:
    assert read_requirements("requirements.txt") == pyproject()["project"]["dependencies"]


def test_dev_requirements_match_pyproject() -> None:
    dev = pyproject()["project"]["optional-dependencies"]["dev"]
    assert read_requirements("requirements-dev.txt") == dev


def test_dev_requirements_include_runtime_ones() -> None:
    assert "-r requirements.txt" in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
