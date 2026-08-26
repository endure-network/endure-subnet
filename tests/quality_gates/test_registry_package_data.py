from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Final

PINNED_UV_CACHE_HOME: Final = Path(
    os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
)


def test_setuptools_declares_registry_package_data() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert (
        '[tool.setuptools.package-data]\n"endure.protocol" = ["activated_versions.json"]'
        in pyproject
    )


def test_built_artifacts_contain_tracked_registry_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    artifacts = tmp_path / "artifacts"
    source.mkdir()
    artifacts.mkdir()
    shutil.copytree("endure", source / "endure")
    for filename in ("LICENSE", "README.md", "pyproject.toml"):
        shutil.copy2(filename, source / filename)
    pinned_requirement = next(
        line.rstrip(" \\")
        for line in Path("docker/uv-bootstrap-requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("uv==")
    )
    uv_version = pinned_requirement.removeprefix("uv==")
    uv = Path(
        os.environ.get(
            "ENDURE_UV",
            PINNED_UV_CACHE_HOME / "endure" / f"uv-{uv_version}" / "bin" / "uv",
        )
    )
    assert uv.is_file(), f"pinned uv {uv_version} is missing; run make bootstrap"

    subprocess.run(
        [
            str(uv),
            "build",
            "--wheel",
            "--sdist",
            "--out-dir",
            str(artifacts),
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    tracked = Path("endure/protocol/activated_versions.json").read_bytes()
    wheel = next(artifacts.glob("*.whl"))
    sdist = next(artifacts.glob("*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_registry = archive.read("endure/protocol/activated_versions.json")
    with tarfile.open(sdist, "r:gz") as archive:
        member = next(
            name
            for name in archive.getnames()
            if name.endswith("/endure/protocol/activated_versions.json")
        )
        extracted = archive.extractfile(member)
        assert extracted is not None
        sdist_registry = extracted.read()

    assert wheel_registry == tracked
    assert sdist_registry == tracked
