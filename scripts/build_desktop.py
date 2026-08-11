#!/usr/bin/env python3
"""Build DanmuQueue as a desktop app with PyInstaller."""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
import os


APP_NAME = "DanmuQueue"
ROOT = Path(__file__).resolve().parents[1]


def data_arg(source: Path, target: str) -> str:
    separator = ";" if platform.system() == "Windows" else ":"
    return f"{source}{separator}{target}"


def icon_arg() -> list[str]:
    system = platform.system()
    if system == "Darwin":
        icon = ROOT / "packaging" / "icon.icns"
    elif system == "Windows":
        icon = ROOT / "packaging" / "icon.ico"
    else:
        icon = ROOT / "packaging" / "icon.png"
    return ["--icon", str(icon)] if icon.exists() else []


def main() -> int:
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(ROOT / ".pyinstaller-cache"))
    try:
        import PyInstaller.__main__  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Run: .venv/bin/python -m pip install pyinstaller")
        return 1

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--add-data",
        data_arg(ROOT / "web", "web"),
        "--collect-submodules",
        "websockets",
        *icon_arg(),
        str(ROOT / "app.py"),
    ]
    print(" ".join(command))
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
