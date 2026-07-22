from __future__ import annotations

import re
from typing import Any

# Aligné sur wholesaler/backend-backoffice-seller/src/shootings/product-dimensions.ts
PACKAGE_DIMENSIONS_PATTERN = re.compile(
    r"^\d+(?:[.,]\d+)?\s*[x*]\s*\d+(?:[.,]\d+)?\s*[x*]\s*\d+(?:[.,]\d+)?(?:\s*cm)?$",
    re.IGNORECASE,
)
SHOE_CATEGORY_EXTRA_IDS = frozenset({"130106", "130306", "130405"})
DEFAULT_SHOE_DIMENSIONS = "60x40x50"


def is_shoe_category(category_id: str | int | None) -> bool:
    value = str(category_id or "").strip()
    if not value:
        return False
    return value.startswith("14") or value in SHOE_CATEGORY_EXTRA_IDS


def has_valid_shoe_dimensions(dimensions: str) -> bool:
    return bool(PACKAGE_DIMENSIONS_PATTERN.match(str(dimensions or "").strip()))


def _first_str(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def dimensions_from_product(product: dict[str, Any]) -> str:
    for key in ("dimensions", "package_dimensions", "box_dimensions", "dimension"):
        candidate = _first_str(product.get(key))
        if candidate and has_valid_shoe_dimensions(candidate):
            return candidate

    length = _first_str(
        product.get("length"),
        product.get("package_length"),
        product.get("box_length"),
    )
    width = _first_str(
        product.get("width"),
        product.get("package_width"),
        product.get("box_width"),
    )
    height = _first_str(
        product.get("height"),
        product.get("package_height"),
        product.get("box_height"),
    )
    if length and width and height:
        candidate = f"{length}x{width}x{height}"
        if has_valid_shoe_dimensions(candidate):
            return candidate
    return ""


def resolve_mel_dimensions(
    product: dict[str, Any],
    category_id: str,
    *,
    reference: str = "",
    notes: list[str] | None = None,
) -> str:
    """Dimensions MEL : vide sauf chaussures (défaut non bloquant si absentes côté PFS)."""
    found = dimensions_from_product(product)
    if found:
        return found
    if not is_shoe_category(category_id):
        return ""
    if notes is not None:
        notes.append(
            f"{reference or '?'} : dimensions colis absentes "
            f"→ défaut {DEFAULT_SHOE_DIMENSIONS}"
        )
    return DEFAULT_SHOE_DIMENSIONS
