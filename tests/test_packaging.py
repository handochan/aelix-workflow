"""Check the built distribution rather than inferring packaging from configuration."""

from __future__ import annotations

import subprocess
import zipfile
from email.parser import Parser
from pathlib import Path

from aelix_agent_core.contracts.manifest import parse_manifest_toml


def test_built_wheel_contains_bound_manifest_and_entry_point(tmp_path):
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        manifest = parse_manifest_toml(archive.read("aelix_workflow/aelix-plugin.toml").decode())
        assert manifest.plugin.id == "aelix-workflow"
        assert manifest.plugin.version == "0.2.0"
        assert manifest.entry.python == "aelix_workflow:setup"
        endpoints = next(name for name in archive.namelist() if name.endswith("entry_points.txt"))
        assert "aelix-workflow = aelix_workflow:setup" in archive.read(endpoints).decode()
        metadata = archive.read(
            next(n for n in archive.namelist() if n.endswith("/METADATA"))
        ).decode()
        assert Parser().parsestr(metadata)["Version"] == manifest.plugin.version
        requirements = Parser().parsestr(metadata).get_all("Requires-Dist", [])
        assert requirements
        assert all("file:///" not in req and "../../aelix-ai" not in req for req in requirements)
        assert "aelix_workflow/py.typed" in archive.namelist()
