from __future__ import annotations

from typing import Any, Callable

from .category_mapping import CategoryMappingStore
from .efashion_client import EfashionApiError, EfashionClient
from .mel_mapper import MelMapper, MelMappingError, _product_descriptions
from .mel_service import MelSyncError, _enrich_selected_products, _product_reference
from .pfs_client import product_weight_kg
from .session_store import EfashionSession, PfsSession


def update_products_on_efashion(
    efashion_session: EfashionSession,
    products: list[dict],
    *,
    pfs_session: PfsSession,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, object]:
    """Met à jour prix / poids / catégorie / collection / provenance / descriptions."""
    if not products:
        raise MelSyncError("Aucun produit sélectionné pour la mise à jour.")

    total = len(products)
    steps = 2 + total * 2

    def progress(done: int, message: str) -> None:
        if on_progress:
            on_progress(done, steps, message)

    progress(0, f"Enrichissement de {total} produit(s)…")
    _enrich_selected_products(
        pfs_session,
        products,
        on_progress=lambda d, t, m: progress(d, m),
    )

    updated = 0
    errors: list[str] = []
    notes: list[str] = []

    try:
        with EfashionClient(efashion_session) as client:
            progress(1, "Chargement des référentiels EFashion…")
            reference_data = client.get_reference_data()
            store = CategoryMappingStore(id_vendeur=efashion_session.id_vendeur)
            mapper = MelMapper(
                client,
                reference_data,
                category_mapping=store,
            )

            for index, product in enumerate(products):
                reference = _product_reference(product)
                step = 2 + index * 2
                progress(step, f"Recherche EFashion : {reference}…")
                try:
                    main = client.find_main_product_by_reference(reference)
                    if not main:
                        raise MelSyncError(
                            f"{reference} : produit introuvable sur le catalogue "
                            "(en ligne)."
                        )
                    product_id = int(main["id_produit"])
                    payload = _build_update_fields(mapper, product)
                    client.update_produit(product_id, payload["produit"])
                    progress(step + 1, f"Descriptions : {reference}…")
                    if any(payload["descriptions"].values()):
                        client.save_produit_description(
                            product_id, payload["descriptions"]
                        )
                    updated += 1
                    notes.extend(mapper.created_notes)
                    mapper.created_notes.clear()
                except (MelSyncError, MelMappingError, EfashionApiError, ValueError) as exc:
                    errors.append(str(exc))
    except EfashionApiError as exc:
        raise MelSyncError(str(exc)) from exc

    if updated == 0 and errors:
        raise MelSyncError("\n".join(errors[:8]))

    message_parts = [f"{updated} produit(s) mis à jour sur le catalogue."]
    if errors:
        message_parts.append(
            f"{len(errors)} échec(s) : " + " | ".join(errors[:4])
        )
    if notes:
        message_parts.append("Notes : " + " | ".join(notes[:4]))

    return {
        "total": updated,
        "message": "\n".join(message_parts),
        "errors": errors,
    }


def _build_update_fields(
    mapper: MelMapper, product: dict[str, Any]
) -> dict[str, Any]:
    reference = _product_reference(product)
    price = product.get("unit_price")
    if not isinstance(price, (int, float)) or price <= 0:
        raise MelMappingError(f"{reference} : prix HT manquant ou invalide.")

    weight = product_weight_kg(product)
    if weight is None or weight <= 0:
        weight = 0.05
        mapper.created_notes.append(f"{reference} : poids absent → défaut 0.05 kg")

    category_id = int(mapper._resolve_category(product))
    collection_raw = mapper._resolve_collection(product)
    provenance_raw = mapper._resolve_provenance(product)
    descriptions = _product_descriptions(product)

    produit: dict[str, Any] = {
        "prix": float(price),
        "poids": float(weight),
        "id_categorie": category_id,
    }
    if collection_raw not in (None, ""):
        produit["id_collection"] = int(collection_raw)
    if provenance_raw not in (None, ""):
        produit["id_provenance"] = int(provenance_raw)

    return {
        "produit": produit,
        "descriptions": {
            "texte_fr": descriptions.get("descriptionFr") or "",
            "texte_uk": descriptions.get("descriptionEn") or "",
            "texte_it": descriptions.get("descriptionIt") or "",
            "texte_es": descriptions.get("descriptionEs") or "",
            "texte_de": descriptions.get("descriptionDe") or "",
        },
    }
