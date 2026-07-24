from __future__ import annotations

import re
from typing import Any, Callable

from .category_mapping import CategoryMappingStore
from .efashion_client import EfashionApiError, EfashionClient
from .mel_mapper import MelMapper, MelMappingError, _product_descriptions
from .mel_service import (
    MelSyncError,
    _apply_visibility_for_product,
    _enrich_selected_products,
    _product_reference,
)
from .pfs_client import (
    classify_pfs_product,
    expand_products_by_distinct_packs,
    inactive_variant_color_labels,
    mel_stock_qty_by_color,
    mel_taille_stock_qtys,
    product_images_by_color,
    product_weight_kg,
    resolve_efashion_vendu_par,
)
from .photo_upload import (
    MAX_PHOTOS_PER_PRODUCT,
    MAX_PHOTOS_PER_REQUEST,
    _aggregate_photo_urls,
    _color_aliases,
    _extract_efashion_color,
    _filter_groups_for_product,
    _normalize,
    download_image,
    fetch_upload_products_for_shootings,
    photo_urls_for_color,
    upload_pfs_photos_to_efashion,
)
from .session_store import EfashionSession, PfsSession
from .stock_sync import apply_stocks_mirror


def update_products_on_efashion(
    efashion_session: EfashionSession,
    products: list[dict],
    *,
    pfs_session: PfsSession,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, object]:
    """
    Miroir PFS → EFashion pour produits déjà en ligne :
    champs, nouvelles couleurs, refresh photos, stock, visibilité.
    Crée les fiches manquantes (ex. nouveau pack x6-REF).
    """
    if not products:
        raise MelSyncError("Aucun produit sélectionné pour la mise à jour.")

    total = len(products)

    def progress(done: int, message: str, *, total_steps: int | None = None) -> None:
        if on_progress:
            on_progress(done, total_steps or max(total * 5, 5), message)

    progress(0, f"Enrichissement de {total} produit(s)…")
    _enrich_selected_products(
        pfs_session,
        products,
        on_progress=lambda d, _t, m: progress(d, m),
    )

    products, pack_notes = expand_products_by_distinct_packs(products)
    total = len(products)
    steps = 3 + total * 5

    updated = 0
    created = 0
    colors_added = 0
    photos_refreshed = 0
    errors: list[str] = []
    notes: list[str] = list(pack_notes)
    mel_for_stock: list[dict[str, Any]] = []

    try:
        with EfashionClient(efashion_session) as client:
            progress(1, "Chargement des référentiels EFashion…", total_steps=steps)
            reference_data = client.get_reference_data()
            store = CategoryMappingStore(id_vendeur=efashion_session.id_vendeur)
            mapper = MelMapper(
                client,
                reference_data,
                category_mapping=store,
            )

            for index, product in enumerate(products):
                reference = _product_reference(product)
                base = 3 + index * 5
                progress(base, f"Miroir : {reference}…", total_steps=steps)
                try:
                    result = _mirror_one_product(
                        client,
                        mapper,
                        product,
                        on_step=lambda msg, offset=0: progress(
                            base + offset, msg, total_steps=steps
                        ),
                    )
                    if result.get("created"):
                        created += 1
                    else:
                        updated += 1
                    colors_added += int(result.get("colors_added") or 0)
                    photos_refreshed += int(result.get("photos_refreshed") or 0)
                    notes.extend(result.get("notes") or [])
                    errors.extend(result.get("errors") or [])
                    mel = result.get("mel")
                    if isinstance(mel, dict):
                        mel_for_stock.append(mel)
                    notes.extend(mapper.created_notes)
                    mapper.created_notes.clear()
                except (MelSyncError, MelMappingError, EfashionApiError, ValueError) as exc:
                    errors.append(f"{reference} : {exc}")

            if mel_for_stock:
                progress(
                    steps - 1,
                    "Application stocks (miroir)…",
                    total_steps=steps,
                )
                stock_errors = apply_stocks_mirror(client, mel_for_stock, [])
                errors.extend(stock_errors)

    except EfashionApiError as exc:
        raise MelSyncError(str(exc)) from exc

    if updated == 0 and created == 0 and errors:
        raise MelSyncError("\n".join(errors[:8]))

    message_parts = [
        f"{updated} produit(s) mis à jour (miroir).",
    ]
    if created:
        message_parts.append(f"{created} fiche(s) créée(s) (ex. nouveau pack).")
    if colors_added:
        message_parts.append(f"{colors_added} couleur(s) ajoutée(s).")
    if photos_refreshed:
        message_parts.append(f"{photos_refreshed} fiche(s) photos rafraîchie(s).")
    if errors:
        message_parts.append(
            f"{len(errors)} alerte(s) : " + " | ".join(errors[:4])
        )
    if notes:
        message_parts.append("Notes : " + " | ".join(notes[:8]))

    return {
        "total": updated + created,
        "updated": updated,
        "created": created,
        "colors_added": colors_added,
        "photos_refreshed": photos_refreshed,
        "message": "\n".join(message_parts),
        "errors": errors,
    }


