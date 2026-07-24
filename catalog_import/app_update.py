"""Vérification des releases GitHub + téléchargement / installation du binaire."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import (
    APP_VERSION,
    GITHUB_REPO,
    UPDATES_DIR,
    USER_AGENT,
)
from .http_client import NETWORK_ERRORS, create_http_session

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")
APP_BUILD_NAME = "GestionnaireCatalogue"


class AppUpdateError(Exception):
    """Erreur lors du check / téléchargement de mise à jour."""


@dataclass(frozen=True)
class AppRelease:
    tag_name: str
    version: str
    name: str
    html_url: str
    asset_name: str
    download_url: str
    size: int


def _parse_version(value: str) -> tuple[int, int, int]:
    text = (value or "").strip().lstrip("vV").lstrip(".")
    match = _VERSION_RE.search(text)
    if not match:
        return (0, 0, 0)
    major = int(match.group(1) or 0)
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    return (major, minor, patch)


def is_newer_version(remote: str, current: str = APP_VERSION) -> bool:
    return _parse_version(remote) > _parse_version(current)


def is_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_install_path() -> Path | None:
    """Chemin de l'app installée (.app macOS ou dossier onedir Windows)."""
    if not is_frozen_app():
        return None
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent
        return None
    return exe.parent


def _platform_asset_markers() -> tuple[str, ...]:
    if sys.platform == "darwin":
        return ("-mac.zip", "-macos.zip", "-darwin.zip")
    if sys.platform == "win32":
        return ("-windows.zip", "-win.zip", "-win64.zip")
    return ("-linux.zip", "-linux.tar.gz")


def _pick_asset(assets: list[dict]) -> dict | None:
    markers = _platform_asset_markers()
    normalized = []
    for asset in assets:
        name = str(asset.get("name") or "").strip()
        url = str(asset.get("browser_download_url") or "").strip()
        if not name or not url:
            continue
        normalized.append(asset)

    for marker in markers:
        for asset in normalized:
            if marker in str(asset.get("name") or "").lower():
                return asset

    zips = [
        a
        for a in normalized
        if str(a.get("name") or "").lower().endswith((".zip", ".exe", ".dmg"))
    ]
    if len(zips) == 1:
        return zips[0]
    return None


