from __future__ import annotations

import re
import unicodedata
from typing import Any

from .category_mapping import CategoryMappingStore, mapping_key_for_product
from .efashion_client import EfashionApiError, EfashionClient
from .pfs_client import infer_vendu_par_label, product_weight_kg, resolve_efashion_vendu_par


class MelMappingError(Exception):
    pass


DEFAULT_WEIGHT_KG = 0.05


def _localized_label(value: Any, lang: str = "fr") -> str:
    if isinstance(value, dict):
        text = value.get(lang) or value.get(lang.lower()) or value.get(lang.upper())
        if text is not None and str(text).strip():
            return str(text)
        # Fallback historique : FR manquant → EN
        if lang == "fr":
            text = value.get("en") or value.get("EN")
            if text is not None and str(text).strip():
                return str(text)
    elif value is not None and str(value).strip() and lang == "fr":
        return str(value)
    return ""


# Langues description PFS → champs MEL EFashion
_DESCRIPTION_LANGS: tuple[tuple[str, str], ...] = (
    ("fr", "descriptionFr"),
    ("en", "descriptionEn"),
    ("it", "descriptionIt"),
    ("es", "descriptionEs"),
    ("de", "descriptionDe"),
)


def _product_descriptions(product: dict[str, Any]) -> dict[str, str]:
    """Extrait FR/EN/IT/ES/DE depuis description (ou labels en fallback FR)."""
    source = product.get("description")
    if not isinstance(source, dict):
        source = {}
    out: dict[str, str] = {}
    for lang, field in _DESCRIPTION_LANGS:
        text = _localized_label(source, lang)
        # Fallback titre/libellé uniquement pour le FR si pas de description
        if not text and lang == "fr":
            text = _localized_label(product.get("labels"), "fr")
        out[field] = text
    return out

PFS_COLOR_ALIASES: dict[str, list[str]] = {
    "WHITE": ["Blanc", "White"],
    "BLACK": ["Noir", "Black"],
    "GRAY": ["Gris", "Gray", "Grey"],
    "GREY": ["Gris", "Gray", "Grey"],
    "GREEN": ["Vert", "Green"],
    "MARRON": ["Marron", "Brown"],
    "BROWN": ["Marron", "Brown"],
    "BLUE": ["Bleu", "Blue"],
    "RED": ["Rouge", "Red"],
    "PINK": ["Rose", "Pink"],
    "YELLOW": ["Jaune", "Yellow"],
    "ORANGE": ["Orange"],
    "PURPLE": ["Violet", "Purple"],
    "GOLD": ["Or", "Gold"],
    "SILVER": ["Argent", "Silver"],
    "BEIGE": ["Beige"],
    "NAVY": ["Marine", "Navy"],
    "KHAKI": ["Kaki", "Khaki"],
}


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKD", text or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().strip()
    value = re.sub(r"\s+", " ", value)
    return value


