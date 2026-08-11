#!/usr/bin/env python3
"""Create a macOS DMG installer for the PyInstaller app bundle."""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
from pathlib import Path


APP_NAME = "DanmuQueue"
ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a macOS DMG for DanmuQueue.")
    parser.add_argument(
        "--app",
        default=str(ROOT / "dist" / f"{APP_NAME}.app"),
        help="Path to the .app bundle. Defaults to dist/DanmuQueue.app.",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "dist" / f"{APP_NAME}-macOS-{platform.machine()}.dmg"),
        help="Output DMG path.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app_path = Path(args.app).resolve()
    output = Path(args.output).resolve()
    staging = ROOT / "build" / "dmg" / APP_NAME

    if platform.system() != "Darwin":
        print("DMG packaging must be run on macOS.")
        return 1
    if not app_path.exists():
        print(f"Missing app bundle: {app_path}")
        print("Run: python scripts/build_desktop.py")
        return 1

    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    shutil.copytree(app_path, staging / f"{APP_NAME}.app", symlinks=True)
    applications_link = staging / "Applications"
    if applications_link.exists() or applications_link.is_symlink():
        applications_link.unlink()
    applications_link.symlink_to("/Applications", target_is_directory=True)

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    subprocess.check_call(
        [
            "hdiutil",
            "create",
            "-volname",
            APP_NAME,
            "-srcfolder",
            str(staging),
            "-ov",
            "-format",
            "UDZO",
            str(output),
        ],
        cwd=ROOT,
    )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
