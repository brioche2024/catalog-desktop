from __future__ import annotations

import re
import unicodedata
from time import sleep
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .config import USER_AGENT
from .http_client import create_http_session
from .efashion_client import EfashionApiError, EfashionClient
from .mel_mapper import PFS_COLOR_ALIASES
from .pfs_client import product_images_by_color, resolve_efashion_vendu_par

MAX_PHOTOS_PER_REQUEST = 10
MAX_PHOTOS_PER_PRODUCT = 20
PHOTO_UPLOAD_MAX_ATTEMPTS = 3


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
    with create_http_session(
        timeout=60.0,
        headers={"User-Agent": USER_AGENT},
        allow_redirects=True,
    ) as client:
        response = client.get(full_url)
    if response.status_code >= 400:
        raise EfashionApiError(f"Téléchargement photo source impossible ({response.status_code}).")
    content_type = (response.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    ext = _guess_extension(content_type, full_url)
    filename = f"pfs{ext}"
    return response.content, filename, content_type


def _efashion_candidates_for_reference(
    ef_products: list[dict[str, Any]],
    reference_base: str,
) -> list[dict[str, Any]]:
    base_norm = _normalize(reference_base)
    return [
        item
        for item in ef_products
        if _normalize(str(item.get("referenceBase") or "")) == base_norm
        or _normalize(str(item.get("reference") or "")).startswith(base_norm)
    ]


def _single_fiche_product_id(candidates: list[dict[str, Any]]) -> int | None:
    """Retourne l'id de la fiche unique (melangees, tailles, couleur unique…)."""
    if len(candidates) != 1:
        return None
    product_id = candidates[0].get("id")
    return int(product_id) if product_id is not None else None


def _find_product_id_for_color(
    ef_products: list[dict[str, Any]],
    reference_base: str,
    color_label: str,
) -> int | None:
    wanted = _color_aliases(color_label)
    candidates = _efashion_candidates_for_reference(ef_products, reference_base)
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


def _product_color_labels(product: dict[str, Any]) -> list[str]:
    raw = str(product.get("colors") or product.get("couleurs") or "").strip()
    if not raw:
        return []
    return [part.strip() for part in re.split(r"[;,]", raw) if part.strip()]


def _filter_groups_for_product(
    product: dict[str, Any],
    groups: list[tuple[str, list[str]]],
) -> list[tuple[str, list[str]]]:
    """Limite les photos aux couleurs du produit (ex. pack split x4-REF)."""
    wanted_labels = _product_color_labels(product)
    if not wanted_labels:
        return groups

    wanted: set[str] = set()
    for label in wanted_labels:
        wanted |= _color_aliases(label)
        wanted.add(_normalize(label))

    filtered = [
        (label, urls)
        for label, urls in groups
        if label and (_normalize(label) in wanted or (_color_aliases(label) & wanted))
    ]
    return filtered if filtered else groups


def _resolve_single_couleur_photo_job(
    product: dict[str, Any],
    groups: list[tuple[str, list[str]]],
    ef_item: dict[str, Any],
) -> tuple[str, list[str]] | None:
    """Photos pour une fiche unique en mode couleurs (jamais l'agrégat multi-couleurs)."""
    color_hint = _extract_efashion_color(
        str(ef_item.get("reference") or ""),
        str(ef_item.get("referenceBase") or ""),
    )
    if color_hint:
        urls = _urls_for_color_label(groups, color_hint)
        if urls:
            return color_hint, urls

    if len(groups) == 1:
        label, urls = groups[0]
        if urls:
            return label or "MAIN", list(urls)[:MAX_PHOTOS_PER_PRODUCT]

    product_colors = _product_color_labels(product)
    if len(product_colors) == 1:
        urls = _urls_for_color_label(groups, product_colors[0])
        if urls:
            return product_colors[0], urls

    return None


def photo_urls_for_color(product: dict[str, Any], color_label: str) -> list[str]:
    """URLs PFS pour une couleur (ou la seule couleur connue), sans agrégat multi-couleurs."""
    groups = _filter_groups_for_product(product, product_images_by_color(product))
    if color_label:
        urls = _urls_for_color_label(groups, color_label)
        if urls:
            return urls
        if len(groups) == 1:
            return list(groups[0][1])[:MAX_PHOTOS_PER_PRODUCT]
        return []

    if len(groups) == 1:
        return list(groups[0][1])[:MAX_PHOTOS_PER_PRODUCT]

    product_colors = _product_color_labels(product)
    if len(product_colors) == 1:
        return _urls_for_color_label(groups, product_colors[0])
    return []


def _aggregate_photo_urls(groups: list[tuple[str, list[str]]]) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for _color_label, color_urls in groups:
        for url in color_urls:
            if url not in seen:
                seen.add(url)
                urls.append(url)
            if len(urls) >= MAX_PHOTOS_PER_PRODUCT:
                return urls
    return urls


def _pair_remaining_by_order(
    jobs_unmatched: list[tuple[str, str, list[str]]],
    ef_products: list[dict[str, Any]],
    already_used: set[int],
) -> list[tuple[str, str, int, list[str]]]:
    """Associe les couleurs restantes par alias de couleur (pas un dump sur la principale)."""
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
        still_pending: list[tuple[str, str, list[str]]] = []
        for reference_job, color_label, urls in pending:
            match_id = None
            for item in available:
                product_id = int(item["id"])
                if product_id in already_used:
                    continue
                ef_color = _extract_efashion_color(
                    str(item.get("reference") or ""),
                    str(item.get("referenceBase") or ""),
                )
                if not ef_color:
                    continue
                if _normalize(ef_color) in _color_aliases(color_label) or (
                    _color_aliases(ef_color) & _color_aliases(color_label)
                ):
                    match_id = product_id
                    break
            if match_id is None:
                still_pending.append((reference_job, color_label, urls))
                continue
            already_used.add(match_id)
            paired.append((reference_job, color_label, match_id, urls))

        # Pas de fallback ordonné : éviter d'envoyer les photos d'une couleur
        # sur la fiche principale d'une autre couleur.
        _ = still_pending

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


def _urls_for_color_label(
    groups: list[tuple[str, list[str]]],
    color_label: str,
) -> list[str]:
    """Photos PFS d'une seule couleur (jamais l'agrégat multi-couleurs)."""
    if not color_label:
        return []
    wanted = _color_aliases(color_label)
    for label, urls in groups:
        if not label:
            continue
        if _normalize(label) in wanted or (_color_aliases(label) & wanted):
            return list(urls)[:MAX_PHOTOS_PER_PRODUCT]
    return []


def _upload_chunk_with_retry(
    client: EfashionClient,
    product_id: int,
    files: list[tuple[str, bytes, str]],
) -> None:
    """Réessaie un upload S3 transitoire avant de laisser la fiche en brouillon."""
    last_error: Exception | None = None
    for attempt in range(1, PHOTO_UPLOAD_MAX_ATTEMPTS + 1):
        try:
            client.upload_product_photos(product_id, files)
            return
        except Exception as exc:  # Réseau / API : l'erreur finale est remontée au produit.
            last_error = exc
            if attempt < PHOTO_UPLOAD_MAX_ATTEMPTS:
                sleep(attempt)
    raise EfashionApiError(
        f"upload échoué après {PHOTO_UPLOAD_MAX_ATTEMPTS} tentatives : {last_error}"
    )


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
            "photo_ready_references": [],
        }

    jobs: list[tuple[str, str, int, list[str]]] = []
    unmatched: list[tuple[str, str, list[str]]] = []
    errors: list[str] = []
    used_ids: set[int] = set()
    expected_ids_by_reference: dict[str, set[int]] = {}

    for product in pfs_products:
        reference = str(product.get("reference") or "").strip()
        if not reference:
            continue
        expected_ids_by_reference.setdefault(reference, set()).update(
            int(item["id"])
            for item in _efashion_candidates_for_reference(ef_products, reference)
            if item.get("id") is not None
        )
        groups = _filter_groups_for_product(product, product_images_by_color(product))
        if not groups:
            errors.append(f"{reference} : aucune photo source.")
            continue

        candidates = _efashion_candidates_for_reference(ef_products, reference)
        vendu_par = resolve_efashion_vendu_par(product)
        single_fiche_id = _single_fiche_product_id(candidates)

        # Couleurs mélangées uniquement : toutes les photos sur la fiche unique.
        if vendu_par == "melangees":
            target_id = single_fiche_id
            if target_id is None and candidates:
                main = next((item for item in candidates if item.get("main")), None)
                target = main or candidates[0]
                if target.get("id") is not None:
                    target_id = int(target["id"])
            if target_id is None:
                errors.append(f"{reference} : fiche EFashion introuvable (mélangées).")
                continue
            urls = _aggregate_photo_urls(groups)[:MAX_PHOTOS_PER_PRODUCT]
            used_ids.add(target_id)
            jobs.append((reference, "MAIN", target_id, urls))
            continue

        if not candidates:
            errors.append(f"{reference} : fiche EFashion introuvable.")
            continue

        # tailles : une seule fiche → toutes les photos.
        if single_fiche_id is not None and vendu_par == "tailles":
            urls = _aggregate_photo_urls(groups)[:MAX_PHOTOS_PER_PRODUCT]
            used_ids.add(single_fiche_id)
            jobs.append((reference, "MAIN", single_fiche_id, urls))
            continue

        # couleurs : une seule fiche → photos de CETTE couleur seulement.
        if single_fiche_id is not None and vendu_par == "couleurs":
            resolved = _resolve_single_couleur_photo_job(product, groups, candidates[0])
            if resolved is None:
                errors.append(f"{reference} : fiche unique sans photos pour sa couleur.")
                continue
            color_label, urls = resolved
            used_ids.add(single_fiche_id)
            jobs.append(
                (
                    reference,
                    color_label,
                    single_fiche_id,
                    urls[:MAX_PHOTOS_PER_PRODUCT],
                )
            )
            continue

        # Mode couleurs : 1 couleur → 1 fiche, photos de CETTE couleur seulement.
        for color_label, urls in groups:
            product_id = _find_product_id_for_color(ef_products, reference, color_label)
            if product_id is None or product_id in used_ids:
                unmatched.append(
                    (reference, color_label or "DEFAULT", urls[:MAX_PHOTOS_PER_PRODUCT])
                )
                continue
            used_ids.add(product_id)
            jobs.append(
                (reference, color_label or "DEFAULT", product_id, urls[:MAX_PHOTOS_PER_PRODUCT])
            )

        # Fiches EFashion restantes (ex. principale après bascule rupture) :
        # uniquement les photos de la couleur de cette fiche — jamais l'agrégat.
        leftover = [
            item
            for item in candidates
            if item.get("id") is not None and int(item["id"]) not in used_ids
        ]
        leftover.sort(
            key=lambda item: (0 if item.get("main") else 1, str(item.get("reference") or ""))
        )
        for item in leftover:
            product_id = int(item["id"])
            color_hint = _extract_efashion_color(
                str(item.get("reference") or ""),
                str(item.get("referenceBase") or ""),
            )
            urls = _urls_for_color_label(groups, color_hint) if color_hint else []
            if not urls:
                # Pas de photos pour cette couleur → on n'injecte pas les autres.
                continue
            used_ids.add(product_id)
            jobs.append((reference, color_hint or "SECONDARY", product_id, urls))

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
    photo_attached_ids: set[int] = set()

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
                _upload_chunk_with_retry(client, product_id, chunk)
                uploaded_photos += len(chunk)
                # Une seule photo validée suffit pour autoriser cette fiche.
                photo_attached_ids.add(product_id)
            uploaded_products += 1
        except EfashionApiError as exc:
            errors.append(f"{reference}/{color_label} : upload échoué ({exc})")

    photo_ready_references: list[str] = []
    for reference, expected_ids in expected_ids_by_reference.items():
        missing_ids = expected_ids - photo_attached_ids
        if expected_ids and not missing_ids:
            photo_ready_references.append(reference)
        elif missing_ids:
            errors.append(
                f"{reference} : {len(missing_ids)} fiche(s) sans photo validée "
                "— laissée(s) en brouillon."
            )

    return {
        "uploaded_products": uploaded_products,
        "uploaded_photos": uploaded_photos,
        "errors": errors,
        "photo_ready_references": photo_ready_references,
    }
