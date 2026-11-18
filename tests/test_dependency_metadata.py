"""Verify the metadata used to resolve pinned spaCy models."""

import json
import subprocess
import tomllib
from importlib.metadata import metadata
from pathlib import Path
from urllib.parse import urldefrag

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
MODELS = (
    "en-core-web-sm",
    "es-core-news-sm",
    "fr-core-news-sm",
    "ru-core-news-sm",
    "uk-core-news-sm",
    "xx-ent-wiki-sm",
)


@pytest.mark.parametrize("name", MODELS)
def test_spacy_wheel_metadata_and_hash_match_lock(name: str) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    override = next(
        item
        for item in project["tool"]["uv"]["dependency-metadata"]
        if item["name"] == name
    )
    package = next(item for item in lock["package"] if item["name"] == name)
    requirement = next(
        req
        for req in map(Requirement, project["project"]["dependencies"])
        if req.name == name
    )
    installed = metadata(name)

    assert override["version"] == package["version"] == installed["Version"]
    assert {str(Requirement(spec)) for spec in override.get("requires-dist", [])} == {
        str(Requirement(spec)) for spec in installed.get_all("Requires-Dist") or []
    }
    assert override.get("requires-python") == installed.get("Requires-Python")
    assert set(override.get("provides-extra", [])) == set(
        installed.get_all("Provides-Extra") or []
    )
    assert requirement.url is not None
    url, fragment = urldefrag(requirement.url)
    assert url == package["source"]["url"] == package["wheels"][0]["url"]
    assert fragment == package["wheels"][0]["hash"].replace(":", "=")


def test_spacy_model_can_be_locked_without_network_or_cache(tmp_path: Path) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirement = next(
        req
        for req in map(Requirement, project["project"]["dependencies"])
        if req.name == "en-core-web-sm"
    )
    override = next(
        item
        for item in project["tool"]["uv"]["dependency-metadata"]
        if item["name"] == requirement.name
    )
    assert override["requires-dist"] == []
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "model-lock-probe"\nversion = "0.0.0"\n'
        'requires-python = ">=3.12,<3.14"\n'
        f"dependencies = [{json.dumps(str(requirement))}]\n\n"
        "[tool.uv]\n"
        f'dependency-metadata = [{{ name = "{requirement.name}", '
        f'version = "{override["version"]}", requires-dist = [] }}]\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["uv", "lock", "--offline", "--no-cache", "--no-progress"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lock = tomllib.loads((tmp_path / "uv.lock").read_text(encoding="utf-8"))
    package = next(item for item in lock["package"] if item["name"] == requirement.name)
    assert requirement.url is not None
    _, fragment = urldefrag(requirement.url)
    assert fragment == package["wheels"][0]["hash"].replace(":", "=")


def test_model_download_uses_runtime_security_floors() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    constraints = {
        req.name: str(req)
        for req in map(Requirement, project["tool"]["uv"]["constraint-dependencies"])
    }
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    for package in ("certifi", "filelock"):
        assert f'--with "{constraints[package]}"' in dockerfile
