#!/usr/bin/env python3
"""Build a distributable desktop app with PyInstaller."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_BUILD_NAME = "GestionnaireCatalogue"
BUNDLE_ID = "com.efashion.gestionnaire-catalogue"

# Modules Qt réellement utilisés par catalog_import/gui.py
PYSIDE6_IMPORTS = (
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
)

# Sécurité : exclure les addons Qt jamais importés (WebEngine, 3D, QML…)
PYSIDE6_EXCLUDES = (
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtUiTools",
)


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


def _prune_pyside6_bundle(app_path: Path) -> None:
    """Retire binaires Qt inutilisés encore copiés par PyInstaller."""
    if not app_path.is_dir():
        return

    prune_names = {
        "qmlls",
        "qmlformat",
        "qmlcachegen",
        "lupdate",
        "lrelease",
        "Assistant__dot__app",
        "Linguist__dot__app",
        "Designer__dot__app",
    }
    for root in (app_path / "Contents" / "Frameworks", app_path / "Contents" / "Resources"):
        pyside_dir = root / "PySide6"
        if not pyside_dir.is_dir():
            continue
        for name in prune_names:
            target = pyside_dir / name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        qml_dir = pyside_dir / "Qt" / "qml"
        if qml_dir.is_dir():
            shutil.rmtree(qml_dir)


def _cleanup_macos_artifacts() -> None:
    """PyInstaller laisse un dossier onedir en doublon du .app sur macOS."""
    if sys.platform != "darwin":
        return
    onedir = ROOT / "dist" / APP_BUILD_NAME
    app_path = ROOT / "dist" / f"{APP_BUILD_NAME}.app"
    if onedir.is_dir() and app_path.is_dir():
        shutil.rmtree(onedir)


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
        "main.py",
    ]
    for module in PYSIDE6_IMPORTS:
        cmd.extend(["--hidden-import", module])
    for module in PYSIDE6_EXCLUDES:
        cmd.extend(["--exclude-module", module])
    # Les hooks PyInstaller de QtWidgets collectent les plugins nécessaires (cocoa, styles…).
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
    if sys.platform == "darwin" and artifact.suffix == ".app":
        _prune_pyside6_bundle(artifact)
    _cleanup_macos_artifacts()

    size_mb = sum(
        f.stat().st_size
        for f in artifact.rglob("*")
        if f.is_file() and not f.is_symlink()
    ) / (1024 * 1024)
    print()
    print("Build terminé.")
    print(f"Artefact : {artifact}")
    print(f"Taille   : {size_mb:.0f} Mo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
