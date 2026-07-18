#!/usr/bin/env python3
"""Build a distributable desktop app with PyInstaller."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_BUILD_NAME = "GestionnaireCatalogue"
BUNDLE_ID = "com.efashion.gestionnaire-catalogue"


def _python() -> Path:
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python
    venv_python_win = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_python_win.exists():
        return venv_python_win
    return Path(sys.executable)


def _run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)


def _artifact_path() -> Path:
    if sys.platform == "darwin":
        app_path = ROOT / "dist" / f"{APP_BUILD_NAME}.app"
        if app_path.exists():
            return app_path
    return ROOT / "dist" / APP_BUILD_NAME


def main() -> int:
    python = _python()
    print(f"Python : {python}")

    _run([str(python), "-m", "pip", "install", "-r", "requirements-build.txt"])

    assets_dir = ROOT / "assets"
    icon_png = assets_dir / "app_icon.png"
    icon_icns = assets_dir / "app_icon.icns"
    add_data_sep = ";" if sys.platform == "win32" else ":"

    cmd = [
        str(python),
        "-m",
        "PyInstaller",
        "--name",
        APP_BUILD_NAME,
        "--windowed",
        "--onedir",
        "--noconfirm",
        "--clean",
        "--noupx",
        "--collect-all",
        "PySide6",
        "main.py",
    ]
    if assets_dir.exists():
        cmd.extend(["--add-data", f"{assets_dir}{add_data_sep}assets"])
    if sys.platform == "darwin":
        cmd.extend(["--osx-bundle-identifier", BUNDLE_ID])
        if icon_icns.exists():
            cmd.extend(["--icon", str(icon_icns)])
    elif icon_png.exists():
        cmd.extend(["--icon", str(icon_png)])

    _run(cmd)

    artifact = _artifact_path()
    size_mb = sum(f.stat().st_size for f in artifact.rglob("*") if f.is_file()) / (1024 * 1024)
    print()
    print("Build terminé.")
    print(f"Artefact : {artifact}")
    print(f"Taille   : {size_mb:.0f} Mo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
