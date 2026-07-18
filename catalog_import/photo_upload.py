from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import USER_AGENT
from .efashion_client import EfashionApiError, EfashionClient
from .mel_mapper import PFS_COLOR_ALIASES
from .pfs_client import product_images_by_color

MAX_PHOTOS_PER_REQUEST = 10
MAX_PHOTOS_PER_PRODUCT = 20


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKD", text or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().strip()
    value = re.sub(r"\s+", " ", value)
    return value


def _full_quality_url(url: str) -> str:
    """Retire le resize PFS (?image_process=...) pour récupérer l'image pleine qualité."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _color_aliases(label: str) -> set[str]:
    key = label.strip().upper()
    names = {label, label.title(), key}
    for alias in PFS_COLOR_ALIASES.get(key, []):
        names.add(alias)
    # aussi chercher dans les valeurs d'alias inversées
    for pfs_key, aliases in PFS_COLOR_ALIASES.items():
        if _normalize(label) in {_normalize(a) for a in aliases} or _normalize(
            label
        ) == _normalize(pfs_key):
            names.add(pfs_key)
            names.update(aliases)
    return {_normalize(name) for name in names if name}


def _extract_efashion_color(reference: str, reference_base: str) -> str:
    ref = (reference or "").strip()
    base = (reference_base or "").strip()
    if base and ref.upper().startswith(base.upper() + "-"):
        return ref[len(base) + 1 :].strip()
    if "-" in ref:
        return ref.rsplit("-", 1)[-1].strip()
    return ""


def _guess_extension(content_type: str, url: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }
    if content_type in mapping:
        return mapping[content_type]
    path = urlsplit(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def download_image(url: str) -> tuple[bytes, str, str]:
    full_url = _full_quality_url(url)
    with httpx.Client(timeout=60.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        response = client.get(full_url)
    if response.status_code >= 400:
        raise EfashionApiError(f"Téléchargement photo PFS impossible ({response.status_code}).")
    content_type = (response.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    ext = _guess_extension(content_type, full_url)
    filename = f"pfs{ext}"
    return response.content, filename, content_type


def _find_product_id_for_color(
    ef_products: list[dict[str, Any]],
    reference_base: str,
    color_label: str,
) -> int | None:
    wanted = _color_aliases(color_label)
    base_norm = _normalize(reference_base)

    candidates = [
        item
        for item in ef_products
        if _normalize(str(item.get("referenceBase") or "")) == base_norm
        or _normalize(str(item.get("reference") or "")).startswith(base_norm)
    ]
    if not candidates:
        return None

    if not color_label:
        main = next((item for item in candidates if item.get("main")), None)
        target = main or candidates[0]
        return int(target["id"]) if target.get("id") is not None else None

    # 1) match exact sur alias
    for item in candidates:
        ef_color = _extract_efashion_color(
            str(item.get("reference") or ""),
            str(item.get("referenceBase") or ""),
        )
        if ef_color and _normalize(ef_color) in wanted:
            return int(item["id"])

    # 2) match par tokens (ex. GIRS/BLANC vs Gris)
    for item in candidates:
        ef_color = _extract_efashion_color(
            str(item.get("reference") or ""),
            str(item.get("referenceBase") or ""),
        )
        tokens = {
            _normalize(tok)
            for tok in re.split(r"[/_\-\s]+", ef_color)
            if tok.strip()
        }
        if tokens & wanted:
            return int(item["id"])

    # 3) une seule fiche restante pour cette référence
    if len(candidates) == 1 and candidates[0].get("id") is not None:
        return int(candidates[0]["id"])
    return None


def _pair_remaining_by_order(
    jobs_unmatched: list[tuple[str, str, list[str]]],
    ef_products: list[dict[str, Any]],
    already_used: set[int],
) -> list[tuple[str, str, int, list[str]]]:
    """Dernier recours : associe les couleurs restantes 1:1 dans l'ordre."""
    paired: list[tuple[str, str, int, list[str]]] = []
    by_ref: dict[str, list[tuple[str, str, list[str]]]] = {}
    for reference, color_label, urls in jobs_unmatched:
        by_ref.setdefault(reference, []).append((reference, color_label, urls))

    for reference, pending in by_ref.items():
        base_norm = _normalize(reference)
        available = [
            item
            for item in ef_products
            if item.get("id") is not None
            and int(item["id"]) not in already_used
            and (
                _normalize(str(item.get("referenceBase") or "")) == base_norm
                or _normalize(str(item.get("reference") or "")).startswith(base_norm)
            )
        ]
        for job, item in zip(pending, available):
            product_id = int(item["id"])
            already_used.add(product_id)
            paired.append((job[0], job[1], product_id, job[2]))
    return paired


