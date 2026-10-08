"""Install the built wheel and check discovery in a fresh, isolated interpreter."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    wheels = sorted((root / "dist").glob("aelix_workflow-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"Expected one built wheel in dist/, found {len(wheels)}")

    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-deps",
            "--reinstall",
            str(wheels[0]),
        ],
        check=True,
    )
    # pytest's source-path setting must not hide a broken installed distribution.
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "from pathlib import Path; import sys; import aelix_workflow; "
            "location = Path(aelix_workflow.__file__).resolve(); "
            "assert location.is_relative_to(Path(sys.prefix).resolve()), location; "
            "print(f'Imported installed wheel: {location}')",
        ],
        check=True,
    )
    subprocess.run(
        [sys.executable, "-I", "-m", "aelix_coding_agent", "extension", "verify", "aelix-workflow"],
        cwd=root,
        check=True,
    )


if __name__ == "__main__":
    main()