def _first_str(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _match_by_label(
    items: list[dict[str, Any]],
    needle: str,
    *label_keys: str,
    exact_only: bool = False,
) -> dict[str, Any] | None:
    target = _normalize(needle)
    if not target:
        return None
    for item in items:
        for key in label_keys:
            label = _first_str(item.get(key))
            if label and _normalize(label) == target:
                return item
    if exact_only:
        return None
    for item in items:
        for key in label_keys:
            label = _first_str(item.get(key))
            if label and target in _normalize(label):
                return item
    return None


class MelMapper:
    def __init__(
        self,
        client: EfashionClient,
        reference_data: dict[str, Any],
        category_mapping: CategoryMappingStore | None = None,
    ) -> None:
        self.client = client
        self.reference_data = reference_data
        self.category_mapping = category_mapping
        self.id_vendeur = client.session.id_vendeur
        self.created_notes: list[str] = []

    def refresh(self) -> None:
        self.reference_data = self.client.get_reference_data()

    def map_products(self, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
        mapped: list[dict[str, Any]] = []
        errors: list[str] = []
        for product in products:
            try:
                mapped.append(self.map_one(product))
            except MelMappingError as exc:
                errors.append(str(exc))
        if errors:
            raise MelMappingError("\n".join(errors[:10]))
        return mapped

    def map_one(self, product: dict[str, Any]) -> dict[str, Any]:
        reference = _first_str(product.get("reference"), product.get("sku"))
        if not reference:
            raise MelMappingError("Produit sans référence.")

        price = product.get("unit_price")
        if not isinstance(price, (int, float)) or price <= 0:
            raise MelMappingError(f"{reference} : prix HT manquant ou invalide.")

        brand = self._resolve_brand(product)
        category_id = self._resolve_category(product)
        collection_id = self._resolve_collection(product)
        provenance_id = self._resolve_provenance(product)
        colors = self._resolve_colors(product)
        compositions = self._resolve_compositions(product)
        vendu_par = self._resolve_vendu_par(product)
        declinaison_id = self._ensure_declinaison(product)
        pack_id = "" if vendu_par == "tailles" else self._ensure_pack(product)
        weight = product_weight_kg(product)
        if weight is None or weight <= 0:
            weight = DEFAULT_WEIGHT_KG
            self.created_notes.append(
                f"{reference} : poids absent → défaut {DEFAULT_WEIGHT_KG} kg"
            )

        description = _product_descriptions(product)

        return {
            "reference": reference,
            "marque": str(brand),
            "poids": f"{weight:.3f}".rstrip("0").rstrip("."),
            "categorie": str(category_id),
            "venduPar": vendu_par,
            "collection": str(collection_id),
            "paysOrigine": str(provenance_id),
            "taillePaquet": str(declinaison_id),
            "quantitePaquet": str(pack_id) if pack_id else "",
            "stock": "",
            "prix": str(price),
            "prixReduit": "",
            "dateRemise": "",
            "pourcentageRemise": "",
            "dimensions": "",
            "minimumCommande": "1",
            "couleurs": colors,
            "descriptionFr": description["descriptionFr"],
            "descriptionEn": description["descriptionEn"],
            "descriptionIt": description["descriptionIt"],
            "descriptionEs": description["descriptionEs"],
            "descriptionDe": description["descriptionDe"],
            "compositions": compositions,
        }

    def _resolve_brand(self, product: dict[str, Any]) -> int:
        marques = self.reference_data.get("marques") or []
        brand_data = product.get("brand") if isinstance(product.get("brand"), dict) else {}
        brand_name = _first_str(brand_data.get("name"))
        if brand_name:
            match = _match_by_label(marques, brand_name, "label")
            if match and match.get("id") is not None:
                return int(match["id"])
        for marque in marques:
            if marque.get("defaut"):
                return int(marque["id"])
        if marques:
            return int(marques[0]["id"])
        raise MelMappingError("Aucune marque EFashion sur ce compte vendeur.")

    def _resolve_category(self, product: dict[str, Any]) -> str:
        if self.category_mapping is not None:
            entry = self.category_mapping.resolve_product(product)
            if entry and entry.id:
                return str(entry.id)
            key = mapping_key_for_product(product)
            raise MelMappingError(
                f"{_first_str(product.get('reference'))} : "
                f"catégorie non mappée ({key}). Choisissez la feuille EFashion."
            )

        # Fallback legacy : L1 seulement (déconseillé)
        categories = self.reference_data.get("categories") or []
        category = product.get("category") if isinstance(product.get("category"), dict) else {}
        label = _localized_label(category.get("labels"))
        if label:
            match = _match_by_label(categories, label, "label")
            if match and match.get("id") is not None:
                return str(match["id"])
        normalized = _normalize(label)
        if any(token in normalized for token in ("montre", "watch", "bijou", "accessoir")):
            accessoires = _match_by_label(categories, "Accessoires", "label")
            if accessoires and accessoires.get("id") is not None:
                return str(accessoires["id"])
        if categories:
            return str(categories[0]["id"])
        raise MelMappingError("Aucune catégorie EFashion disponible.")

    def _resolve_collection(self, product: dict[str, Any]) -> str:
        collections = self.reference_data.get("collections") or []
        collection = product.get("collection") if isinstance(product.get("collection"), dict) else {}
        label = _localized_label(collection.get("labels")) or _first_str(
            collection.get("reference")
        )
        normalized = _normalize(label)

        if "printemps" in normalized or "ete" in normalized or normalized.startswith("pe"):
            match = _match_by_label(collections, "Printemps / Été", "label")
            if match:
                return str(match["id"])
        if "automne" in normalized or "hiver" in normalized or normalized.startswith("ah"):
            match = _match_by_label(collections, "Automne / Hiver", "label")
            if match:
                return str(match["id"])

        match = _match_by_label(collections, label, "label") if label else None
        if match:
            return str(match["id"])

        toutes = _match_by_label(collections, "Toutes les saisons", "label")
        if toutes:
            return str(toutes["id"])
        if collections:
            return str(collections[0]["id"])
        raise MelMappingError("Aucune collection EFashion disponible.")

    def _resolve_provenance(self, product: dict[str, Any]) -> str:
        provenances = self.reference_data.get("provenances") or []
        country = _first_str(product.get("country_of_manufacture")).upper()
        mapping = {
            "CN": "Chine",
            "CHINA": "Chine",
            "FR": "France",
            "FRANCE": "France",
            "IT": "Italie",
            "ITALY": "Italie",
            "TR": "Turquie",
            "TURKEY": "Turquie",
            "IN": "Inde",
            "INDIA": "Inde",
            "PT": "Portugal",
            "ES": "Espagne",
            "DE": "Allemagne",
            "BD": "Bangladesh",
            "VN": "Vietnam",
            "PK": "Pakistan",
        }
        label = mapping.get(country, country)
        if label:
            match = _match_by_label(provenances, label, "label")
            if match:
                return str(match["id"])
        chine = _match_by_label(provenances, "Chine", "label")
        if chine:
            return str(chine["id"])
        if provenances:
            return str(provenances[0]["id"])
        raise MelMappingError("Aucune provenance EFashion disponible.")

    def _color_candidates(self, raw: str) -> list[str]:
        key = raw.strip().upper()
        aliases = list(PFS_COLOR_ALIASES.get(key, []))
        if raw.strip() and key not in PFS_COLOR_ALIASES:
            aliases.insert(0, raw.strip().title())
            aliases.append(raw.strip())
        elif raw.strip() and key in PFS_COLOR_ALIASES:
            aliases.append(raw.strip().title())
        # unique preserve order
        seen: set[str] = set()
        result: list[str] = []
        for name in aliases:
            norm = _normalize(name)
            if norm and norm not in seen:
                seen.add(norm)
                result.append(name)
        return result or [raw.strip().title() or raw.strip()]

    def _resolve_colors(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        colors_raw = _first_str(product.get("colors"), product.get("couleurs"))
        names: list[str] = []
        if colors_raw:
            names = [part.strip() for part in colors_raw.replace(",", ";").split(";") if part.strip()]

        if not names:
            variants = product.get("variants") if isinstance(product.get("variants"), list) else []
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                item = variant.get("item") if isinstance(variant.get("item"), dict) else {}
                color = item.get("color") if isinstance(item.get("color"), dict) else {}
                label = _localized_label(color.get("labels")) or _first_str(color.get("reference"))
                if label and label not in names:
                    names.append(label)

        if not names:
            raise MelMappingError(
                f"{_first_str(product.get('reference'))} : aucune couleur trouvée."
            )

        resolved: list[dict[str, Any]] = []
        for index, name in enumerate(names):
            color_id, color_label = self._ensure_color(name)
            resolved.append(
                {
                    "id": color_id,
                    "nom": color_label,
                    "isMain": index == 0,
                }
            )
        return resolved

    def _ensure_color(self, raw_name: str) -> tuple[int, str]:
        couleurs = self.reference_data.get("couleurs") or []
        for candidate in self._color_candidates(raw_name):
            match = _match_by_label(
                couleurs, candidate, "francais", "anglais", exact_only=True
            )
            if match and match.get("id") is not None:
                return int(match["id"]), _first_str(match.get("francais"), candidate)

        display_name = self._color_candidates(raw_name)[0]
        color_id = self._find_or_create_global_color(display_name)
        self._add_color_to_vendor(color_id)
        self.refresh()
        self.created_notes.append(f"Couleur créée/liée : {display_name}")
        return color_id, display_name

    def _find_or_create_global_color(self, name: str) -> int:
        query = """
        query {
          allCouleursDefaut {
            id_couleur
            couleur_FR
            couleur_EN
          }
        }
        """
        try:
            data = self.client.graphql(query)
            colors = data.get("allCouleursDefaut") or []
            match = _match_by_label(
                colors, name, "couleur_FR", "couleur_EN", exact_only=True
            )
            if match and match.get("id_couleur") is not None:
                return int(match["id_couleur"])
        except EfashionApiError:
            pass

        mutation = """
        mutation($input: CreateCouleurDefautInput!) {
          createCouleur(input: $input) {
            id_couleur
            couleur_FR
          }
        }
        """
        created = self.client.graphql(
            mutation,
            {"input": {"couleur_FR": name, "couleur_EN": name, "defaut": 0}},
        )
        color = created.get("createCouleur") or {}
        if color.get("id_couleur") is None:
            raise MelMappingError(f"Impossible de créer la couleur {name}.")
        return int(color["id_couleur"])

    def _add_color_to_vendor(self, color_id: int) -> None:
        if self.id_vendeur is None:
            raise MelMappingError("id_vendeur EFashion manquant.")
        mutation = """
        mutation($input: CreateCouleursVendeurInput!) {
          addCouleurToVendeur(input: $input) {
            id_couleur
          }
        }
        """
        try:
            self.client.graphql(
                mutation,
                {"input": {"id_vendeur": self.id_vendeur, "id_couleur": color_id}},
            )
        except EfashionApiError as exc:
            # déjà liée au vendeur → ok
            if "exist" not in str(exc).lower() and "déjà" not in str(exc).lower():
                # parfois unique constraint: ignore if already present
                if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                    raise

    def _resolve_compositions(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        materials = product.get("material_composition")
        if not isinstance(materials, list) or not materials:
            raise MelMappingError(
                f"{_first_str(product.get('reference'))} : composition manquante "
                "(enrichissez le produit avant envoi)."
            )

        compositions_ref = [
            item
            for item in (self.reference_data.get("compositions") or [])
            if item.get("type") == "composition"
        ]
        localisations_ref = [
            item
            for item in (self.reference_data.get("compositions") or [])
            if item.get("type") == "localisation"
        ]
        localisation = _match_by_label(localisations_ref, "Produit", "libelle") or _match_by_label(
            localisations_ref, "Autres", "libelle"
        )
        if not localisation:
            raise MelMappingError("Localisation composition EFashion introuvable.")

        result: list[dict[str, Any]] = []
        for material in materials:
            if not isinstance(material, dict):
                continue
            label = _localized_label(material.get("labels")) or _first_str(
                material.get("reference")
            )
            if not label:
                continue
            match = _match_by_label(compositions_ref, label, "libelle")
            if not match:
                # tentative partielle (ex. Metal / Métal)
                match = next(
                    (
                        item
                        for item in compositions_ref
                        if _normalize(label) in _normalize(_first_str(item.get("libelle")))
                        or _normalize(_first_str(item.get("libelle"))) in _normalize(label)
                    ),
                    None,
                )
            if not match:
                raise MelMappingError(
                    f"{_first_str(product.get('reference'))} : "
                    f"composition « {label} » introuvable dans EFashion."
                )
            percentage = material.get("percentage")
            result.append(
                {
                    "id_composition": int(match["id"]),
                    "materiau": _first_str(match.get("libelle"), label),
                    "localisation": str(localisation["id"]),
                    "pourcentage": int(percentage) if percentage is not None else 100,
                }
            )

        if not result:
            raise MelMappingError(
                f"{_first_str(product.get('reference'))} : composition invalide."
            )
        return result

    def _resolve_vendu_par(self, product: dict[str, Any]) -> str:
        return resolve_efashion_vendu_par(product)

    def _sizes_from_product(self, product: dict[str, Any]) -> list[str]:
        sizes_raw = _first_str(product.get("sizes"), product.get("tailles"))
        sizes = [part.strip() for part in sizes_raw.replace(";", ",").split(",") if part.strip()]
        if sizes:
            return sizes

        variants = product.get("variants") if isinstance(product.get("variants"), list) else []
        found: list[str] = []
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            item = variant.get("item") if isinstance(variant.get("item"), dict) else {}
            size = _first_str(item.get("size"))
            if size and size not in found:
                found.append(size)
            for pack in variant.get("packs") or []:
                if not isinstance(pack, dict):
                    continue
                for size_entry in pack.get("sizes") or []:
                    if not isinstance(size_entry, dict):
                        continue
                    size = _first_str(size_entry.get("size"))
                    if size and size not in found:
                        found.append(size)
        return found or ["TU"]

    def _ensure_declinaison(self, product: dict[str, Any]) -> int:
        sizes = self._sizes_from_product(product)
        wanted = ",".join(sizes)
        declinaisons = self.reference_data.get("declinaisons") or []
        for item in declinaisons:
            existing = _first_str(item.get("tailles"), item.get("label")).replace(" ", "")
            if _normalize(existing.replace("-", ",")) == _normalize(wanted.replace("-", ",")):
                return int(item["id"])
            if _normalize(_first_str(item.get("label"))) == _normalize(wanted):
                return int(item["id"])

        if self.id_vendeur is None:
            raise MelMappingError("id_vendeur EFashion manquant.")

        titre = "-".join(sizes)
        fields: dict[str, Any] = {"titre": titre, "id_vendeur": self.id_vendeur}
        for index, size in enumerate(sizes[:12], start=1):
            fields[f"d{index}_FR"] = size
        for index in range(len(sizes) + 1, 13):
            fields[f"d{index}_FR"] = None

        mutation = """
        mutation($input: CreateDeclinaisonInput!) {
          createDeclinaison(createDeclinaisonInput: $input) {
            id_declinaison
            titre
          }
        }
        """
        created = self.client.graphql(mutation, {"input": fields})
        decl = created.get("createDeclinaison") or {}
        if decl.get("id_declinaison") is None:
            raise MelMappingError(f"Impossible de créer la déclinaison {titre}.")
        self.refresh()
        self.created_notes.append(f"Déclinaison créée : {titre}")
        return int(decl["id_declinaison"])

    def _pack_quantities(self, product: dict[str, Any]) -> list[int]:
        variants = product.get("variants") if isinstance(product.get("variants"), list) else []
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            if str(variant.get("type") or "").upper() != "PACK":
                continue
            qtys: list[int] = []
            for pack in variant.get("packs") or []:
                if not isinstance(pack, dict):
                    continue
                for size_entry in pack.get("sizes") or []:
                    if not isinstance(size_entry, dict):
                        continue
                    qty = size_entry.get("qty")
                    if isinstance(qty, (int, float)) and qty > 0:
                        qtys.append(int(qty))
            if qtys:
                return qtys
            pieces = variant.get("pieces")
            if isinstance(pieces, (int, float)) and pieces > 0:
                return [int(pieces)]

        # ITEM → pack unitaire
        return [1]

    def _ensure_pack(self, product: dict[str, Any]) -> int:
        quantities = self._pack_quantities(product)
        wanted = ",".join(str(qty) for qty in quantities)
        packs = self.reference_data.get("packs") or []
        for pack in packs:
            value = _first_str(pack.get("value"), pack.get("label")).replace(" ", "").replace("-", ",")
            if _normalize(value) == _normalize(wanted):
                return int(pack["id"])

        if self.id_vendeur is None:
            raise MelMappingError("id_vendeur EFashion manquant.")

        titre = "-".join(str(qty) for qty in quantities)
        fields: dict[str, Any] = {"titre": titre, "id_vendeur": self.id_vendeur}
        for index, qty in enumerate(quantities[:12], start=1):
            fields[f"p{index}"] = qty

        mutation = """
        mutation($input: CreatePackInput!) {
          createPack(input: $input) {
            id_pack
            titre
          }
        }
        """
        created = self.client.graphql(mutation, {"input": fields})
        pack = created.get("createPack") or {}
        if pack.get("id_pack") is None:
            raise MelMappingError(f"Impossible de créer le pack {titre}.")
        self.refresh()
        self.created_notes.append(f"Pack créé : {titre}")
        return int(pack["id_pack"])