def _mirror_one_product(
    client: EfashionClient,
    mapper: MelMapper,
    product: dict[str, Any],
    *,
    on_step: Callable[[str], None] | Callable[[str, int], None],
) -> dict[str, Any]:
    reference = _product_reference(product)
    notes: list[str] = []
    errors: list[str] = []

    def step(message: str, offset: int = 0) -> None:
        try:
            on_step(message, offset)  # type: ignore[call-arg]
        except TypeError:
            on_step(message)  # type: ignore[call-arg]

    mel = mapper.map_one(product)
    _attach_mirror_stocks(product, mel)
    group = _efashion_group_for_reference(client, reference)
    created = False

    if not group:
        step(f"Création manquante : {reference}…", 1)
        _create_product_full(client, mapper, product, mel, notes, errors)
        created = True
        group = _efashion_group_for_reference(client, reference)
        if not group:
            raise MelSyncError(f"{reference} : création OK mais fiche introuvable.")

    main = next((item for item in group if item.get("main")), group[0])
    main_id = int(main["id"])
    all_ids = [int(item["id"]) for item in group if item.get("id") is not None]

    step(f"Champs catalogue : {reference}…", 1)
    payload = _build_mirror_fields(mapper, product, mel)
    for product_id in all_ids:
        try:
            client.update_produit(product_id, payload["produit"])
        except EfashionApiError as exc:
            errors.append(f"{reference}#{product_id} update : {exc}")
    if any(payload["descriptions"].values()):
        try:
            client.save_produit_description(main_id, payload["descriptions"])
        except EfashionApiError as exc:
            errors.append(f"{reference} descriptions : {exc}")

    compositions = mel.get("compositions") or []
    if compositions:
        for product_id in all_ids:
            try:
                client.replace_produit_compositions(product_id, compositions)
            except EfashionApiError as exc:
                errors.append(f"{reference}#{product_id} composition : {exc}")
        notes.append(f"{reference} : composition resynchronisée")

    step(f"Couleurs manquantes : {reference}…", 2)
    colors_added, color_notes, color_errors = _sync_missing_colors(
        client,
        mapper,
        product,
        mel,
        main_product_id=main_id,
        reference=reference,
    )
    notes.extend(color_notes)
    errors.extend(color_errors)

    # Recharger le groupe après ajouts
    group = _efashion_group_for_reference(client, reference) or group
    orphan_notes, orphan_errors = _delete_orphan_colors(client, product, mel, group)
    notes.extend(orphan_notes)
    errors.extend(orphan_errors)

    step(f"Refresh photos : {reference}…", 3)
    photos_refreshed, photo_notes, photo_errors = _refresh_all_photos(
        client, product, group, reference
    )
    notes.extend(photo_notes)
    errors.extend(photo_errors)

    step(f"Principale / visibilité : {reference}…", 4)
    _sync_main_color(client, product, mel, group, notes, errors)
    kind = classify_pfs_product(product)
    vis_errors: list[str] = []
    _apply_visibility_for_product(
        client,
        product,
        [],
        force_offline=(kind == "disabled"),
        errors=vis_errors,
    )
    errors.extend(vis_errors)

    return {
        "created": created,
        "colors_added": colors_added,
        "photos_refreshed": photos_refreshed,
        "notes": notes,
        "errors": errors,
        "mel": mel,
    }


def _efashion_group_for_reference(
    client: EfashionClient, reference: str
) -> list[dict[str, Any]]:
    try:
        items = client.find_products_by_reference(reference)
    except EfashionApiError:
        return []
    return items


