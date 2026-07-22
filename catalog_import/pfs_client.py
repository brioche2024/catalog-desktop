from __future__ import annotations

import copy
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from curl_cffi.requests import Response, Session

from .config import (
    DEFAULT_ENRICH_CONCURRENCY,
    DEFAULT_FETCH_RETRIES,
    DEFAULT_FETCH_RETRY_DELAY,
    DEFAULT_PER_PAGE,
    DEFAULT_PRODUCT_STATUS,
    DEFAULT_VARIANTS_STATUS,
    PFS_ACTIVE_PRODUCT_STATUSES,
    PFS_CATALOG_PRODUCT_STATUSES,
    PFS_DISABLED_PRODUCT_STATUSES,
    PFS_DRAFT_PRODUCT_STATUSES,
    PFS_LIST_PRODUCTS_URL,
    PFS_LIST_VARIANTS_URL,
    PFS_PRODUCT_URL,
    PFS_PRODUCT_VARIANTS_URL,
    USER_AGENT,
)
from .http_client import NETWORK_ERRORS, create_http_session
from .session_store import PfsSession


class PfsApiError(Exception):
    pass


_RETRYABLE_STATUS = {429, 502, 503, 504}


def _retry_delay(response: Response | None, delay: float) -> float:
    if response is None:
        return delay
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(float(retry_after), delay)
        except ValueError:
            pass
    return delay


def _get_with_retries(
    client: Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_retries: int = DEFAULT_FETCH_RETRIES,
    initial_delay: float = DEFAULT_FETCH_RETRY_DELAY,
) -> Response:
    delay = initial_delay
    last_response: Response | None = None

    for attempt in range(max_retries):
        try:
            response = client.get(url, params=params)
            last_response = response
        except NETWORK_ERRORS as exc:
            if attempt + 1 >= max_retries:
                raise PfsApiError(f"API compte source injoignable : {exc}") from exc
            time.sleep(delay)
            delay = min(delay * 2, 10.0)
            continue

        if response.status_code == 401:
            raise PfsApiError("Session compte source expirée ou non autorisée. Reconnectez-vous.")
        if response.status_code in _RETRYABLE_STATUS and attempt + 1 < max_retries:
            time.sleep(_retry_delay(response, delay))
            delay = min(delay * 2, 10.0)
            continue
        return response

    assert last_response is not None
    return last_response


