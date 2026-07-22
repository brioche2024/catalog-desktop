from __future__ import annotations

from typing import Any

from .efashion_client import EfashionApiError, EfashionClient
from .photo_upload import (
    _color_aliases,
    _efashion_candidates_for_reference,
    _extract_efashion_color,
    _find_product_id_for_color,
    _normalize,
    _single_fiche_product_id,
)


def _is_rupture_stock(value: Any) -> bool:
    return value in (0, "0") or value == 0.0


def _resolve_product_id_for_color(
    client: EfashionClient,
    ef_products: list[dict[str, Any]],
    reference: str,
    color_name: str,
) -> int | None:
    product_id = _find_product_id_for_color(ef_products, reference, color_name)
    if product_id is not None:
        return product_id

    # Après publish : recharger via productsPage (réfs BASE-COULEUR)
    try:
        items = client.find_products_by_reference(reference)
    except EfashionApiError:
        return None
    if not items:
        return None
    if not color_name:
        main = next((item for item in items if item.get("main")), None)
        target = main or items[0]
        return int(target["id"]) if target.get("id") is not None else None

    wanted = _color_aliases(color_name)
    for item in items:
        ef_color = _extract_efashion_color(
            str(item.get("reference") or ""),
            str(item.get("referenceBase") or item.get("reference_base") or ""),
        )
        if ef_color and (
            _normalize(ef_color) in wanted or (_color_aliases(ef_color) & wanted)
        ):
            return int(item["id"])
    # dernier recours : _find sur la liste productsPage normalisée
    return _find_product_id_for_color(items, reference, color_name)


def apply_rupture_stocks_after_publish(
    client: EfashionClient,
    mel_references: list[dict[str, Any]],
    ef_products: list[dict[str, Any]],
) -> list[str]:
    """
    Force stock=0 dans l'input EFashion (table produits_stock).

    Le save MEL ignore stock <= 0 à la création → createProduitStock(value=0)
    est obligatoire pour afficher le badge rupture / l'input à 0.
    """
    errors: list[str] = []
    for mel in mel_references:
        reference = str(mel.get("reference") or "").strip()
        if not reference:
            continue
        vendu_par = str(mel.get("venduPar") or "couleurs").lower()
        couleurs = mel.get("couleurs") or []

        if vendu_par == "couleurs":
            for couleur in couleurs:
                if not isinstance(couleur, dict):
                    continue
                if not _is_rupture_stock(couleur.get("stock")):
                    continue
                color_name = str(couleur.get("nom") or "").strip()
                color_id = couleur.get("id")
                product_id = _resolve_product_id_for_color(
                    client, ef_products, reference, color_name
                )
                if product_id is None:
                    errors.append(
                        f"{reference}/{color_name or '?'} : "
                        "fiche introuvable pour stock rupture."
                    )
                    continue
                if color_id is None:
                    errors.append(
                        f"{reference}/{color_name or '?'} : id couleur MEL manquant."
                    )
                    continue
                try:
                    client.create_produit_stock(
                        product_id,
                        id_couleur=int(color_id),
                        value=0,
                    )
                except EfashionApiError as exc:
                    errors.append(f"{reference}/{color_name} : stock rupture ({exc})")

        elif vendu_par == "tailles":
            for couleur in couleurs:
                if not isinstance(couleur, dict):
                    continue
                taille_stocks = couleur.get("tailleStocks")
                if not isinstance(taille_stocks, list):
                    continue
                color_name = str(couleur.get("nom") or "").strip()
                color_id = couleur.get("id")
                product_id = _resolve_product_id_for_color(
                    client, ef_products, reference, color_name
                )
                if product_id is None or color_id is None:
                    continue
                for entry in taille_stocks:
                    if not isinstance(entry, dict):
                        continue
                    if not _is_rupture_stock(entry.get("quantity")):
                        continue
                    taille = str(entry.get("taille") or "").strip()
                    if not taille:
                        continue
                    try:
                        client.create_produit_stock(
                            product_id,
                            id_couleur=int(color_id),
                            value=0,
                            taille=taille,
                        )
                    except EfashionApiError as exc:
                        errors.append(
                            f"{reference}/{color_name}/{taille} : "
                            f"stock rupture ({exc})"
                        )

        elif vendu_par == "melangees":
            if not _is_rupture_stock(mel.get("stock")):
                continue
            candidates = _efashion_candidates_for_reference(ef_products, reference)
            product_id = _single_fiche_product_id(candidates)
            if product_id is None:
                try:
                    items = client.find_products_by_reference(reference)
                except EfashionApiError:
                    items = []
                if items and items[0].get("id") is not None:
                    product_id = int(items[0]["id"])
            if product_id is None:
                errors.append(f"{reference} : fiche introuvable pour stock rupture.")
                continue
            main_color_id = None
            for couleur in couleurs:
                if isinstance(couleur, dict) and couleur.get("isMain") and couleur.get("id"):
                    main_color_id = int(couleur["id"])
                    break
            if main_color_id is None and couleurs:
                first = couleurs[0]
                if isinstance(first, dict) and first.get("id"):
                    main_color_id = int(first["id"])
            if main_color_id is None:
                errors.append(f"{reference} : id couleur manquant pour stock rupture.")
                continue
            try:
                client.create_produit_stock(
                    product_id,
                    id_couleur=main_color_id,
                    value=0,
                )
            except EfashionApiError as exc:
                errors.append(f"{reference} : stock rupture ({exc})")

    return errors