def _create_product_full(
    client: EfashionClient,
    mapper: MelMapper,
    product: dict[str, Any],
    mel: dict[str, Any],
    notes: list[str],
    errors: list[str],
) -> None:
    """Crée une fiche absente (souvent un pack xN-REF) via le flux MEL."""
    reference = _product_reference(product)
    client.save_mel_draft([mel])
    choice = client.save_mel_choice_upload([mel])
    shootings = choice.get("shootings") or []
    shooting_ids = [
        int(item["id"])
        for item in shootings
        if isinstance(item, dict) and item.get("id") is not None
    ]
    if shooting_ids:
        photo_result = upload_pfs_photos_to_efashion(
            client, [product], shooting_ids
        )
        errors.extend(list(photo_result.get("errors") or []))
        ef_products = fetch_upload_products_for_shootings(client, shooting_ids)
        ids = [
            int(item["id"])
            for item in ef_products
            if isinstance(item, dict)
            and item.get("id") is not None
            and (
                str(item.get("reference") or "").startswith(reference)
                or str(item.get("referenceBase") or "") == reference
            )
        ]
        if ids and classify_pfs_product(product) != "draft":
            try:
                offline = classify_pfs_product(product) == "disabled"
                client.publish_brouillon_bulk(ids)
                client.set_produits_visible(ids, visible=not offline)
            except EfashionApiError as exc:
                errors.append(f"{reference} publish : {exc}")
    notes.append(f"{reference} : fiche créée (absente du catalogue)")


def _build_mirror_fields(
    mapper: MelMapper, product: dict[str, Any], mel: dict[str, Any]
) -> dict[str, Any]:
    reference = _product_reference(product)
    price = product.get("unit_price")
    if not isinstance(price, (int, float)) or price <= 0:
        raise MelMappingError(f"{reference} : prix HT manquant ou invalide.")

    weight = product_weight_kg(product)
    if weight is None or weight <= 0:
        weight = 0.05
        mapper.created_notes.append(f"{reference} : poids absent → défaut 0.05 kg")

    brand_id = int(mapper._resolve_brand(product))
    category_id = int(mapper._resolve_category(product))
    collection_raw = mapper._resolve_collection(product)
    provenance_raw = mapper._resolve_provenance(product)
    descriptions = _product_descriptions(product)
    vendu_par = str(mel.get("venduPar") or resolve_efashion_vendu_par(product))

    produit: dict[str, Any] = {
        "prix": float(price),
        "poids": float(weight),
        "id_categorie": category_id,
        "id_vendeur_marque": brand_id,
        "vendu_par": vendu_par,
    }
    if collection_raw not in (None, ""):
        produit["id_collection"] = int(collection_raw)
    if provenance_raw not in (None, ""):
        produit["id_provenance"] = int(provenance_raw)

    pack_raw = mel.get("quantitePaquet")
    if pack_raw not in (None, ""):
        try:
            produit["id_pack"] = int(pack_raw)
        except (TypeError, ValueError):
            pass
    decl_raw = mel.get("taillePaquet")
    if decl_raw not in (None, ""):
        try:
            produit["id_declinaison"] = int(decl_raw)
        except (TypeError, ValueError):
            pass
    dimensions = mel.get("dimensions")
    if dimensions:
        produit["dimension"] = str(dimensions)

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


def _attach_mirror_stocks(product: dict[str, Any], mel: dict[str, Any]) -> None:
    """Enrichit le payload MEL avec les stocks PFS (0 et >0) pour le miroir."""
    vendu_par = str(mel.get("venduPar") or "").lower()
    couleurs = mel.get("couleurs") or []
    if not isinstance(couleurs, list):
        return

    if vendu_par == "couleurs":
        qty_by_label = mel_stock_qty_by_color(product)
        for couleur in couleurs:
            if not isinstance(couleur, dict):
                continue
            nom = str(couleur.get("nom") or "").strip()
            if not nom:
                continue
            wanted = _color_aliases(nom)
            wanted.add(_normalize(nom))
            for label, qty in qty_by_label.items():
                if _normalize(label) in wanted or (_color_aliases(label) & wanted):
                    couleur["stock"] = int(qty)
                    break
    elif vendu_par == "tailles":
        by_color = mel_taille_stock_qtys(product)
        for couleur in couleurs:
            if not isinstance(couleur, dict):
                continue
            nom = str(couleur.get("nom") or "").strip()
            if not nom:
                continue
            wanted = _color_aliases(nom)
            wanted.add(_normalize(nom))
            for label, stocks in by_color.items():
                if _normalize(label) in wanted or (_color_aliases(label) & wanted):
                    couleur["tailleStocks"] = stocks
                    break
    elif vendu_par == "melangees":
        # deja rempli par efashion_mel_stock ("0" ou "")
        qtys = list(mel_stock_qty_by_color(product).values())
        if qtys:
            mel["stock"] = str(sum(qtys))


