from __future__ import annotations

from typing import Any, Callable

from .category_mapping import CategoryMappingStore
from .efashion_client import EfashionApiError, EfashionClient
from .mel_mapper import MelMapper, MelMappingError
from .pfs_client import (
    PfsApiError,
    PfsClient,
    classify_pfs_product,
    expand_products_by_distinct_packs,
    inactive_variant_color_labels,
    _first_str,
    _localized_label,
)
from .photo_upload import (
    _color_aliases,
    _efashion_candidates_for_reference,
    _extract_efashion_color,
    _normalize,
    fetch_upload_products_for_shootings,
    upload_pfs_photos_to_efashion,
)
from .session_store import EfashionSession, PfsSession
from .stock_sync import apply_rupture_stocks_after_publish


SEND_BATCH_SIZE = 100


class MelSyncError(Exception):
    pass


def _product_reference(product: dict) -> str:
    return str(product.get("reference") or product.get("id") or "").strip()


def _product_color_labels(product: dict) -> list[str]:
    labels: list[str] = []
    colors_raw = str(product.get("colors") or product.get("couleurs") or "").strip()
    if colors_raw:
        for part in colors_raw.replace(",", ";").split(";"):
            name = part.strip()
            if name and name not in labels:
                labels.append(name)

    variants = product.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            item = variant.get("item") if isinstance(variant.get("item"), dict) else {}
            color = item.get("color") if isinstance(item.get("color"), dict) else {}
            label = (
                _localized_label(color.get("labels"))
                or _first_str(color, "reference")
                or ""
            ).strip()
            if label and label not in labels:
                labels.append(label)
    return labels


def _unavailable_color_keys(product: dict) -> set[str]:
    """Clés normalisées des couleurs PFS désactivées (is_active=false), alias FR/EN inclus."""
    keys: set[str] = set()
    for label in inactive_variant_color_labels(product):
        keys |= _color_aliases(label)
        keys.add(_normalize(label))
    return keys


def _ef_item_id(item: dict) -> int | None:
    raw = item.get("id")
    if raw is None:
        raw = item.get("id_produit")
    if raw is None:
        return None
    return int(raw)


def _set_visibility(
    client: EfashionClient,
    product_ids: list[int],
    *,
    visible: bool,
    errors: list[str],
) -> None:
    if not product_ids:
        return
    unique = sorted({int(pid) for pid in product_ids})
    try:
        client.set_produits_visible(unique, visible=visible)
    except EfashionApiError as exc:
        errors.append(f"setProduitsVisible({visible}) : {exc}")
    for product_id in unique:
        try:
            client.update_produit(product_id, {"visible": visible})
        except EfashionApiError as exc:
            errors.append(f"visible={visible} #{product_id} : {exc}")


def _ef_color_is_unavailable(ef_color: str, unavailable: set[str]) -> bool:
    if not ef_color:
        return False
    if _normalize(ef_color) in unavailable:
        return True
    return bool(_color_aliases(ef_color) & unavailable)


def _apply_visibility_for_product(
    client: EfashionClient,
    product: dict,
    ef_products: list[dict],
    *,
    force_offline: bool,
    errors: list[str],
) -> None:
    reference = _product_reference(product)
    if not reference:
        return

    # Priorité aux fiches productsPage (réf. BASE-COULEUR après publish)
    id_to_item: dict[int, dict] = {}
    try:
        for item in client.find_products_by_reference(reference):
            pid = _ef_item_id(item)
            if pid is not None:
                id_to_item[pid] = item
    except EfashionApiError as exc:
        errors.append(f"productsPage({reference}) : {exc}")

    for item in _efashion_candidates_for_reference(ef_products, reference):
        pid = _ef_item_id(item)
        if pid is None:
            continue
        # Ne pas écraser une fiche productsPage plus complète
        if pid not in id_to_item:
            id_to_item[pid] = item

    if not id_to_item:
        return

    if force_offline:
        _set_visibility(client, list(id_to_item.keys()), visible=False, errors=errors)
        return

    unavailable = _unavailable_color_keys(product)
    visible_ids: list[int] = []
    hidden_ids: list[int] = []

    for product_id, item in id_to_item.items():
        ef_color = _extract_efashion_color(
            str(item.get("reference") or ""),
            str(item.get("referenceBase") or item.get("reference_base") or ""),
        )
        # Fiche sans suffixe couleur (melangees / 1 couleur) :
        # hors ligne seulement si aucune couleur dispo côté PFS
        if not ef_color:
            if unavailable and len(unavailable) >= len(_product_color_labels(product)):
                hidden_ids.append(product_id)
            else:
                visible_ids.append(product_id)
            continue

        if _ef_color_is_unavailable(ef_color, unavailable):
            hidden_ids.append(product_id)
        else:
            visible_ids.append(product_id)

    # publishBrouillon force visible=true → on décoche d'abord les indispos
    _set_visibility(client, hidden_ids, visible=False, errors=errors)
    _set_visibility(client, visible_ids, visible=True, errors=errors)


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