def apply_stocks_mirror(
    client: EfashionClient,
    mel_references: list[dict[str, Any]],
    ef_products: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Miroir des stocks PFS → EFashion (0 et quantités > 0) via upsert."""
    ef_products = ef_products or []
    errors: list[str] = []
    for mel in mel_references:
        reference = str(mel.get("reference") or "").strip()
        if not reference:
            continue
        vendu_par = str(mel.get("venduPar") or "couleurs").lower()
        couleurs = mel.get("couleurs") or []

        if vendu_par == "couleurs":
            for couleur in couleurs:
                if not isinstance(couleur, dict) or "stock" not in couleur:
                    continue
                try:
                    value = int(couleur.get("stock"))
                except (TypeError, ValueError):
                    continue
                color_name = str(couleur.get("nom") or "").strip()
                color_id = couleur.get("id")
                product_id = _resolve_product_id_for_color(
                    client, ef_products, reference, color_name
                )
                if product_id is None or color_id is None:
                    errors.append(
                        f"{reference}/{color_name or '?'} : fiche/couleur stock introuvable."
                    )
                    continue
                try:
                    client.upsert_produit_stock(
                        product_id,
                        id_couleur=int(color_id),
                        value=value,
                    )
                except EfashionApiError as exc:
                    errors.append(f"{reference}/{color_name} : stock ({exc})")

        elif vendu_par == "tailles":
            for couleur in couleurs:
                if not isinstance(couleur, dict):
                    continue
                taille_stocks = couleur.get("tailleStocks")
                if not isinstance(taille_stocks, list):
                    continue
                color_name = str(couleur.get("nom") or "").strip()
                color_id = couleur.get("id")
                product_id = _resolve_product_id_for_color(
                    client, ef_products, reference, color_name
                )
                if product_id is None or color_id is None:
                    continue
                for entry in taille_stocks:
                    if not isinstance(entry, dict) or "quantity" not in entry:
                        continue
                    try:
                        value = int(entry.get("quantity"))
                    except (TypeError, ValueError):
                        continue
                    taille = str(entry.get("taille") or "").strip()
                    if not taille:
                        continue
                    try:
                        client.upsert_produit_stock(
                            product_id,
                            id_couleur=int(color_id),
                            value=value,
                            taille=taille,
                        )
                    except EfashionApiError as exc:
                        errors.append(
                            f"{reference}/{color_name}/{taille} : stock ({exc})"
                        )

        elif vendu_par == "melangees":
            if mel.get("stock") in (None, ""):
                continue
            try:
                value = int(mel.get("stock"))
            except (TypeError, ValueError):
                continue
            candidates = _efashion_candidates_for_reference(ef_products, reference)
            product_id = _single_fiche_product_id(candidates)
            if product_id is None:
                try:
                    items = client.find_products_by_reference(reference)
                except EfashionApiError:
                    items = []
                if items and items[0].get("id") is not None:
                    product_id = int(items[0]["id"])
            if product_id is None:
                errors.append(f"{reference} : fiche introuvable pour stock.")
                continue
            main_color_id = None
            for couleur in couleurs:
                if isinstance(couleur, dict) and couleur.get("isMain") and couleur.get("id"):
                    main_color_id = int(couleur["id"])
                    break
            if main_color_id is None and couleurs:
                first = couleurs[0]
                if isinstance(first, dict) and first.get("id"):
                    main_color_id = int(first["id"])
            if main_color_id is None:
                errors.append(f"{reference} : id couleur manquant pour stock.")
                continue
            try:
                client.upsert_produit_stock(
                    product_id,
                    id_couleur=main_color_id,
                    value=value,
                )
            except EfashionApiError as exc:
                errors.append(f"{reference} : stock ({exc})")

    return errors