def _color_is_inactive_on_pfs(product: dict[str, Any], color_label: str) -> bool:
    wanted = _color_aliases(color_label)
    wanted.add(_normalize(color_label))
    for label in inactive_variant_color_labels(product):
        if _normalize(label) in wanted or (_color_aliases(label) & wanted):
            return True
    return False


def _existing_efashion_color_keys(group: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for item in group:
        ef_color = _extract_efashion_color(
            str(item.get("reference") or ""),
            str(item.get("referenceBase") or item.get("reference_base") or ""),
        )
        if ef_color:
            keys |= _color_aliases(ef_color)
            keys.add(_normalize(ef_color))
    return keys


def _files_from_urls(
    reference: str, color_label: str, urls: list[str]
) -> list[tuple[str, bytes, str]]:
    files: list[tuple[str, bytes, str]] = []
    for url_index, url in enumerate(urls):
        content, filename, content_type = download_image(url)
        safe_name = (
            f"{reference}-{color_label}-{url_index}"
            f"{filename[filename.rfind('.'):]}"
        )
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", safe_name)
        files.append((safe_name, content, content_type))
    return files


def _upload_files(
    client: EfashionClient, product_id: int, files: list[tuple[str, bytes, str]]
) -> None:
    for offset in range(0, len(files), MAX_PHOTOS_PER_REQUEST):
        chunk = files[offset : offset + MAX_PHOTOS_PER_REQUEST]
        client.upload_product_photos(product_id, chunk)


def _sync_missing_colors(
    client: EfashionClient,
    mapper: MelMapper,
    product: dict[str, Any],
    mel: dict[str, Any],
    *,
    main_product_id: int,
    reference: str,
) -> tuple[int, list[str], list[str]]:
    notes: list[str] = []
    errors: list[str] = []
    added = 0

    vendu_par = str(mel.get("venduPar") or resolve_efashion_vendu_par(product))
    if vendu_par == "melangees":
        return 0, notes, errors

    couleurs = mel.get("couleurs") or []
    group = _efashion_group_for_reference(client, reference)
    existing = _existing_efashion_color_keys(group)
    new_ids: list[int] = []

    for couleur in couleurs:
        if not isinstance(couleur, dict):
            continue
        nom = str(couleur.get("nom") or "").strip()
        color_id = couleur.get("id")
        if not nom or color_id is None:
            continue
        aliases = _color_aliases(nom)
        aliases.add(_normalize(nom))
        if aliases & existing:
            continue
        try:
            created = client.duplicate_with_new_color(
                main_product_id,
                couleur_id=int(color_id),
                couleur_name=nom,
            )
            new_id = int(created["id_produit"])
            urls = photo_urls_for_color(product, nom)
            if urls:
                files = _files_from_urls(reference, nom, urls)
                _upload_files(client, new_id, files)
            else:
                notes.append(f"{reference}/{nom} : créée sans photo source")

            if _color_is_inactive_on_pfs(product, nom):
                try:
                    client.set_produits_visible([new_id], visible=False)
                except EfashionApiError:
                    pass
            else:
                new_ids.append(new_id)

            existing |= aliases
            added += 1
            notes.append(f"{reference} : couleur ajoutée « {nom} »")
        except EfashionApiError as exc:
            errors.append(f"{reference}/{nom} : {exc}")

    if new_ids:
        try:
            client.publish_brouillon_bulk(new_ids)
            client.set_produits_visible(new_ids, visible=True)
        except EfashionApiError as exc:
            errors.append(f"{reference} : publish nouvelles couleurs : {exc}")

    return added, notes, errors


def _delete_orphan_colors(
    client: EfashionClient,
    product: dict[str, Any],
    mel: dict[str, Any],
    group: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Couleurs EFashion absentes de PFS → soft-delete (jamais la fiche principale)."""
    notes: list[str] = []
    errors: list[str] = []
    vendu_par = str(mel.get("venduPar") or resolve_efashion_vendu_par(product))
    if vendu_par == "melangees" or len(group) <= 1:
        return notes, errors

    pfs_keys: set[str] = set()
    for couleur in mel.get("couleurs") or []:
        if not isinstance(couleur, dict):
            continue
        nom = str(couleur.get("nom") or "").strip()
        if not nom:
            continue
        pfs_keys |= _color_aliases(nom)
        pfs_keys.add(_normalize(nom))

    if not pfs_keys:
        return notes, errors

    delete_ids: list[int] = []
    for item in group:
        if item.get("main"):
            continue
        ef_color = _extract_efashion_color(
            str(item.get("reference") or ""),
            str(item.get("referenceBase") or item.get("reference_base") or ""),
        )
        if not ef_color:
            continue
        if _normalize(ef_color) in pfs_keys or (_color_aliases(ef_color) & pfs_keys):
            continue
        if item.get("id") is not None:
            delete_ids.append(int(item["id"]))
            notes.append(
                f"{_product_reference(product)}/{ef_color} : plus sur PFS → supprimée"
            )

    if delete_ids:
        try:
            client.soft_delete_produits(delete_ids)
        except EfashionApiError as exc:
            errors.append(f"suppression couleurs orphelines : {exc}")
    return notes, errors


def _refresh_all_photos(
    client: EfashionClient,
    product: dict[str, Any],
    group: list[dict[str, Any]],
    reference: str,
) -> tuple[int, list[str], list[str]]:
    notes: list[str] = []
    errors: list[str] = []
    refreshed = 0
    vendu_par = resolve_efashion_vendu_par(product)

    for item in group:
        product_id = item.get("id")
        if product_id is None:
            continue
        product_id = int(product_id)
        ef_color = _extract_efashion_color(
            str(item.get("reference") or ""),
            str(item.get("referenceBase") or item.get("reference_base") or ""),
        )
        if vendu_par == "melangees":
            urls = _aggregate_photo_urls(
                _filter_groups_for_product(product, product_images_by_color(product))
            )[:MAX_PHOTOS_PER_PRODUCT]
            label = "MAIN"
        elif vendu_par == "tailles" and len(group) == 1 and not ef_color:
            urls = _aggregate_photo_urls(
                _filter_groups_for_product(product, product_images_by_color(product))
            )[:MAX_PHOTOS_PER_PRODUCT]
            label = "MAIN"
        else:
            urls = photo_urls_for_color(product, ef_color)
            label = ef_color or ("MAIN" if item.get("main") else "SECONDARY")

        if not urls:
            notes.append(f"{reference}/{label} : pas de photos PFS à rafraîchir")
            continue

        try:
            files = _files_from_urls(reference, label, urls)
            client.replace_product_photos(product_id, files)
            refreshed += 1
            notes.append(f"{reference}/{label} : photos rafraîchies ({len(urls)})")
        except EfashionApiError as exc:
            errors.append(f"{reference}/{label} photos : {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{reference}/{label} photos : {exc}")

    return refreshed, notes, errors


def _sync_main_color(
    client: EfashionClient,
    product: dict[str, Any],
    mel: dict[str, Any],
    group: list[dict[str, Any]],
    notes: list[str],
    errors: list[str],
) -> None:
    couleurs = mel.get("couleurs") or []
    wanted_main = next(
        (
            str(c.get("nom") or "").strip()
            for c in couleurs
            if isinstance(c, dict) and c.get("isMain")
        ),
        "",
    )
    if not wanted_main:
        return

    wanted = _color_aliases(wanted_main)
    wanted.add(_normalize(wanted_main))
    current_main = next((item for item in group if item.get("main")), None)
    if current_main:
        cur_color = _extract_efashion_color(
            str(current_main.get("reference") or ""),
            str(
                current_main.get("referenceBase")
                or current_main.get("reference_base")
                or ""
            ),
        )
        if cur_color and (
            _normalize(cur_color) in wanted or (_color_aliases(cur_color) & wanted)
        ):
            return

    target = None
    for item in group:
        ef_color = _extract_efashion_color(
            str(item.get("reference") or ""),
            str(item.get("referenceBase") or item.get("reference_base") or ""),
        )
        if ef_color and (
            _normalize(ef_color) in wanted or (_color_aliases(ef_color) & wanted)
        ):
            target = item
            break
    if not target or target.get("id") is None:
        return
    if target.get("main"):
        return
    try:
        client.toggle_main_product(int(target["id"]))
        notes.append(
            f"{_product_reference(product)} : principale basculée → {wanted_main}"
        )
    except EfashionApiError as exc:
        errors.append(f"toggleMain « {wanted_main} » : {exc}")