def _github_headers(*, download: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if download:
        headers["Accept"] = "application/octet-stream"
    else:
        headers["Accept"] = "application/vnd.github+json"

    token = (
        os.environ.get("CATALOG_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or ""
    ).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _state_path() -> Path:
    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    return UPDATES_DIR / "state.json"


def load_update_state() -> dict:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_update_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _remember_download(release: AppRelease, target: Path) -> None:
    state = load_update_state()
    state["downloaded_version"] = release.version
    state["downloaded_file"] = str(target)
    state["tag_name"] = release.tag_name
    save_update_state(state)


def fetch_latest_release() -> AppRelease | None:
    """Retourne la dernière release plus récente que APP_VERSION, sinon None."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        with create_http_session(
            timeout=(10.0, 30.0), headers=_github_headers()
        ) as client:
            response = client.get(url)
    except NETWORK_ERRORS as exc:
        raise AppUpdateError(f"Impossible de joindre GitHub : {exc}") from exc

    if response.status_code == 404:
        raise AppUpdateError(
            "Aucune release GitHub accessible "
            f"(repo {GITHUB_REPO} privé/inexistant, ou pas de release publiée)."
        )
    if response.status_code in {401, 403}:
        raise AppUpdateError(
            f"Accès GitHub refusé (HTTP {response.status_code}). "
            "Le dépôt est probablement privé."
        )
    if response.status_code >= 400:
        raise AppUpdateError(
            f"GitHub releases HTTP {response.status_code}: {response.text[:200]}"
        )

    payload = response.json()
    tag_name = str(payload.get("tag_name") or "").strip()
    version = tag_name.lstrip("vV").lstrip(".") or str(payload.get("name") or "").strip()
    if not version or not is_newer_version(version):
        return None

    asset = _pick_asset(list(payload.get("assets") or []))
    if asset is None:
        raise AppUpdateError(
            f"Aucun binaire adapté à cette plateforme dans la release {tag_name}. "
            "Attendu : GestionnaireCatalogue-Mac.zip ou -Windows.zip."
        )

    return AppRelease(
        tag_name=tag_name,
        version=version,
        name=str(payload.get("name") or tag_name),
        html_url=str(payload.get("html_url") or ""),
        asset_name=str(asset.get("name") or ""),
        download_url=str(asset.get("browser_download_url") or ""),
        size=int(asset.get("size") or 0),
    )


def download_release(release: AppRelease) -> Path:
    """Télécharge l'asset dans UPDATES_DIR et retourne le chemin local."""
    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    target = UPDATES_DIR / release.asset_name
    if target.exists() and release.size > 0 and target.stat().st_size == release.size:
        _remember_download(release, target)
        return target

    try:
        with create_http_session(
            timeout=(10.0, 300.0), headers=_github_headers(download=True)
        ) as client:
            response = client.get(release.download_url)
    except NETWORK_ERRORS as exc:
        raise AppUpdateError(f"Téléchargement impossible : {exc}") from exc

    if response.status_code >= 400:
        raise AppUpdateError(
            f"Téléchargement HTTP {response.status_code} pour {release.asset_name}"
        )

    tmp = target.with_suffix(target.suffix + ".partial")
    tmp.write_bytes(response.content)
    tmp.replace(target)
    _remember_download(release, target)
    return target


def check_and_download_update() -> tuple[AppRelease, Path] | None:
    """
    Check latest release.
    Si plus récente : télécharge (ou réutilise le fichier local) et retourne (release, path).
    """
    release = fetch_latest_release()
    if release is None:
        return None
    path = download_release(release)
    return release, path


def _find_payload(extract_dir: Path) -> Path:
    """Trouve le .app (macOS) ou le dossier onedir (Windows) dans l'archive extraite."""
    if sys.platform == "darwin":
        apps = [p for p in extract_dir.rglob("*.app") if p.is_dir() and p.suffix == ".app"]
        if apps:
            apps.sort(key=lambda p: len(p.parts))
            return apps[0]
        raise AppUpdateError("Archive Mac invalide : aucun .app trouvé.")

    for exe in extract_dir.rglob(f"{APP_BUILD_NAME}.exe"):
        if exe.is_file():
            return exe.parent
    candidates = [
        p
        for p in extract_dir.rglob(APP_BUILD_NAME)
        if p.is_dir() and p.name == APP_BUILD_NAME
    ]
    if candidates:
        candidates.sort(key=lambda p: len(p.parts))
        return candidates[0]
    raise AppUpdateError(
        f"Archive Windows invalide : {APP_BUILD_NAME}.exe introuvable."
    )


def stage_update_from_zip(zip_path: Path) -> Path:
    """Dézippe l'archive dans updates/staging et retourne le payload prêt à installer."""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise AppUpdateError(f"Archive introuvable : {zip_path}")

    staging_root = UPDATES_DIR / "staging"
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staging_root)

    payload = _find_payload(staging_root)
    if sys.platform == "darwin":
        final = staging_root / f"{APP_BUILD_NAME}.app"
    else:
        final = staging_root / APP_BUILD_NAME

    if payload.resolve() != final.resolve():
        if final.exists():
            shutil.rmtree(final)
        shutil.move(str(payload), str(final))

    return final


def _write_macos_installer(script_path: Path) -> None:
    script_path.write_text(
        """#!/bin/bash
set -e
PID="$1"
STAGED="$2"
TARGET="$3"
LOG="$4"
exec >>"$LOG" 2>&1
echo "[update] wait pid=$PID"
while kill -0 "$PID" 2>/dev/null; do sleep 0.4; done
sleep 1
echo "[update] replace $TARGET"
rm -rf "$TARGET"
mkdir -p "$(dirname "$TARGET")"
mv "$STAGED" "$TARGET"
xattr -cr "$TARGET" 2>/dev/null || true
echo "[update] launch"
open "$TARGET"
echo "[update] done"
""",
        encoding="utf-8",
    )
    script_path.chmod(0o755)


def _write_windows_installer(script_path: Path) -> None:
    script_path.write_text(
        r"""param(
  [Parameter(Mandatory=$true)][int]$ProcessId,
  [Parameter(Mandatory=$true)][string]$Staged,
  [Parameter(Mandatory=$true)][string]$Target,
  [Parameter(Mandatory=$true)][string]$LaunchExe,
  [Parameter(Mandatory=$true)][string]$Log
)
$ErrorActionPreference = 'Continue'
function Write-Log($m) { Add-Content -Path $Log -Value ("[{0}] {1}" -f (Get-Date -Format o), $m) }
Write-Log "wait pid=$ProcessId"
while (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 400 }
Start-Sleep -Seconds 1
Write-Log "replace $Target"
if (Test-Path $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
$parent = Split-Path -Parent $Target
if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
Move-Item -LiteralPath $Staged -Destination $Target
Write-Log "launch $LaunchExe"
Start-Process -FilePath $LaunchExe -WorkingDirectory $Target
Write-Log "done"
""",
        encoding="utf-8",
    )


def apply_update_and_relaunch(zip_path: Path) -> None:
    """
    Prépare la nouvelle version et lance un script d'installation détaché.
    Le caller doit quitter l'app immédiatement après.
    """
    install_path = current_install_path()
    if install_path is None:
        raise AppUpdateError(
            "Installation auto disponible uniquement depuis l’application empaquetée "
            "(.app / .exe), pas en mode développement."
        )

    staged = stage_update_from_zip(Path(zip_path))
    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    log_path = UPDATES_DIR / "install.log"
    pid = os.getpid()

    if sys.platform == "darwin":
        script = UPDATES_DIR / "install_update.sh"
        _write_macos_installer(script)
        subprocess.Popen(
            [
                "/bin/bash",
                str(script),
                str(pid),
                str(staged),
                str(install_path),
                str(log_path),
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    elif sys.platform == "win32":
        script = UPDATES_DIR / "install_update.ps1"
        _write_windows_installer(script)
        launch_after = str(Path(install_path) / f"{APP_BUILD_NAME}.exe")
        creationflags = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creationflags |= subprocess.DETACHED_PROCESS
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-ProcessId",
                str(pid),
                "-Staged",
                str(staged),
                "-Target",
                str(install_path),
                "-LaunchExe",
                launch_after,
                "-Log",
                str(log_path),
            ],
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        raise AppUpdateError("Plateforme non supportée pour l’install auto.")
