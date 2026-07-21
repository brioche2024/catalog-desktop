from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import SESSION_DIR


def _localized_label(value: Any, lang: str = "fr") -> str:
    if isinstance(value, dict):
        text = value.get(lang) or value.get("en")
        if text is not None and str(text).strip():
            return str(text)
    if value is not None and str(value).strip():
        return str(value)
    return ""


@dataclass
class CategoryMappingEntry:
    id: str
    label: str
    l1_id: str = ""
    l2_id: str = ""
    pfs_category: str = ""
    gender: str = ""


def _normalize_gender(value: Any) -> str:
    if value is None:
        return "*"
    if isinstance(value, dict):
        value = value.get("reference") or value.get("name") or value
    key = str(value).strip().upper()
    aliases = {
        "MAN": "MAN",
        "MEN": "MAN",
        "HOMME": "MAN",
        "WOMAN": "WOMAN",
        "WOMEN": "WOMAN",
        "FEMME": "WOMAN",
        "UNISEX": "UNISEX",
        "KID": "KID",
        "KIDS": "KID",
        "ENFANT": "KID",
    }
    return aliases.get(key, key or "*")


def pfs_category_label(product: dict[str, Any]) -> str:
    category = product.get("category") if isinstance(product.get("category"), dict) else {}
    return (_localized_label(category.get("labels")) or str(category.get("id") or "")).strip()


def mapping_key_for_product(product: dict[str, Any]) -> str:
    category = pfs_category_label(product) or "Sans catégorie"
    gender = _normalize_gender(product.get("gender"))
    return f"{category}|{gender}"


def mapping_key_fallbacks(product: dict[str, Any]) -> list[str]:
    category = pfs_category_label(product) or "Sans catégorie"
    gender = _normalize_gender(product.get("gender"))
    keys = [f"{category}|{gender}"]
    if gender != "*":
        keys.append(f"{category}|*")
    return keys


class CategoryMappingStore:
    def __init__(self, path: Path | None = None, id_vendeur: int | None = None) -> None:
        suffix = f"_{id_vendeur}" if id_vendeur is not None else ""
        self.path = path or (SESSION_DIR / f"category_mapping{suffix}.json")
        self.entries: dict[str, CategoryMappingEntry] = {}
        self.load()

    def load(self) -> None:
        self.entries = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            if not isinstance(value, dict) or not value.get("id"):
                continue
            self.entries[str(key)] = CategoryMappingEntry(
                id=str(value["id"]),
                label=str(value.get("label") or ""),
                l1_id=str(value.get("l1_id") or ""),
                l2_id=str(value.get("l2_id") or ""),
                pfs_category=str(value.get("pfs_category") or ""),
                gender=str(value.get("gender") or ""),
            )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {
                "id": entry.id,
                "label": entry.label,
                "l1_id": entry.l1_id,
                "l2_id": entry.l2_id,
                "pfs_category": entry.pfs_category,
                "gender": entry.gender,
            }
            for key, entry in sorted(self.entries.items())
        }
        self.path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def get(self, key: str) -> CategoryMappingEntry | None:
        return self.entries.get(key)

    def resolve_product(self, product: dict[str, Any]) -> CategoryMappingEntry | None:
        for key in mapping_key_fallbacks(product):
            entry = self.get(key)
            if entry:
                return entry
        return None

    def set_entry(self, key: str, entry: CategoryMappingEntry) -> None:
        self.entries[key] = entry
        self.save()

    def delete_entry(self, key: str) -> bool:
        if key not in self.entries:
            return False
        del self.entries[key]
        self.save()
        return True

    def sorted_entries(self) -> list[tuple[str, CategoryMappingEntry]]:
        return sorted(self.entries.items(), key=lambda item: item[0].lower())

    def missing_keys_for_products(self, products: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        """Retourne les clés manquantes uniques avec un produit exemple."""
        missing: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for product in products:
            key = mapping_key_for_product(product)
            if key in seen:
                continue
            seen.add(key)
            if self.resolve_product(product) is None:
                missing.append((key, product))
        return missing


def category_options_from_reference(
    reference_data: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Retourne (L1, L2, L3) depuis get-reference-data."""
    l1 = [
        item
        for item in (reference_data.get("categories") or [])
        if isinstance(item, dict) and item.get("id") is not None
    ]
    l2 = [
        item
        for item in (reference_data.get("sousCategories") or [])
        if isinstance(item, dict) and item.get("id") is not None
    ]
    l3 = [
        item
        for item in (reference_data.get("sousSousCategories") or [])
        if isinstance(item, dict) and item.get("id") is not None
    ]
    return l1, l2, l3


def children_of(
    items: list[dict[str, Any]],
    parent_id: str,
) -> list[dict[str, Any]]:
    parent = str(parent_id)
    return [
        item
        for item in items
        if str(item.get("parentId") or "") == parent
    ]
