from __future__ import annotations

from typing import Callable

from .category_mapping import CategoryMappingStore
from .efashion_client import EfashionApiError, EfashionClient
from .mel_mapper import MelMapper, MelMappingError
from .pfs_client import PfsApiError, PfsClient
from .photo_upload import fetch_upload_products_for_shootings, upload_pfs_photos_to_efashion
from .session_store import EfashionSession, PfsSession


class MelSyncError(Exception):
    pass


def _product_reference(product: dict) -> str:
    return str(product.get("reference") or product.get("id") or "").strip()


def validate_products_for_send(products: list[dict]) -> list[str]:
    if not products:
        raise MelSyncError("Aucun produit sélectionné.")

    errors: list[str] = []
    for product in products:
        reference = _product_reference(product)
        if not reference:
            errors.append("Un produit sélectionné n'a pas de référence.")
            continue
        price = product.get("unit_price")
        if not isinstance(price, (int, float)) or price <= 0:
            errors.append(f"{reference} : prix HT manquant ou invalide.")

    if errors:
        raise MelSyncError("\n".join(errors[:8]))
    return [_product_reference(product) for product in products]


def _apply_enriched_inplace(originals: list[dict], enriched: list[dict]) -> None:
    """Met à jour les dicts d'origine (référencés aussi par la GUI)."""
    for original, updated in zip(originals, enriched):
        if original is updated:
            continue
        original.clear()
        original.update(updated)


def _enrich_selected_products(
    pfs_session: PfsSession,
    products: list[dict],
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> None:
    try:
        with PfsClient(pfs_session) as client:
            enriched = client.enrich_products(products, on_progress=on_progress)
    except PfsApiError as exc:
        raise MelSyncError(str(exc)) from exc

    _apply_enriched_inplace(products, enriched)

    incomplete = [
        _product_reference(product) or "?"
        for product in products
        if not product.get("detail_loaded")
    ]
    if incomplete:
        sample = ", ".join(incomplete[:6])
        more = f" (+{len(incomplete) - 6})" if len(incomplete) > 6 else ""
        raise MelSyncError(
            "Impossible d'enrichir certains produits PFS avant l'envoi : "
            f"{sample}{more}."
        )


def send_products_to_efashion(
    efashion_session: EfashionSession,
    products: list[dict],
    *,
    pfs_session: PfsSession,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, object]:
    references_names = validate_products_for_send(products)
    total = len(references_names)
    base_steps = 6

    def progress(done: int, message: str, total_steps: int | None = None) -> None:
        if on_progress:
            on_progress(done, total_steps or (base_steps + total), message)

    def enrich_progress(done: int, enrich_total: int, message: str) -> None:
        progress(done, message, total_steps=base_steps + enrich_total + total)

    progress(0, f"Enrichissement PFS de {total} produit(s) sélectionné(s)…")
    _enrich_selected_products(pfs_session, products, on_progress=enrich_progress)

    try:
        with EfashionClient(efashion_session) as client:
            progress(1, "Chargement des référentiels EFashion…")
            reference_data = client.get_reference_data()
            mapping_store = CategoryMappingStore(id_vendeur=client.session.id_vendeur)
            mapper = MelMapper(client, reference_data, category_mapping=mapping_store)

            progress(2, f"Mapping PFS → MEL de {total} produit(s)…")
            mel_references = mapper.map_products(products)

            progress(3, "Création des fiches MEL…")
            draft_result = client.save_mel_draft(mel_references)
            created = int(draft_result.get("totalProducts") or 0)

            progress(4, "Passage en MEL UPGR (gestion upload photo)…")
            choice_result = client.save_mel_choice_upload(mel_references)

            shootings = choice_result.get("shootings") or []
            shooting_ids = [
                int(item["id"])
                for item in shootings
                if isinstance(item, dict) and item.get("id") is not None
            ]

            photo_result: dict[str, object] = {
                "uploaded_products": 0,
                "uploaded_photos": 0,
                "errors": [],
            }
            if shooting_ids:
                progress(5, "Upload photos PFS → S3 EFashion (resize serveur)…")

                def photo_progress(done: int, photo_total: int, message: str) -> None:
                    progress(
                        base_steps + done,
                        message,
                        total_steps=base_steps + photo_total + 1,
                    )

                photo_result = upload_pfs_photos_to_efashion(
                    client,
                    products,
                    shooting_ids,
                    on_progress=photo_progress,
                )

            published = 0
            publish_errors: list[str] = []
            if shooting_ids:
                progress(
                    base_steps + int(photo_result.get("uploaded_photos") or 0),
                    "Mise en ligne des produits (activés)…",
                    total_steps=base_steps
                    + max(int(photo_result.get("uploaded_photos") or 0), 1)
                    + 1,
                )
                ef_products = fetch_upload_products_for_shootings(client, shooting_ids)
                mains = [
                    int(item["id"])
                    for item in ef_products
                    if item.get("main") and item.get("id") is not None
                ]
                others = [
                    int(item["id"])
                    for item in ef_products
                    if not item.get("main") and item.get("id") is not None
                ]
                product_ids = mains + others
                if product_ids:
                    try:
                        published = client.publish_brouillon_bulk(product_ids)
                    except EfashionApiError as exc:
                        publish_errors.append(str(exc))
                else:
                    publish_errors.append(
                        "Aucun produit UPGR trouvé pour la mise en ligne."
                    )

            notes = list(mapper.created_notes)
            uploaded_photos = int(photo_result.get("uploaded_photos") or 0)
            uploaded_products = int(photo_result.get("uploaded_products") or 0)
            photo_errors = list(photo_result.get("errors") or [])

            message_parts = [
                f"{created or total} produit(s) créé(s) et mis en ligne "
                f"({published} fiche(s) activée(s)).",
                f"{uploaded_photos} photo(s) uploadée(s) sur S3 "
                f"({uploaded_products} fiche(s)).",
            ]
            if shooting_ids:
                message_parts.append(
                    "Shooting(s) : " + ", ".join(str(sid) for sid in shooting_ids)
                )
            if notes:
                message_parts.append("Notes : " + " ; ".join(notes[:6]))
            if photo_errors:
                message_parts.append(
                    "Alertes photos : " + " ; ".join(str(err) for err in photo_errors[:4])
                )
            if publish_errors:
                message_parts.append(
                    "Alertes publication : "
                    + " ; ".join(str(err) for err in publish_errors[:4])
                )

            if on_progress:
                on_progress(100, 100, "Envoi EFashion terminé")

            return {
                "total": created or total,
                "published": published,
                "references": references_names,
                "shooting_ids": [str(sid) for sid in shooting_ids],
                "uploaded_photos": uploaded_photos,
                "uploaded_products": uploaded_products,
                "message": " ".join(message_parts),
                "notes": notes,
                "photo_errors": photo_errors,
                "publish_errors": publish_errors,
            }
    except MelMappingError as exc:
        raise MelSyncError(str(exc)) from exc
    except EfashionApiError as exc:
        raise MelSyncError(str(exc)) from exc
