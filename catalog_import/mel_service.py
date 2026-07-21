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
    valid, _errors = partition_products_for_send(products)
    if not valid:
        raise MelSyncError("Aucun produit sélectionné valide pour l'envoi.")
    return [_product_reference(product) for product in valid]


def partition_products_for_send(
    products: list[dict],
) -> tuple[list[dict], list[str]]:
    """Retourne (produits valides, erreurs des produits exclus)."""
    if not products:
        raise MelSyncError("Aucun produit sélectionné.")

    valid: list[dict] = []
    errors: list[str] = []
    for product in products:
        reference = _product_reference(product)
        if not reference:
            errors.append("Un produit sélectionné n'a pas de référence.")
            continue
        price = product.get("unit_price")
        if not isinstance(price, (int, float)) or price <= 0:
            errors.append(f"{reference} : prix HT manquant ou invalide.")
            continue
        valid.append(product)
    return valid, errors


def _products_ready_after_enrichment(
    products: list[dict],
) -> tuple[list[dict], list[str]]:
    ready: list[dict] = []
    errors: list[str] = []
    for product in products:
        reference = _product_reference(product) or "?"
        if not product.get("detail_loaded"):
            errors.append(
                f"{reference} : enrichissement incomplet "
                "(détail produit inaccessible)."
            )
            continue
        ready.append(product)
    return ready, errors


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


def send_products_to_efashion(
    efashion_session: EfashionSession,
    products: list[dict],
    *,
    pfs_session: PfsSession,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, object]:
    candidates, skipped_errors = partition_products_for_send(products)
    total_selected = len(products)
    total = len(candidates)
    base_steps = 6

    def progress(done: int, message: str, total_steps: int | None = None) -> None:
        if on_progress:
            on_progress(done, total_steps or (base_steps + total), message)

    def enrich_progress(done: int, enrich_total: int, message: str) -> None:
        progress(done, message, total_steps=base_steps + enrich_total + total)

    progress(0, f"Enrichissement de {total} produit(s) sélectionné(s)…")
    _enrich_selected_products(pfs_session, candidates, on_progress=enrich_progress)

    ready_products, enrich_errors = _products_ready_after_enrichment(candidates)
    skipped_errors.extend(enrich_errors)

    if not ready_products:
        raise MelSyncError("\n".join(skipped_errors[:10]))

    try:
        with EfashionClient(efashion_session) as client:
            progress(1, "Chargement des référentiels EFashion…")
            reference_data = client.get_reference_data()
            mapping_store = CategoryMappingStore(id_vendeur=client.session.id_vendeur)
            mapper = MelMapper(client, reference_data, category_mapping=mapping_store)

            progress(2, f"Mapping source → MEL de {len(ready_products)} produit(s)…")
            mel_references, map_errors = mapper.map_products(ready_products)
            skipped_errors.extend(map_errors)

            if not mel_references:
                raise MelSyncError("\n".join(skipped_errors[:10]))

            sent_refs = [
                str(item.get("reference") or "").strip()
                for item in mel_references
                if str(item.get("reference") or "").strip()
            ]
            products_for_photos = [
                product
                for product in ready_products
                if _product_reference(product) in sent_refs
            ]

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
                progress(5, "Upload photos → S3 EFashion (resize serveur)…")

                def photo_progress(done: int, photo_total: int, message: str) -> None:
                    progress(
                        base_steps + done,
                        message,
                        total_steps=base_steps + photo_total + 1,
                    )

                photo_result = upload_pfs_photos_to_efashion(
                    client,
                    products_for_photos,
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
            sent_count = len(mel_references)

            message_parts = [
                f"{created or sent_count} produit(s) créé(s) et mis en ligne "
                f"({published} fiche(s) activée(s)) "
                f"sur {total_selected} sélectionné(s).",
                f"{uploaded_photos} photo(s) uploadée(s) sur S3 "
                f"({uploaded_products} fiche(s)).",
            ]
            if skipped_errors:
                message_parts.append(
                    f"{len(skipped_errors)} non envoyé(s) : "
                    + " | ".join(skipped_errors[:6])
                )
                if len(skipped_errors) > 6:
                    message_parts.append(f"(+{len(skipped_errors) - 6} autre(s))")
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
                "total": sent_count,
                "published": published,
                "references": sent_refs,
                "shooting_ids": [str(sid) for sid in shooting_ids],
                "uploaded_photos": uploaded_photos,
                "uploaded_products": uploaded_products,
                "message": "\n".join(message_parts),
                "notes": notes,
                "photo_errors": photo_errors,
                "publish_errors": publish_errors,
                "errors": skipped_errors,
            }
    except MelMappingError as exc:
        raise MelSyncError(str(exc)) from exc
    except EfashionApiError as exc:
        raise MelSyncError(str(exc)) from exc