def _efashion_reference_base(item: dict) -> str:
    return str(
        item.get("referenceBase") or item.get("reference_base") or ""
    ).strip()


def _product_ids_for_references(
    ef_products: list[dict],
    references: set[str],
) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for item in ef_products:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        ref = str(item.get("reference") or "").strip()
        ref_base = _efashion_reference_base(item)
        if ref not in references and ref_base not in references:
            continue
        product_id = int(item["id"])
        if product_id in seen:
            continue
        seen.add(product_id)
        ids.append(product_id)
    return ids


def _ordered_product_ids(
    product_ids: list[int],
    ef_products: list[dict],
) -> list[int]:
    mains = [
        pid
        for pid in product_ids
        if any(
            isinstance(item, dict)
            and int(item.get("id") or 0) == pid
            and item.get("main")
            for item in ef_products
        )
    ]
    others = [pid for pid in product_ids if pid not in mains]
    return mains + others


def _finalize_products_after_photos(
    client: EfashionClient,
    products: list[dict],
    mel_references: list[dict],
    shooting_ids: list[int],
    photo_ready_references: set[str],
) -> tuple[int, int, int, list[str]]:
    """
    Finalise les fiches EFashion :
    - NEW → reste brouillon
    - READY_FOR_SALE → publish seulement si toutes ses fiches ont une photo validée
    - ARCHIVED → publish puis Visible décoché sur tout le groupe
    """
    if not products or not shooting_ids:
        return 0, 0, 0, []

    active_products: list[dict] = []
    draft_products: list[dict] = []
    disabled_products: list[dict] = []
    errors: list[str] = []

    for product in products:
        reference = _product_reference(product)
        if not reference:
            continue
        kind = classify_pfs_product(product)
        if kind == "disabled":
            disabled_products.append(product)
        elif kind == "draft":
            draft_products.append(product)
        elif reference in photo_ready_references:
            active_products.append(product)
        else:
            errors.append(
                f"{reference} : non mis en ligne car au moins une fiche "
                "n'a pas de photo validée."
            )

    if not active_products and not draft_products and not disabled_products:
        return 0, 0, 0, errors

    active_refs = {_product_reference(p) for p in active_products}
    disabled_refs = {_product_reference(p) for p in disabled_products}
    draft_refs = {_product_reference(p) for p in draft_products}

    ef_products = fetch_upload_products_for_shootings(client, shooting_ids)
    active_ids = _ordered_product_ids(
        _product_ids_for_references(ef_products, active_refs),
        ef_products,
    )
    disabled_ids = _ordered_product_ids(
        _product_ids_for_references(ef_products, disabled_refs),
        ef_products,
    )

    published_online = 0
    published_offline = 0
    draft_count = len(draft_refs)

    if active_ids:
        try:
            published_online = client.publish_brouillon_bulk(active_ids)
        except EfashionApiError as exc:
            errors.append(f"Mise en ligne : {exc}")
        for product in active_products:
            _apply_visibility_for_product(
                client,
                product,
                ef_products,
                force_offline=False,
                errors=errors,
            )
    elif active_refs:
        errors.append("Aucune fiche EFashion trouvée pour les produits actifs.")

    if disabled_ids:
        try:
            published_offline = client.publish_brouillon_bulk(disabled_ids)
        except EfashionApiError as exc:
            errors.append(f"Publication hors ligne : {exc}")
        for product in disabled_products:
            _apply_visibility_for_product(
                client,
                product,
                ef_products,
                force_offline=True,
                errors=errors,
            )
    elif disabled_refs:
        errors.append(
            "Aucune fiche EFashion trouvée pour les produits source désactivés."
        )

    if draft_refs:
        found_drafts = _product_ids_for_references(ef_products, draft_refs)
        if not found_drafts:
            errors.append(
                "Aucune fiche EFashion trouvée pour les brouillons source (NEW)."
            )

    stock_errors = apply_rupture_stocks_after_publish(
        client,
        mel_references,
        ef_products,
    )
    errors.extend(stock_errors)

    return published_online, published_offline, draft_count, errors


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

    # Même ref ×4 + ×6 → 2 fiches (x4-REF, x6-REF)
    ready_products, pack_split_notes = expand_products_by_distinct_packs(ready_products)

    try:
        with EfashionClient(efashion_session) as client:
            progress(1, "Chargement des référentiels EFashion…")
            reference_data = client.get_reference_data()
            mapping_store = CategoryMappingStore(id_vendeur=client.session.id_vendeur)
            mapper = MelMapper(client, reference_data, category_mapping=mapping_store)
            mapper.created_notes.extend(pack_split_notes)

            progress(2, f"Mapping source → MEL de {len(ready_products)} produit(s)…")
            mel_references, map_errors = mapper.map_products(ready_products)
            skipped_errors.extend(map_errors)

            if not mel_references:
                raise MelSyncError("\n".join(skipped_errors[:10]))

            sent_refs = {
                str(item.get("reference") or "").strip()
                for item in mel_references
                if str(item.get("reference") or "").strip()
            }
            products_for_photos = [
                product
                for product in ready_products
                if _product_reference(product) in sent_refs
            ]

            counts = {"active": 0, "draft": 0, "disabled": 0}
            for product in products_for_photos:
                kind = classify_pfs_product(product)
                counts[kind] = counts.get(kind, 0) + 1

            # Les endpoints MEL reçoivent un payload très volumineux par produit.
            # Limiter les mutations à 100 références évite les erreurs serveur
            # lorsqu'un vendeur sélectionne plusieurs centaines de produits.
            created = 0
            shooting_ids: list[int] = []
            batch_count = (
                len(mel_references) + SEND_BATCH_SIZE - 1
            ) // SEND_BATCH_SIZE
            for offset in range(0, len(mel_references), SEND_BATCH_SIZE):
                batch_number = offset // SEND_BATCH_SIZE + 1
                references_batch = mel_references[offset : offset + SEND_BATCH_SIZE]
                progress(
                    3,
                    "Création des fiches MEL (brouillon) — "
                    f"lot {batch_number}/{batch_count} "
                    f"({len(references_batch)} produit(s))…",
                )
                draft_result = client.save_mel_draft(references_batch)
                created += int(draft_result.get("totalProducts") or 0)

                progress(
                    4,
                    "Préparation de l'upload photos — "
                    f"lot {batch_number}/{batch_count}…",
                )
                choice_result = client.save_mel_choice_upload(references_batch)
                shootings = choice_result.get("shootings") or []
                shooting_ids.extend(
                    int(item["id"])
                    for item in shootings
                    if isinstance(item, dict) and item.get("id") is not None
                )

            photo_result: dict[str, object] = {
                "uploaded_products": 0,
                "uploaded_photos": 0,
                "errors": [],
                "photo_ready_references": [],
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

            published_online = 0
            published_offline = 0
            draft_kept = 0
            finalize_errors: list[str] = []
            if shooting_ids and products_for_photos:
                progress(
                    base_steps + int(photo_result.get("uploaded_photos") or 0),
                    "Finalisation : brouillon / en ligne / hors ligne / stock…",
                    total_steps=base_steps
                    + max(int(photo_result.get("uploaded_photos") or 0), 1)
                    + 1,
                )
                published_online, published_offline, draft_kept, finalize_errors = (
                    _finalize_products_after_photos(
                        client,
                        products_for_photos,
                        mel_references,
                        shooting_ids,
                        {
                            str(reference).strip()
                            for reference in (
                                photo_result.get("photo_ready_references") or []
                            )
                            if str(reference).strip()
                        },
                    )
                )

            notes = list(mapper.created_notes)
            uploaded_photos = int(photo_result.get("uploaded_photos") or 0)
            uploaded_products = int(photo_result.get("uploaded_products") or 0)
            photo_errors = list(photo_result.get("errors") or [])
            sent_count = len(mel_references)

            message_parts = [
                f"{created or sent_count} produit(s) traité(s) sur {total_selected} sélectionné(s).",
                f"{uploaded_photos} photo(s) uploadée(s) sur S3 "
                f"({uploaded_products} fiche(s)).",
            ]
            if counts["active"]:
                message_parts.append(
                    f"{published_online} fiche(s) actives "
                    f"mise(s) en ligne."
                )
            if counts["draft"]:
                message_parts.append(
                    f"{draft_kept or counts['draft']} brouillon(s) source (NEW) "
                    f"laissé(s) en brouillon EFashion."
                )
            if counts["disabled"]:
                message_parts.append(
                    f"{published_offline or counts['disabled']} fiche(s) "
                    f"archivée(s) importée(s) hors ligne."
                )
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
            if finalize_errors:
                message_parts.append(
                    "Alertes finalisation : "
                    + " ; ".join(str(err) for err in finalize_errors[:6])
                )

            if on_progress:
                on_progress(100, 100, "Envoi EFashion terminé")

            return {
                "total": sent_count,
                "published": published_online,
                "published_offline": published_offline,
                "draft_kept": draft_kept,
                "references": sent_refs,
                "shooting_ids": [str(sid) for sid in shooting_ids],
                "uploaded_photos": uploaded_photos,
                "uploaded_products": uploaded_products,
                "message": "\n".join(message_parts),
                "notes": notes,
                "photo_errors": photo_errors,
                "finalize_errors": finalize_errors,
                "errors": skipped_errors,
            }
    except MelMappingError as exc:
        raise MelSyncError(str(exc)) from exc
    except EfashionApiError as exc:
        raise MelSyncError(str(exc)) from exc