def fetch_upload_products_for_shootings(
    client: EfashionClient,
    shooting_ids: list[int],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not shooting_ids:
        return items

    page = 1
    while True:
        payload = client.get_upload_products(
            shooting_ids=shooting_ids,
            page=page,
            limit=100,
        )
        batch = payload.get("items") or []
        items.extend(item for item in batch if isinstance(item, dict))
        total_pages = int(payload.get("totalPages") or 1)
        if page >= total_pages or not batch:
            break
        page += 1
    return items


def upload_pfs_photos_to_efashion(
    client: EfashionClient,
    pfs_products: list[dict[str, Any]],
    shooting_ids: list[int],
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    ef_products = fetch_upload_products_for_shootings(client, shooting_ids)
    if not ef_products:
        return {
            "uploaded_products": 0,
            "uploaded_photos": 0,
            "errors": ["Aucun produit UPGR trouvé pour y rattacher les photos."],
        }

    jobs: list[tuple[str, str, int, list[str]]] = []
    unmatched: list[tuple[str, str, list[str]]] = []
    errors: list[str] = []
    used_ids: set[int] = set()

    for product in pfs_products:
        reference = str(product.get("reference") or "").strip()
        if not reference:
            continue
        groups = product_images_by_color(product)
        if not groups:
            errors.append(f"{reference} : aucune photo PFS.")
            continue

        for color_label, urls in groups:
            product_id = _find_product_id_for_color(ef_products, reference, color_label)
            if product_id is None or product_id in used_ids:
                unmatched.append((reference, color_label or "DEFAULT", urls[:MAX_PHOTOS_PER_PRODUCT]))
                continue
            used_ids.add(product_id)
            jobs.append((reference, color_label or "DEFAULT", product_id, urls[:MAX_PHOTOS_PER_PRODUCT]))

    if unmatched:
        paired = _pair_remaining_by_order(unmatched, ef_products, used_ids)
        paired_keys = {(ref, color) for ref, color, _, _ in paired}
        jobs.extend(paired)
        for reference, color_label, _urls in unmatched:
            if (reference, color_label) not in paired_keys:
                errors.append(
                    f"{reference} / {color_label} : fiche EFashion introuvable."
                )

    total_jobs = max(len(jobs), 1)
    uploaded_products = 0
    uploaded_photos = 0

    for index, (reference, color_label, product_id, urls) in enumerate(jobs, start=1):
        if on_progress:
            on_progress(
                index,
                total_jobs,
                f"Photos S3 : {index}/{total_jobs} — {reference} ({color_label})",
            )

        files: list[tuple[str, bytes, str]] = []
        for url_index, url in enumerate(urls):
            try:
                content, filename, content_type = download_image(url)
                safe_name = f"{reference}-{color_label}-{url_index}{filename[filename.rfind('.'):]}"
                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", safe_name)
                files.append((safe_name, content, content_type))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{reference}/{color_label} : download échoué ({exc})")

        if not files:
            continue

        try:
            for offset in range(0, len(files), MAX_PHOTOS_PER_REQUEST):
                chunk = files[offset : offset + MAX_PHOTOS_PER_REQUEST]
                client.upload_product_photos(product_id, chunk)
                uploaded_photos += len(chunk)
            uploaded_products += 1
        except EfashionApiError as exc:
            errors.append(f"{reference}/{color_label} : upload échoué ({exc})")

    return {
        "uploaded_products": uploaded_products,
        "uploaded_photos": uploaded_photos,
        "errors": errors,
    }