def _first_str(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _localized_label(value: Any, lang: str = "fr") -> str:
    if isinstance(value, dict):
        text = value.get(lang) or value.get("en")
        if text is not None and str(text).strip():
            return str(text)
    if value is not None and str(value).strip():
        return str(value)
    return ""


def _gender_label(value: Any) -> str:
    mapping = {
        "MAN": "Homme",
        "WOMAN": "Femme",
        "WOMEN": "Femme",
        "UNISEX": "Unisexe",
        "KID": "Enfant",
        "KIDS": "Enfant",
    }
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("reference") or value.get("name")
    if value is None:
        return ""
    key = str(value).upper()
    return mapping.get(key, str(value))


def _auth_headers(access_token: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }


def _extract_product_detail(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    if isinstance(payload, dict):
        return payload
    return None


def _extract_list_data(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [item for item in payload["data"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return None


def _fetch_json(access_token: str, url: str) -> Any | None:
    headers = _auth_headers(access_token)
    with create_http_session(timeout=60.0, headers=headers) as client:
        response = _get_with_retries(client, url)

    if response.status_code >= 400:
        return None

    try:
        return response.json()
    except ValueError:
        return None


def fetch_product_detail(access_token: str, product_id: str) -> dict[str, Any] | None:
    payload = _fetch_json(access_token, PFS_PRODUCT_URL.format(product_id=product_id))
    if payload is None:
        return None
    return _extract_product_detail(payload)


def fetch_product_variants(
    access_token: str, product_id: str
) -> list[dict[str, Any]] | None:
    payload = _fetch_json(
        access_token, PFS_PRODUCT_VARIANTS_URL.format(product_id=product_id)
    )
    if payload is None:
        return None
    return _extract_list_data(payload)


def _total_stock(product: dict[str, Any]) -> int:
    variants = product.get("variants")
    if not isinstance(variants, list):
        return 0
    total = 0
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        qty = variant.get("stock_qty")
        if isinstance(qty, (int, float)):
            total += int(qty)
    return total


def product_pfs_status(product: dict[str, Any]) -> str:
    return _first_str(product, "status", "state").upper()


def classify_pfs_product(product: dict[str, Any]) -> str:
    """Retourne 'disabled', 'draft' ou 'active' selon le statut PFS."""
    status = product_pfs_status(product)
    if status in PFS_DISABLED_PRODUCT_STATUSES:
        return "disabled"
    if status in PFS_DRAFT_PRODUCT_STATUSES:
        return "draft"
    if status in PFS_ACTIVE_PRODUCT_STATUSES or status == DEFAULT_PRODUCT_STATUS:
        return "active"
    # READY_FOR_SALE, etc. : actif par défaut si vendable
    return "active"


def product_is_pfs_disabled(product: dict[str, Any]) -> bool:
    return classify_pfs_product(product) == "disabled"


def product_is_pfs_draft(product: dict[str, Any]) -> bool:
    return classify_pfs_product(product) == "draft"


def _format_composition(items: Any) -> str:
    if not isinstance(items, list) or not items:
        return ""

    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = _localized_label(item.get("labels")) or _first_str(item, "reference")
        percentage = item.get("percentage")
        if label and percentage is not None:
            parts.append(f"{label} {int(percentage)}%")
        elif label:
            parts.append(label)
    return ", ".join(parts)


def format_creation_date(product: dict[str, Any]) -> str:
    raw = _first_str(product, "creation_date", "created_at")
    if not raw:
        return ""
    text = raw.replace("Z", "+00:00")
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return raw[:10]
    return parsed.strftime("%Y-%m-%d %H:%M")


def _collection_label(product: dict[str, Any]) -> str:
    collection = product.get("collection")
    if isinstance(collection, dict):
        return _localized_label(collection.get("labels")) or _first_str(
            collection, "reference"
        )
    return ""


def merge_product_detail(summary: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    merged = dict(summary)
    for key in (
        "material_composition",
        "lining_composition",
        "collection",
        "country_of_manufacture",
        "description",
        "family",
        "default_color",
        "mannequin_height",
        "mannequin_size",
        "size_details_tu",
        "gender",
    ):
        value = detail.get(key)
        if value not in (None, "", []):
            merged[key] = value

    detail_images = detail.get("images")
    if isinstance(detail_images, dict) and detail_images:
        merged["images"] = detail_images

    detail_category = detail.get("category")
    if isinstance(detail_category, dict):
        summary_category = merged.get("category")
        if isinstance(summary_category, dict):
            merged["category"] = {**summary_category, **detail_category}
        else:
            merged["category"] = detail_category

    if not merged.get("labels") and isinstance(detail.get("label"), dict):
        merged["labels"] = detail["label"]

    merged["detail_loaded"] = True
    return merged


def _positive_weights(variants: list[dict[str, Any]]) -> list[float]:
    weights: list[float] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        weight = variant.get("weight")
        if isinstance(weight, (int, float)) and weight > 0:
            weights.append(float(weight))
    return weights


def product_weight_kg(product: dict[str, Any]) -> float | None:
    weight = product.get("weight")
    if isinstance(weight, (int, float)) and weight > 0:
        return float(weight)

    variants = product.get("variants")
    if isinstance(variants, list):
        weights = _positive_weights(variants)
        if weights:
            return max(weights)
    return None


def merge_product_variants(
    summary: dict[str, Any], variants: list[dict[str, Any]]
) -> dict[str, Any]:
    merged = dict(summary)
    if not variants:
        return merged

    merged["variants"] = variants
    merged["variants_loaded"] = True

    weights = _positive_weights(variants)
    if weights:
        merged["weight"] = max(weights)

    merged["vendu_par_label"] = infer_vendu_par_label(merged)
    merged["pack_label"] = product_pack_label(merged)

    return merged


def _format_variant_pack(variant: dict[str, Any]) -> str:
    parts: list[str] = []
    for pack in variant.get("packs") or []:
        if not isinstance(pack, dict):
            continue
        color_data = pack.get("color")
        color = ""
        if isinstance(color_data, dict):
            color = _localized_label(color_data.get("labels")) or _first_str(
                color_data, "reference"
            )
        for size_entry in pack.get("sizes") or []:
            if not isinstance(size_entry, dict):
                continue
            qty = size_entry.get("qty")
            size = size_entry.get("size")
            if qty is None or not size:
                continue
            segment = f"{qty}×{size}"
            if color:
                segment = f"{segment} ({color})"
            parts.append(segment)

    if parts:
        return ", ".join(parts)

    pieces = variant.get("pieces")
    if isinstance(pieces, (int, float)) and pieces > 0:
        return f"{int(pieces)} pcs"
    return ""


def _variant_type(variant: dict[str, Any]) -> str:
    if not isinstance(variant, dict):
        return ""
    return str(variant.get("type") or "ITEM").upper()


def _active_variants(product: dict[str, Any]) -> list[dict[str, Any]]:
    variants = product.get("variants")
    if not isinstance(variants, list):
        return []
    active = [
        variant
        for variant in variants
        if isinstance(variant, dict) and variant.get("is_active") is not False
    ]
    if active:
        return active
    return [variant for variant in variants if isinstance(variant, dict)]


def _normalize_size(size: str) -> str:
    text = str(size or "").strip()
    return text or "TU"


def _item_color_and_sizes(
    variants: list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    colors: set[str] = set()
    sizes: set[str] = set()
    for variant in variants:
        if _variant_type(variant) != "ITEM":
            continue
        item = variant.get("item") if isinstance(variant.get("item"), dict) else {}
        color = item.get("color") if isinstance(item.get("color"), dict) else {}
        color_label = (
            _localized_label(color.get("labels"))
            or _first_str(color, "reference")
            or ""
        ).strip()
        if color_label:
            colors.add(color_label)
        sizes.add(_normalize_size(_first_str(item, "size")))
    return colors, sizes


def _variant_has_stock(variant: dict[str, Any]) -> bool:
    if not isinstance(variant, dict):
        return False
    in_stock = variant.get("in_stock")
    if in_stock is True:
        return True
    if in_stock is False:
        return False
    qty = variant.get("stock_qty")
    if isinstance(qty, (int, float)):
        return int(qty) > 0
    return False


def _variant_color_label(variant: dict[str, Any]) -> str:
    if _variant_type(variant) == "ITEM":
        item = variant.get("item") if isinstance(variant.get("item"), dict) else {}
        color = item.get("color") if isinstance(item.get("color"), dict) else {}
        return (
            _localized_label(color.get("labels"))
            or _first_str(color, "reference")
            or ""
        ).strip()
    if _variant_type(variant) == "PACK":
        pack_colors = _colors_in_pack_variant(variant)
        if len(pack_colors) == 1:
            return next(iter(pack_colors))
    return ""


def _variant_item_size_label(variant: dict[str, Any]) -> str:
    if _variant_type(variant) != "ITEM":
        return ""
    item = variant.get("item") if isinstance(variant.get("item"), dict) else {}
    return _normalize_size(_first_str(item, "size"))


def _variants_by_color(
    product: dict[str, Any],
) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """Regroupe les variantes actives par couleur (clé normalisée → libellé + variantes)."""
    grouped: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for variant in _active_variants(product):
        color_label = _variant_color_label(variant)
        if not color_label:
            continue
        key = color_label.strip().upper()
        if key not in grouped:
            grouped[key] = (color_label, [])
        grouped[key][1].append(variant)
    return grouped


def product_is_out_of_stock(product: dict[str, Any]) -> bool:
    """True si aucune variante active n'est disponible."""
    variants = _active_variants(product)
    if not variants:
        return True
    return not any(_variant_has_stock(variant) for variant in variants)


def mel_rupture_stock_by_color(product: dict[str, Any]) -> dict[str, int]:
    """Couleurs en rupture → stock 0 (libellé couleur PFS)."""
    rupture: dict[str, int] = {}
    for color_label, variants in _variants_by_color(product).values():
        if variants and not any(_variant_has_stock(variant) for variant in variants):
            rupture[color_label] = 0
    return rupture


def mel_stock_qty_by_color(product: dict[str, Any]) -> dict[str, int]:
    """
    Quantité stock par couleur (somme stock_qty des variantes actives).
    0 si rupture ; omis si en stock sans qty numérique.
    """
    result: dict[str, int] = {}
    for color_label, variants in _variants_by_color(product).values():
        if not variants:
            continue
        if not any(_variant_has_stock(variant) for variant in variants):
            result[color_label] = 0
            continue
        total = 0
        saw_qty = False
        for variant in variants:
            qty = variant.get("stock_qty")
            if isinstance(qty, (int, float)):
                saw_qty = True
                total += max(int(qty), 0)
        if saw_qty:
            result[color_label] = total
    return result


def mel_taille_stock_qtys(
    product: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Par couleur : stocks par taille (quantity depuis stock_qty / rupture)."""
    result: dict[str, list[dict[str, Any]]] = {}
    for variant in _active_variants(product):
        if _variant_type(variant) != "ITEM":
            continue
        color_label = _variant_color_label(variant)
        if not color_label:
            continue
        size = _variant_item_size_label(variant)
        qty = variant.get("stock_qty")
        if isinstance(qty, (int, float)):
            quantity = max(int(qty), 0)
        elif _variant_has_stock(variant):
            continue
        else:
            quantity = 0
        stocks = result.setdefault(color_label, [])
        existing = next((e for e in stocks if e.get("taille") == size), None)
        if existing:
            existing["quantity"] = int(existing.get("quantity") or 0) + quantity
        else:
            stocks.append({"taille": size, "quantity": quantity})
    return result


def _color_lookup_keys(color_label: str) -> set[str]:
    """Clés UPPER pour matcher un libellé couleur (BLACK ↔ NOIR via alias)."""
    raw = str(color_label or "").strip()
    if not raw:
        return set()
    keys = {raw.upper(), _color_match_key(raw).upper()}
    try:
        from .mel_mapper import PFS_COLOR_ALIASES
    except Exception:
        return {k for k in keys if k}

    upper = raw.upper()
    for alias in PFS_COLOR_ALIASES.get(upper, []):
        keys.add(str(alias).strip().upper())
    for pfs_key, aliases in PFS_COLOR_ALIASES.items():
        bucket = {pfs_key.upper(), *(str(a).strip().upper() for a in aliases)}
        if keys & bucket:
            keys |= bucket
    return {k for k in keys if k}


def color_is_available(product: dict[str, Any], color_label: str) -> bool:
    """True si au moins une variante active de cette couleur est en stock."""
    if not color_label:
        return False
    wanted = _color_lookup_keys(color_label)
    if not wanted:
        return False

    # Variantes actives de cette couleur (clés FR/EN)
    for key, (_label, variants) in _variants_by_color(product).items():
        if key not in wanted and _color_match_key(key).upper() not in wanted:
            continue
        return any(_variant_has_stock(variant) for variant in variants)

    # Couleur présente seulement en variantes inactives → indisponible
    raw_variants = product.get("variants")
    if isinstance(raw_variants, list):
        saw_color = False
        for variant in raw_variants:
            if not isinstance(variant, dict):
                continue
            label = _variant_color_label(variant)
            if not label:
                continue
            label_keys = _color_lookup_keys(label)
            if not (label_keys & wanted):
                continue
            saw_color = True
            if variant.get("is_active") is not False and _variant_has_stock(variant):
                return True
        if saw_color:
            return False

    # Pas d'info variante → on ne bloque pas
    return True


def inactive_variant_color_labels(product: dict[str, Any]) -> list[str]:
    """Libellés couleur des variantes dont toutes les occurrences sont is_active=false."""
    raw_variants = product.get("variants")
    if not isinstance(raw_variants, list) or not raw_variants:
        return []

    active_keys: set[str] = set()
    inactive_labels: dict[str, str] = {}

    for variant in raw_variants:
        if not isinstance(variant, dict):
            continue
        label = _variant_color_label(variant)
        if not label:
            continue
        key = _color_match_key(label)
        if variant.get("is_active") is not False:
            active_keys.add(key)
            inactive_labels.pop(key, None)
        elif key not in active_keys:
            inactive_labels[key] = label

    return list(inactive_labels.values())


def _color_match_key(label: str) -> str:
    return " ".join(str(label or "").strip().lower().split())


def product_default_color_label(product: dict[str, Any]) -> str:
    default = product.get("default_color")
    if isinstance(default, dict):
        return (
            _localized_label(default.get("labels"))
            or _first_str(default, "reference", "name")
            or ""
        ).strip()
    if isinstance(default, str):
        return default.strip()
    return ""


def resolve_main_color_label(
    product: dict[str, Any], color_names: list[str]
) -> str | None:
    """
    Choisit la couleur principale MEL :
    - défaut PFS s'il est disponible
    - sinon première couleur disponible (secondaire active)
    - sinon première de la liste (tout en rupture)
    """
    if not color_names:
        return None

    preferred = product_default_color_label(product)
    preferred_key = _color_match_key(preferred) if preferred else ""

    def _matches_preferred(name: str) -> bool:
        if not preferred_key:
            return False
        key = _color_match_key(name)
        return key == preferred_key or preferred_key in key or key in preferred_key

    if preferred_key:
        for name in color_names:
            if _matches_preferred(name) and color_is_available(product, name):
                return name
        # Principale PFS indisponible → première secondaire encore en stock
        for name in color_names:
            if _matches_preferred(name):
                continue
            if color_is_available(product, name):
                return name

    for name in color_names:
        if color_is_available(product, name):
            return name
    return color_names[0]


def mel_rupture_taille_stocks(
    product: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Par couleur, tailles en rupture (quantity=0 pour EFashion)."""
    result: dict[str, list[dict[str, Any]]] = {}
    for variant in _active_variants(product):
        if _variant_type(variant) != "ITEM":
            continue
        color_label = _variant_color_label(variant)
        if not color_label or _variant_has_stock(variant):
            continue
        size = _variant_item_size_label(variant)
        stocks = result.setdefault(color_label, [])
        if not any(entry.get("taille") == size for entry in stocks):
            stocks.append({"taille": size, "quantity": 0})
    return result


def efashion_mel_stock(product: dict[str, Any]) -> str:
    """Stock global MEL (mode melangees uniquement) : « 0 » si tout est en rupture."""
    if resolve_efashion_vendu_par(product) != "melangees":
        return ""
    if product_is_out_of_stock(product):
        return "0"
    return ""


def _colors_in_pack_variant(variant: dict[str, Any]) -> set[str]:
    colors: set[str] = set()
    for pack in variant.get("packs") or []:
        if not isinstance(pack, dict):
            continue
        color_data = pack.get("color")
        if not isinstance(color_data, dict):
            continue
        label = (
            _localized_label(color_data.get("labels"))
            or _first_str(color_data, "reference")
            or ""
        ).strip()
        if label:
            colors.add(label)
    return colors


def is_mixed_color_pack(product: dict[str, Any]) -> bool:
    """
    True si un paquet PFS mélange plusieurs couleurs (melangees).
    False si chaque variant PACK est mono-couleur (couleurs + paquet ex. 3-3).
    """
    pack_variants = [
        variant
        for variant in _active_variants(product)
        if _variant_type(variant) == "PACK"
    ]
    if not pack_variants:
        return False

    for variant in pack_variants:
        if len(_colors_in_pack_variant(variant)) > 1:
            return True
    return False


def infer_vendu_par_label(product: dict[str, Any]) -> str:
    variants = _active_variants(product)
    if not variants:
        return ""

    types = {_variant_type(variant) for variant in variants if _variant_type(variant)}
    has_item = "ITEM" in types
    has_pack = "PACK" in types

    if has_item and has_pack:
        return "Mixte (unité + paquet)"
    if has_pack and not has_item:
        if is_mixed_color_pack(product):
            return "Couleurs melangees"
        return "Couleurs"

    colors, sizes = _item_color_and_sizes(variants)
    if len(sizes) > 1:
        return "Tailles"
    if len(colors) > 1:
        return "Couleurs"
    return "Couleurs"


def resolve_efashion_vendu_par(product: dict[str, Any]) -> str:
    """Mode de vente EFashion : couleurs | melangees | tailles."""
    label = infer_vendu_par_label(product).lower()
    if "mixte" in label:
        # Une seule fiche pour l'instant : côté unité/couleurs.
        # Le mode paquet (même ref) pourra être envoyé à part plus tard.
        return "couleurs"
    if "melange" in label or ("paquet" in label and "mixte" not in label):
        return "melangees"
    if "taille" in label:
        return "tailles"
    return "couleurs"


def pack_color_labels(product: dict[str, Any]) -> list[str]:
    """Couleurs présentes dans un variant PACK (melangees)."""
    names: list[str] = []
    seen: set[str] = set()
    for variant in _active_variants(product):
        if _variant_type(variant) != "PACK":
            continue
        for pack in variant.get("packs") or []:
            if not isinstance(pack, dict):
                continue
            color_data = pack.get("color")
            if not isinstance(color_data, dict):
                continue
            label = (
                _localized_label(color_data.get("labels"))
                or _first_str(color_data, "reference")
                or ""
            ).strip()
            if label and label not in seen:
                seen.add(label)
                names.append(label)
    return names


def pack_quantities_by_size(
    product: dict[str, Any],
    sizes_order: list[str] | None = None,
) -> list[int] | None:
    """
    Somme les quantités PACK par taille (toutes couleurs confondues).
    Ex. Rouge 1×S + Bleu 1×S → qty S = 2.
    """
    qty_by_size: dict[str, int] = {}
    size_order: list[str] = []

    for variant in _active_variants(product):
        if _variant_type(variant) != "PACK":
            continue
        for pack in variant.get("packs") or []:
            if not isinstance(pack, dict):
                continue
            for size_entry in pack.get("sizes") or []:
                if not isinstance(size_entry, dict):
                    continue
                qty = size_entry.get("qty")
                if not isinstance(qty, (int, float)) or qty <= 0:
                    continue
                size = _normalize_size(_first_str(size_entry, "size"))
                qty_by_size[size] = qty_by_size.get(size, 0) + int(qty)
                if size not in size_order:
                    size_order.append(size)

    if not qty_by_size:
        return None

    order = sizes_order or size_order
    return [qty_by_size.get(size, 0) for size in order]


def pack_quantities_single_color_pack(
    product: dict[str, Any],
    sizes_order: list[str] | None = None,
) -> list[int] | None:
    """
    Quantités du paquet pour vendu à la couleur (premier variant PACK mono-couleur).
    N'aggrège pas les couleurs entre variants (ex. Bleu 3-3 + Rouge 3-3 → 3-3, pas 6-6).
    """
    for variant in _active_variants(product):
        if _variant_type(variant) != "PACK":
            continue
        qty_by_size: dict[str, int] = {}
        size_order: list[str] = []
        for pack in variant.get("packs") or []:
            if not isinstance(pack, dict):
                continue
            for size_entry in pack.get("sizes") or []:
                if not isinstance(size_entry, dict):
                    continue
                qty = size_entry.get("qty")
                if not isinstance(qty, (int, float)) or qty <= 0:
                    continue
                size = _normalize_size(_first_str(size_entry, "size"))
                qty_by_size[size] = qty_by_size.get(size, 0) + int(qty)
                if size not in size_order:
                    size_order.append(size)
        if qty_by_size:
            order = sizes_order or size_order
            return [qty_by_size.get(size, 0) for size in order]
    return None


def catalog_presence_key(reference: str, vendu_par: str) -> str:
    return f"{reference.strip()}|{vendu_par.strip() or 'couleurs'}"


def product_pack_label(product: dict[str, Any]) -> str:
    variants = product.get("variants")
    if not isinstance(variants, list):
        return ""

    labels: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        if not isinstance(variant, dict) or _variant_type(variant) != "PACK":
            continue
        label = _format_variant_pack(variant)
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return " | ".join(labels)


def pack_variant_signature(variant: dict[str, Any]) -> str:
    """
    Signature stable d'un pack pour regrouper ×4 / ×6 / 3-3, etc.
    Ex. « 4 », « 6 », « 3-3 ».
    """
    if not isinstance(variant, dict) or _variant_type(variant) != "PACK":
        return ""

    qty_by_size: dict[str, int] = {}
    size_order: list[str] = []
    for pack in variant.get("packs") or []:
        if not isinstance(pack, dict):
            continue
        for size_entry in pack.get("sizes") or []:
            if not isinstance(size_entry, dict):
                continue
            qty = size_entry.get("qty")
            if not isinstance(qty, (int, float)) or qty <= 0:
                continue
            size = _normalize_size(_first_str(size_entry, "size"))
            qty_by_size[size] = qty_by_size.get(size, 0) + int(qty)
            if size not in size_order:
                size_order.append(size)
    if qty_by_size:
        return "-".join(str(qty_by_size[size]) for size in size_order)

    pieces = variant.get("pieces")
    if isinstance(pieces, (int, float)) and pieces > 0:
        return str(int(pieces))
    return ""


def group_pack_variants_by_signature(
    product: dict[str, Any],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Regroupe les variantes PACK actives par signature (×4, ×6…)."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for variant in _active_variants(product):
        if _variant_type(variant) != "PACK":
            continue
        signature = pack_variant_signature(variant)
        if not signature:
            continue
        if signature not in grouped:
            grouped[signature] = []
            order.append(signature)
        grouped[signature].append(variant)
    return [(signature, grouped[signature]) for signature in order]


def expand_product_by_distinct_packs(product: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Même ref vendue en packs distincts (ex. ×4 et ×6) → une fiche par pack,
    avec référence préfixée (x4-REF, x6-REF) pour éviter les doublons EFashion.
    """
    groups = group_pack_variants_by_signature(product)
    if len(groups) <= 1:
        return [product]

    base_ref = _first_str(product, "reference", "sku")
    if not base_ref:
        return [product]

    non_pack = [
        variant
        for variant in (product.get("variants") or [])
        if isinstance(variant, dict) and _variant_type(variant) != "PACK"
    ]

    expanded: list[dict[str, Any]] = []
    for signature, pack_variants in groups:
        clone = copy.deepcopy(product)
        new_ref = f"x{signature}-{base_ref}"
        clone["reference"] = new_ref
        clone["reference_source"] = base_ref
        clone["pack_split_signature"] = signature
        clone["variants"] = list(non_pack) + list(pack_variants)
        clone["variants_loaded"] = True
        # Couleurs du pack uniquement (pas toute la liste d'origine)
        pack_colors = pack_color_labels(clone)
        if pack_colors:
            clone["colors"] = ";".join(pack_colors)
            clone.pop("couleurs", None)
        clone["pack_label"] = product_pack_label(clone)
        clone["vendu_par_label"] = infer_vendu_par_label(clone)
        expanded.append(clone)
    return expanded


def expand_products_by_distinct_packs(
    products: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Étend la liste ; notes informatives si des refs ont été dédoublées."""
    result: list[dict[str, Any]] = []
    notes: list[str] = []
    for product in products:
        base_ref = _first_str(product, "reference", "sku") or "?"
        expanded = expand_product_by_distinct_packs(product)
        if len(expanded) > 1:
            refs = ", ".join(_first_str(item, "reference") for item in expanded)
            notes.append(
                f"{base_ref} : {len(expanded)} packs distincts → fiches {refs}"
            )
        result.extend(expanded)
    return result, notes


def merge_list_variants_index(
    products: list[dict[str, Any]], variants_by_id: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    if not variants_by_id:
        return products

    merged_products: list[dict[str, Any]] = []
    for product in products:
        product_id = _first_str(product, "id")
        variants = variants_by_id.get(product_id)
        if variants:
            merged_products.append(merge_product_variants(product, variants))
        else:
            merged_products.append(product)
    return merged_products


def enrich_one_product(
    summary: dict[str, Any], access_token: str
) -> dict[str, Any]:
    product_id = _first_str(summary, "id")
    if not product_id:
        return summary

    merged = summary
    detail = fetch_product_detail(access_token, product_id)
    if detail is not None:
        merged = merge_product_detail(merged, detail)

    if not merged.get("variants_loaded"):
        variants = fetch_product_variants(access_token, product_id)
        if variants is not None:
            merged = merge_product_variants(merged, variants)

    return merged


def _image_urls(product: dict[str, Any]) -> list[str]:
    images = product.get("images")
    if not isinstance(images, dict):
        return []

    urls: list[str] = []
    default = images.get("DEFAULT")
    if isinstance(default, str) and default:
        urls.append(default)

    for key, value in images.items():
        if key == "DEFAULT":
            continue
        if isinstance(value, str) and value:
            urls.append(value)
        elif isinstance(value, list):
            urls.extend(str(item) for item in value if item)

    return list(dict.fromkeys(urls))


def product_image_urls(product: dict[str, Any]) -> list[str]:
    return _image_urls(product)


def _color_label_map(product: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    variants = product.get("variants")
    if not isinstance(variants, list):
        return mapping

    def _add_color(color: dict[str, Any]) -> None:
        reference = color.get("reference")
        if not reference:
            return
        label = _localized_label(color.get("labels")) or str(reference)
        mapping[str(reference).upper()] = label

    for variant in variants:
        if not isinstance(variant, dict):
            continue
        item = variant.get("item") if isinstance(variant.get("item"), dict) else {}
        color = item.get("color") if isinstance(item.get("color"), dict) else {}
        if color:
            _add_color(color)
        for pack in variant.get("packs") or []:
            if not isinstance(pack, dict):
                continue
            pack_color = pack.get("color") if isinstance(pack.get("color"), dict) else {}
            if pack_color:
                _add_color(pack_color)
    return mapping


def _urls_from_value(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str) and value:
        urls.append(value)
    elif isinstance(value, list):
        urls.extend(str(item) for item in value if item)
    return urls


def product_images_by_color(product: dict[str, Any]) -> list[tuple[str, list[str]]]:
    images = product.get("images")
    if not isinstance(images, dict):
        return []

    label_map = _color_label_map(product)
    color_keys = [key for key in images.keys() if key != "DEFAULT"]

    groups: list[tuple[str, list[str]]] = []
    if color_keys:
        for key in color_keys:
            urls = list(dict.fromkeys(_urls_from_value(images.get(key))))
            if not urls:
                continue
            label = label_map.get(str(key).upper(), str(key).title())
            groups.append((label, urls))
    else:
        default_urls = list(dict.fromkeys(_urls_from_value(images.get("DEFAULT"))))
        if default_urls:
            groups.append(("", default_urls))

    return groups


PRODUCT_TABLE_HEADERS = [
    "Référence",
    "Titre",
    "Marque",
    "Catégorie",
    "Genre",
    "Prix HT",
    "Poids",
    "Vendu par",
    "Paquet",
    "Couleurs",
    "Tailles",
    "Stock",
    "Variantes",
    "Statut",
    "Créé le",
]


def product_table_row(product: dict[str, Any]) -> tuple[str, ...]:
    reference = _first_str(product, "reference", "sku", "ref", "code")
    title = _localized_label(product.get("labels")) or _first_str(
        product, "title", "name", "titre", "label"
    )

    brand = ""
    brand_data = product.get("brand")
    if isinstance(brand_data, dict):
        brand = str(brand_data.get("name") or "")

    category = ""
    category_data = product.get("category")
    if isinstance(category_data, dict):
        category = _localized_label(category_data.get("labels"))

    gender = _gender_label(product.get("gender"))

    unit_price = product.get("unit_price")
    if isinstance(unit_price, (int, float)):
        price = f"{unit_price:.2f} €"
    else:
        price = _first_str(product, "price", "prix", "prix_ht", "price_ht")

    weight = product_weight_kg(product)
    if weight is not None:
        weight_label = f"{weight:.2f} kg"
    else:
        weight_label = ""

    vendu_par = _first_str(product, "vendu_par_label") or infer_vendu_par_label(product)
    pack = _first_str(product, "pack_label") or product_pack_label(product)

    colors = _first_str(product, "colors", "couleurs")
    sizes = _first_str(product, "sizes", "tailles")
    stock = str(_total_stock(product))
    variants_count = str(product.get("count_variants") or len(product.get("variants") or []))
    status = _first_str(product, "status", "state")
    created = format_creation_date(product)

    return (
        reference,
        title,
        brand,
        category,
        gender,
        price,
        weight_label,
        vendu_par,
        pack,
        colors,
        sizes,
        stock,
        variants_count,
        status,
        created,
    )


def product_display_fields(product: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    row = product_table_row(product)
    return row[0], row[1], row[2], row[5], row[9], row[13]


def format_product_details(product: dict[str, Any]) -> str:
    row = product_table_row(product)
    collection = _collection_label(product)
    composition = _format_composition(product.get("material_composition"))
    lines = [
        f"Référence : {row[0]}",
        f"Titre : {row[1]}",
        f"Marque : {row[2]}",
        f"Catégorie : {row[3]}",
        f"Collection : {collection or '—'}",
        f"Composition : {composition or '—'}",
        f"Genre : {row[4]}",
        f"Prix HT : {row[5]}",
        f"Poids : {row[6] or '—'}",
        f"Vendu par : {row[7] or '—'}",
        f"Paquet : {row[8] or '—'}",
        f"Couleurs : {row[9]}",
        f"Tailles : {row[10]}",
        f"Stock total : {row[11]}",
        f"Variantes : {row[12]}",
        f"Statut : {row[13]}",
        f"ID source : {_first_str(product, 'id')}",
        f"Date création : {format_creation_date(product) or '—'}",
    ]

    country = _first_str(product, "country_of_manufacture")
    if country:
        lines.append(f"Pays de fabrication : {country}")

    lining = _format_composition(product.get("lining_composition"))
    if lining:
        lines.append(f"Doublure : {lining}")

    desc_raw = product.get("description")
    if isinstance(desc_raw, dict) and desc_raw:
        for lang in ("fr", "en", "it", "es", "de"):
            text = _localized_label(desc_raw, lang)
            if text:
                lines.append(f"Description ({lang.upper()}) : {text}")
    else:
        description = _localized_label(product.get("description"))
        if description:
            lines.append(f"Description : {description}")

    discount = product.get("flash_sales_discount")
    if discount not in (None, "", 0):
        lines.append(f"Promo flash : {discount}")

    lines.append("")
    lines.append("Variantes détaillées :")
    variants = product.get("variants")
    if isinstance(variants, list) and variants:
        for index, variant in enumerate(variants, start=1):
            if not isinstance(variant, dict):
                continue
            item = variant.get("item") if isinstance(variant.get("item"), dict) else {}
            color = item.get("color") if isinstance(item.get("color"), dict) else {}
            color_label = _localized_label(color.get("labels")) or str(color.get("reference") or "")
            size = str(item.get("size") or "")
            stock_qty = variant.get("stock_qty", "?")
            in_stock = "oui" if variant.get("in_stock") else "non"
            active = "oui" if variant.get("is_active") else "non"
            variant_type = str(variant.get("type") or "ITEM")
            pack_info = _format_variant_pack(variant) if variant_type == "PACK" else ""
            price_sale = variant.get("price_sale")
            price_value = ""
            if isinstance(price_sale, dict):
                unit = price_sale.get("unit")
                if isinstance(unit, dict) and unit.get("value") is not None:
                    price_value = f"{unit['value']} {unit.get('currency', 'EUR')}"
            weight = variant.get("weight")
            weight_value = ""
            if isinstance(weight, (int, float)) and weight > 0:
                weight_value = f" — poids {weight:.2f} kg"
            variant_images = variant.get("images")
            image_count = 0
            if isinstance(variant_images, dict):
                image_count = len(_image_urls({"images": variant_images}))
            image_value = f" — {image_count} image(s)" if image_count else ""
            lines.append(
                f"  {index}. [{variant_type}] {color_label} / {size} — stock {stock_qty} — "
                f"dispo {in_stock} — actif {active}"
                + (f" — paquet {pack_info}" if pack_info else "")
                + (f" — prix {price_value}" if price_value else "")
                + weight_value
                + image_value
            )
    else:
        lines.append("  (aucune variante)")

    image_urls = _image_urls(product)
    lines.append("")
    lines.append(f"Images ({len(image_urls)}) :")
    if image_urls:
        for url in image_urls[:8]:
            lines.append(f"  - {url}")
        if len(image_urls) > 8:
            lines.append(f"  … +{len(image_urls) - 8} image(s)")
    else:
        lines.append("  (aucune image)")

    return "\n".join(lines)


def extract_products_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    for key in ("products", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_products_list(value)
            if nested:
                return nested

    return []


def extract_pagination(payload: dict[str, Any]) -> dict[str, int]:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return {}

    result: dict[str, int] = {}
    for key in ("current_page", "last_page", "per_page", "total"):
        value = meta.get(key)
        if value is not None:
            try:
                result[key] = int(value)
            except (TypeError, ValueError):
                continue
    return result


def has_more_pages(pagination: dict[str, int], page: int, batch_size: int) -> bool:
    if batch_size == 0:
        return False

    last_page = pagination.get("last_page")
    if last_page is not None:
        return page < last_page

    total = pagination.get("total")
    per_page = pagination.get("per_page") or batch_size
    if total is not None:
        return page * per_page < total

    return batch_size >= per_page


class PfsClient:
    def __init__(self, session: PfsSession) -> None:
        self.session = session
        self._client = create_http_session(
            timeout=60.0,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Authorization": f"Bearer {session.access_token}",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PfsClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def list_products_page(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        status: str = DEFAULT_PRODUCT_STATUS,
    ) -> dict[str, Any]:
        response = _get_with_retries(
            self._client,
            PFS_LIST_PRODUCTS_URL,
            params={
                "page": page,
                "per_page": per_page,
                "status": status,
            },
        )

        if response.status_code >= 400:
            detail = response.text[:300]
            raise PfsApiError(
                f"Erreur API compte source (HTTP {response.status_code}) : {detail}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PfsApiError("Réponse API compte source invalide (JSON attendu).") from exc

        if not isinstance(payload, dict):
            raise PfsApiError("Réponse API compte source inattendue.")

        return payload

    def list_variants_page(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        status: str = DEFAULT_VARIANTS_STATUS,
    ) -> dict[str, Any]:
        response = _get_with_retries(
            self._client,
            PFS_LIST_VARIANTS_URL,
            params={
                "page": page,
                "per_page": per_page,
                "status": status,
            },
        )

        if response.status_code >= 400:
            detail = response.text[:300]
            raise PfsApiError(
                f"Erreur API compte source (HTTP {response.status_code}) : {detail}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PfsApiError("Réponse API compte source invalide (JSON attendu).") from exc

        if not isinstance(payload, dict):
            raise PfsApiError("Réponse API compte source inattendue.")

        return payload

    def fetch_all_list_variants(
        self,
        *,
        status: str = DEFAULT_VARIANTS_STATUS,
        per_page: int = DEFAULT_PER_PAGE,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        variants_by_id: dict[str, list[dict[str, Any]]] = {}
        raw_pages: list[dict[str, Any]] = []
        page = 1
        total_expected = 0
        loaded = 0

        while True:
            payload = self.list_variants_page(page=page, per_page=per_page, status=status)
            raw_pages.append(payload)
            batch = extract_products_list(payload)
            pagination = extract_pagination(payload)

            if pagination.get("total"):
                total_expected = pagination["total"]

            for product in batch:
                product_id = _first_str(product, "id")
                variants = product.get("variants")
                if product_id and isinstance(variants, list):
                    variants_by_id[product_id] = variants
                    loaded += 1

            if on_progress:
                total = total_expected or max(loaded, 1)
                last_page = pagination.get("last_page")
                page_label = f"{page}/{last_page}" if last_page else str(page)
                on_progress(
                    loaded,
                    total,
                    f"listVariants {page_label} — {loaded}/{total} produits",
                )

            if not batch:
                break

            if not has_more_pages(pagination, page, len(batch)):
                break

            page += 1

        if on_progress:
            on_progress(loaded, loaded or 1, "listVariants terminé")

        return variants_by_id, raw_pages

    def fetch_all_products(
        self,
        *,
        status: str = DEFAULT_PRODUCT_STATUS,
        per_page: int = DEFAULT_PER_PAGE,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        products: list[dict[str, Any]] = []
        raw_pages: list[dict[str, Any]] = []
        page = 1
        total_expected = 0

        while True:
            payload = self.list_products_page(page=page, per_page=per_page, status=status)
            raw_pages.append(payload)
            batch = extract_products_list(payload)
            pagination = extract_pagination(payload)

            if pagination.get("total"):
                total_expected = pagination["total"]

            products.extend(batch)

            if on_progress:
                total = total_expected or max(len(products), 1)
                last_page = pagination.get("last_page")
                page_label = f"{page}/{last_page}" if last_page else str(page)
                on_progress(
                    len(products),
                    total,
                    f"Page {page_label} — {len(products)}/{total} produits",
                )

            if not batch:
                break

            if not has_more_pages(pagination, page, len(batch)):
                break

            page += 1

        if on_progress:
            on_progress(len(products), len(products) or 1, "Liste produits terminée")

        return products, raw_pages

    def get_product(self, product_id: str) -> dict[str, Any]:
        detail = fetch_product_detail(self.session.access_token, product_id)
        if detail is None:
            raise PfsApiError(f"Produit introuvable ou inaccessible : {product_id}")
        return detail

    def enrich_products(
        self,
        products: list[dict[str, Any]],
        *,
        max_workers: int = DEFAULT_ENRICH_CONCURRENCY,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> list[dict[str, Any]]:
        if not products:
            return []

        # Skip déjà enrichis (évite de recharger à chaque envoi)
        pending_indices = [
            index
            for index, product in enumerate(products)
            if not product.get("detail_loaded")
            or not product.get("variants_loaded")
        ]
        if not pending_indices:
            if on_progress:
                on_progress(len(products), len(products), "Enrichissement déjà à jour")
            return products

        total = len(pending_indices)
        token = self.session.access_token
        result = list(products)

        def fetch_enrichment(index: int) -> tuple[int, dict[str, Any]]:
            return index, enrich_one_product(result[index], token)

        def missing_summary(items: list[dict[str, Any]]) -> str:
            missing_detail = sum(1 for item in items if not item.get("detail_loaded"))
            missing_variants = sum(
                1 for item in items if not item.get("variants_loaded")
            )
            parts: list[str] = []
            if missing_detail:
                parts.append(f"{missing_detail} sans détail")
            if missing_variants:
                parts.append(f"{missing_variants} sans variantes")
            return ", ".join(parts)

        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(fetch_enrichment, index): index
                for index in pending_indices
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    result_index, merged = future.result()
                    result[result_index] = merged
                except PfsApiError:
                    raise
                except Exception:
                    pass

                completed += 1
                if on_progress:
                    suffix = missing_summary(result)
                    message = f"Enrichissement : {completed}/{total}"
                    if suffix:
                        message += f" — {suffix}"
                    on_progress(completed, total, message)

        retry_indices = [
            index
            for index in pending_indices
            if not result[index].get("detail_loaded")
            or not result[index].get("variants_loaded")
        ]
        if retry_indices:
            if on_progress:
                on_progress(
                    0,
                    len(retry_indices),
                    f"Nouvelle tentative pour {len(retry_indices)} produit(s)…",
                )
            time.sleep(DEFAULT_FETCH_RETRY_DELAY)
            for offset, index in enumerate(retry_indices, start=1):
                try:
                    result[index] = enrich_one_product(result[index], token)
                except PfsApiError:
                    raise
                except Exception:
                    pass
                if on_progress:
                    on_progress(
                        offset,
                        len(retry_indices),
                        f"Nouvelle tentative : {offset}/{len(retry_indices)}",
                    )

        if on_progress:
            suffix = missing_summary(result)
            message = "Enrichissement terminé"
            if suffix:
                message += f" ({suffix})"
            on_progress(len(products), len(products), message)

        return result

    def fetch_catalog(
        self,
        *,
        statuses: tuple[str, ...] | None = None,
        status: str | None = None,
        variants_status: str = DEFAULT_VARIANTS_STATUS,
        per_page: int = DEFAULT_PER_PAGE,
        enrich_details: bool = False,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        if status is not None:
            catalog_statuses = (status,)
        elif statuses is not None:
            catalog_statuses = statuses
        else:
            catalog_statuses = PFS_CATALOG_PRODUCT_STATUSES

        products: list[dict[str, Any]] = []
        raw_pages: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for catalog_status in catalog_statuses:
            batch, pages = self.fetch_all_products(
                status=catalog_status,
                per_page=per_page,
                on_progress=on_progress,
            )
            raw_pages.extend(pages)
            for product in batch:
                product_id = _first_str(product, "id")
                if product_id:
                    if product_id in seen_ids:
                        continue
                    seen_ids.add(product_id)
                products.append(product)

        variants_by_id, raw_variant_pages = self.fetch_all_list_variants(
            status=variants_status,
            per_page=per_page,
            on_progress=on_progress,
        )
        products = merge_list_variants_index(products, variants_by_id)

        if enrich_details and products:
            if on_progress:
                on_progress(
                    0,
                    len(products),
                    "Enrichissement /products/{id} (compositions, collection…)…",
                )
            products = self.enrich_products(products, on_progress=on_progress)

        return products, raw_pages, raw_variant_pages
