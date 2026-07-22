from __future__ import annotations

from typing import Any, Callable

from curl_cffi import CurlMime

from .config import EFASHION_API_URL, EFASHION_REST_BASE_URL, USER_AGENT
from .http_client import create_http_session
from .session_store import EfashionSession


class EfashionApiError(Exception):
    pass


def _auth_headers(session: EfashionSession, *, json_content: bool = True) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Cookie": f"auth-token={session.access_token}",
        "Authorization": f"Bearer {session.access_token}",
    }
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


class EfashionClient:
    def __init__(self, session: EfashionSession) -> None:
        self.session = session
        self._client = create_http_session(
            timeout=120.0,
            headers=_auth_headers(session, json_content=False),
            allow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EfashionClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def post_rest(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{EFASHION_REST_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
        response = self._client.post(
            url,
            json=payload or {},
            headers={"Content-Type": "application/json"},
        )

        if response.status_code == 401:
            raise EfashionApiError(
                "Session EFashion expirée ou non autorisée. Reconnectez-vous."
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise EfashionApiError("Réponse EFashion invalide (JSON attendu).") from exc

        if response.status_code >= 400:
            message = body.get("message") if isinstance(body, dict) else None
            if isinstance(message, list):
                message = "; ".join(str(item) for item in message)
            if not message:
                message = response.text[:300]
            raise EfashionApiError(str(message))

        if not isinstance(body, dict):
            raise EfashionApiError("Réponse EFashion inattendue.")
        return body

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.post(
            EFASHION_API_URL,
            json={"query": query, "variables": variables or {}},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code == 401:
            raise EfashionApiError(
                "Session EFashion expirée ou non autorisée. Reconnectez-vous."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise EfashionApiError("Réponse GraphQL EFashion invalide.") from exc

        if not isinstance(payload, dict):
            raise EfashionApiError("Réponse GraphQL EFashion inattendue.")
        if payload.get("errors"):
            first = payload["errors"][0]
            message = first.get("message") if isinstance(first, dict) else str(first)
            raise EfashionApiError(str(message or "Erreur GraphQL EFashion."))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise EfashionApiError("Aucune donnée GraphQL renvoyée.")
        return data

    def get_reference_data(self) -> dict[str, Any]:
        return self.post_rest("/shootings/get-reference-data", {})

    def save_mel_draft(self, references: list[dict[str, Any]]) -> dict[str, Any]:
        return self.post_rest(
            "/shootings/save-mel-draft",
            {"dataSource": "form", "references": references},
        )

    def save_mel_choice_upload(self, references: list[dict[str, Any]]) -> dict[str, Any]:
        return self.post_rest(
            "/shootings/save-mel-choice",
            {
                "melOption": "upload",
                "dataSource": "form",
                "shootAllColors": True,
                "references": references,
            },
        )

    def check_references_exists_batch(
        self,
        references: list[dict[str, str]],
        *,
        chunk_size: int = 80,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, bool]:
        """Retourne {reference: True/False}. Sans venduPar = doublon strict sur la ref."""
        found: dict[str, bool] = {}
        clean: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in references:
            reference = str(item.get("reference") or "").strip()
            if not reference or reference in seen:
                continue
            seen.add(reference)
            clean.append({"reference": reference})
        if not clean:
            return found

        total = len(clean)
        if on_progress:
            on_progress(0, total)

        checked = 0
        for offset in range(0, len(clean), chunk_size):
            chunk = clean[offset : offset + chunk_size]
            body = self.post_rest(
                "/shootings/check-references-exists-batch",
                {"references": chunk},
            )
            results = body.get("results") or []
            for index, req in enumerate(chunk):
                reference = req["reference"]
                exists = False
                if index < len(results) and isinstance(results[index], dict):
                    exists = bool(results[index].get("exists"))
                else:
                    for item in results:
                        if (
                            isinstance(item, dict)
                            and str(item.get("reference") or "").strip() == reference
                        ):
                            exists = bool(item.get("exists"))
                            break
                found[reference] = exists
            checked += len(chunk)
            if on_progress:
                on_progress(checked, total)
        return found

    def find_main_product_by_reference(self, reference: str) -> dict[str, Any] | None:
        """Retrouve le produit principal en ligne pour une référence (exacte)."""
        reference = str(reference or "").strip()
        if not reference:
            return None
        if self.session.id_vendeur is None:
            raise EfashionApiError("id_vendeur EFashion manquant.")

        query = """
        query GetProductsPage($filter: FilterProduitInput!) {
          productsPage(filter: $filter) {
            items {
              id_produit
              reference
              reference_base
              main
              premel
              visible
              prix
              poids
            }
            total
          }
        }
        """
        data = self.graphql(
            query,
            {
                "filter": {
                    "id_vendeur": int(self.session.id_vendeur),
                    "reference": reference,
                    "premelFilter": "en_ligne",
                    "take": 50,
                    "skip": 0,
                    "orderBy": "reference",
                    "orderDir": "ASC",
                }
            },
        )
        page = data.get("productsPage") if isinstance(data, dict) else None
        items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(items, list):
            return None

        exact: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("reference") or "").strip()
            ref_base = str(item.get("reference_base") or "").strip()
            if ref == reference or ref_base == reference:
                exact.append(item)
        if not exact:
            return None

        for item in exact:
            if item.get("main") is True:
                return item
        return exact[0]

    def update_produit(self, product_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        mutation = """
        mutation UpdateProduit($input: UpdateProduitInput!) {
          updateProduit(input: $input) {
            id_produit
            reference
            prix
            poids
            id_categorie
            id_collection
            id_provenance
            main
            visible
          }
        }
        """
        payload = {"id_produit": int(product_id), **fields}
        data = self.graphql(mutation, {"input": payload})
        result = data.get("updateProduit")
        if not isinstance(result, dict):
            raise EfashionApiError("Réponse updateProduit invalide.")
        return result

    def save_produit_description(
        self, product_id: int, texts: dict[str, str]
    ) -> None:
        mutation = """
        mutation SaveProduitDescription($input: SaveProduitDescriptionInput!) {
          saveProduitDescription(input: $input)
        }
        """
        payload = {
            "id_produit": int(product_id),
            "texte_fr": texts.get("texte_fr") or "",
            "texte_uk": texts.get("texte_uk") or "",
            "texte_it": texts.get("texte_it") or "",
            "texte_es": texts.get("texte_es") or "",
            "texte_de": texts.get("texte_de") or "",
        }
        self.graphql(mutation, {"input": payload})

    def get_upload_products(
        self,
        *,
        shooting_ids: list[int] | None = None,
        reference: str | None = None,
        page: int = 1,
        limit: int = 100,
    ) -> dict[str, Any]:
        if self.session.id_vendeur is None:
            raise EfashionApiError("id_vendeur EFashion manquant.")

        query = """
        query GetUploadProducts(
          $vendorId: Int!
          $shootingIds: [Int!]
          $reference: String
          $page: Int
          $limit: Int
        ) {
          uploadProducts(
            vendorId: $vendorId
            shootingIds: $shootingIds
            reference: $reference
            page: $page
            limit: $limit
            premelFilter: "brouillon"
          ) {
            items {
              id
              reference
              referenceBase
              idShooting
              premel
              main
            }
            total
            page
            totalPages
          }
        }
        """
        variables: dict[str, Any] = {
            "vendorId": self.session.id_vendeur,
            "page": page,
            "limit": limit,
        }
        if shooting_ids:
            variables["shootingIds"] = shooting_ids
        if reference:
            variables["reference"] = reference

        data = self.graphql(query, variables)
        page_data = data.get("uploadProducts")
        if not isinstance(page_data, dict):
            return {"items": [], "total": 0}
        return page_data

    def publish_brouillon_bulk(self, product_ids: list[int]) -> int:
        """Met en ligne des brouillons (premel=0, visible=true). Retourne le nombre publié."""
        if not product_ids:
            return 0
        if self.session.id_vendeur is None:
            raise EfashionApiError("id_vendeur EFashion manquant.")

        mutation = """
        mutation PublishBrouillonBulk($id_produits: [Int!]!, $id_vendeur: Int!) {
          publishBrouillonBulk(id_produits: $id_produits, id_vendeur: $id_vendeur)
        }
        """
        data = self.graphql(
            mutation,
            {
                "id_produits": [int(pid) for pid in product_ids],
                "id_vendeur": int(self.session.id_vendeur),
            },
        )
        return int(data.get("publishBrouillonBulk") or 0)

    def soft_delete_produits(self, product_ids: list[int]) -> bool:
        """Soft-delete EFashion (supprimer=1), comme le back-office vendeur."""
        if not product_ids:
            return True
        unique = sorted({int(pid) for pid in product_ids})
        mutation = """
        mutation SoftDeleteProduits($ids: [Int!]!) {
          softDeleteProduits(ids: $ids)
        }
        """
        data = self.graphql(mutation, {"ids": unique})
        return bool(data.get("softDeleteProduits"))

    def set_produits_visible(self, product_ids: list[int], *, visible: bool) -> None:
        """Coche/décoche la case Visible (hors ligne = visible false)."""
        if not product_ids:
            return
        unique_ids = sorted({int(pid) for pid in product_ids})
        mutation = """
        mutation SetProduitsVisible($ids: [Int!]!, $visible: Boolean!) {
          setProduitsVisible(ids: $ids, visible: $visible)
        }
        """
        try:
            self.graphql(
                mutation,
                {
                    "ids": unique_ids,
                    "visible": visible,
                },
            )
            return
        except EfashionApiError:
            # Fallback produit par produit (même effet sur la case Visible).
            errors: list[str] = []
            for product_id in unique_ids:
                try:
                    self.update_produit(product_id, {"visible": visible})
                except EfashionApiError as exc:
                    errors.append(f"{product_id}: {exc}")
            if errors:
                raise EfashionApiError(
                    "Impossible de mettre à jour la visibilité : "
                    + " ; ".join(errors[:4])
                ) from None

    def find_products_by_reference(self, reference: str) -> list[dict[str, Any]]:
        """Fiches en ligne pour une référence (principale + couleurs), clés normalisées."""
        reference = str(reference or "").strip()
        if not reference or self.session.id_vendeur is None:
            return []
        query = """
        query GetProductsPage($filter: FilterProduitInput!) {
          productsPage(filter: $filter) {
            items {
              id_produit
              reference
              reference_base
              main
              visible
            }
            total
          }
        }
        """
        data = self.graphql(
            query,
            {
                "filter": {
                    "id_vendeur": int(self.session.id_vendeur),
                    "reference": reference,
                    "premelFilter": "en_ligne",
                    "take": 100,
                    "skip": 0,
                    "orderBy": "reference",
                    "orderDir": "ASC",
                }
            },
        )
        page = data.get("productsPage") if isinstance(data, dict) else None
        items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(items, list):
            return []

        results: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in items:
            if not isinstance(item, dict) or item.get("id_produit") is None:
                continue
            ref = str(item.get("reference") or "").strip()
            ref_base = str(item.get("reference_base") or "").strip()
            if ref != reference and ref_base != reference:
                if not (
                    ref.startswith(reference + "-")
                    or ref_base.startswith(reference + "-")
                ):
                    continue
            product_id = int(item["id_produit"])
            if product_id in seen:
                continue
            seen.add(product_id)
            results.append(
                {
                    "id": product_id,
                    "id_produit": product_id,
                    "reference": ref,
                    "referenceBase": ref_base,
                    "reference_base": ref_base,
                    "main": bool(item.get("main")),
                    "visible": item.get("visible"),
                }
            )
        return results

    def find_product_ids_by_reference(self, reference: str) -> list[int]:
        """Tous les id_produit en ligne pour une référence (toutes couleurs)."""
        return [
            int(item["id"])
            for item in self.find_products_by_reference(reference)
            if item.get("id") is not None
        ]

    def duplicate_with_new_color(
        self,
        id_produit: int,
        *,
        couleur_id: int,
        couleur_name: str,
    ) -> dict[str, Any]:
        """Crée une variante secondaire (main=0) avec une nouvelle couleur."""
        mutation = """
        mutation DuplicateWithNewColor(
          $idProduit: Int!
          $couleurId: Int!
          $couleurName: String!
        ) {
          duplicateWithNewColor(
            idProduit: $idProduit
            couleurId: $couleurId
            couleurName: $couleurName
          ) {
            id_produit
            reference
            reference_base
            main
            visible
            premel
          }
        }
        """
        data = self.graphql(
            mutation,
            {
                "idProduit": int(id_produit),
                "couleurId": int(couleur_id),
                "couleurName": str(couleur_name),
            },
        )
        result = data.get("duplicateWithNewColor")
        if not isinstance(result, dict) or result.get("id_produit") is None:
            raise EfashionApiError("Réponse duplicateWithNewColor invalide.")
        return result

    def create_produit_stock(
        self,
        id_produit: int,
        *,
        id_couleur: int,
        value: int,
        taille: str | None = None,
    ) -> None:
        mutation = """
        mutation CreateProduitStock($input: CreateProduitStockInput!) {
          createProduitStock(input: $input) {
            id_produit_stock
          }
        }
        """
        payload: dict[str, Any] = {
            "id_produit": int(id_produit),
            "id_couleur": int(id_couleur),
            "value": int(value),
        }
        if taille:
            payload["taille"] = taille
        self.graphql(mutation, {"input": payload})

    def list_produit_stocks(self, id_produit: int) -> list[dict[str, Any]]:
        query = """
        query ProduitsStockPage($filter: FilterProduitStockInput!) {
          produitsStockPage(filter: $filter) {
            items {
              id_produit_stock
              id_produit
              id_couleur
              value
              taille
            }
            total
          }
        }
        """
        data = self.graphql(
            query,
            {
                "filter": {
                    "id_produit": int(id_produit),
                    "take": 200,
                    "skip": 0,
                }
            },
        )
        page = data.get("produitsStockPage") if isinstance(data, dict) else None
        items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def update_produit_stock(
        self,
        id_produit_stock: int,
        *,
        value: int,
        id_couleur: int | None = None,
        taille: str | None = None,
    ) -> None:
        mutation = """
        mutation UpdateProduitStock($input: UpdateProduitStockInput!) {
          updateProduitStock(input: $input) {
            id_produit_stock
            value
          }
        }
        """
        payload: dict[str, Any] = {
            "id_produit_stock": int(id_produit_stock),
            "value": int(value),
        }
        if id_couleur is not None:
            payload["id_couleur"] = int(id_couleur)
        if taille is not None:
            payload["taille"] = taille
        self.graphql(mutation, {"input": payload})

    def upsert_produit_stock(
        self,
        id_produit: int,
        *,
        id_couleur: int,
        value: int,
        taille: str | None = None,
    ) -> None:
        """Crée ou met à jour la ligne stock (couleur ± taille)."""
        existing = self.list_produit_stocks(id_produit)
        taille_norm = (taille or "").strip() or None
        match_id = None
        for row in existing:
            if int(row.get("id_couleur") or 0) != int(id_couleur):
                continue
            row_taille = str(row.get("taille") or "").strip() or None
            if row_taille == taille_norm:
                match_id = row.get("id_produit_stock")
                break
        if match_id is not None:
            self.update_produit_stock(int(match_id), value=int(value))
            return
        self.create_produit_stock(
            id_produit,
            id_couleur=int(id_couleur),
            value=int(value),
            taille=taille_norm,
        )

    def list_produit_compositions(self, id_produit: int) -> list[dict[str, Any]]:
        query = """
        query ProduitsCompositionsPage($filter: FilterProduitCompositionInput!) {
          produitsCompositionsPage(filter: $filter) {
            items {
              id_produit_composition
              id_produit
              id_composition
              id_composition_localisation
              value
            }
            total
          }
        }
        """
        data = self.graphql(
            query,
            {
                "filter": {
                    "id_produit": int(id_produit),
                    "take": 100,
                    "skip": 0,
                }
            },
        )
        page = (
            data.get("produitsCompositionsPage") if isinstance(data, dict) else None
        )
        items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def remove_produit_composition(self, id_produit_composition: int) -> bool:
        mutation = """
        mutation RemoveProduitComposition($id: Int!) {
          removeProduitComposition(id: $id)
        }
        """
        data = self.graphql(mutation, {"id": int(id_produit_composition)})
        return bool(data.get("removeProduitComposition"))

    def create_produit_composition(
        self,
        id_produit: int,
        *,
        id_composition: int,
        id_composition_localisation: int,
        value: float,
    ) -> None:
        mutation = """
        mutation CreateProduitComposition($input: CreateProduitCompositionInput!) {
          createProduitComposition(input: $input) {
            id_produit_composition
          }
        }
        """
        self.graphql(
            mutation,
            {
                "input": {
                    "id_produit": int(id_produit),
                    "id_composition": int(id_composition),
                    "id_composition_localisation": int(id_composition_localisation),
                    "value": float(value),
                }
            },
        )

    def replace_produit_compositions(
        self,
        id_produit: int,
        compositions: list[dict[str, Any]],
    ) -> None:
        """Remplace toute la composition d'une fiche."""
        for row in self.list_produit_compositions(id_produit):
            comp_id = row.get("id_produit_composition")
            if comp_id is None:
                continue
            try:
                self.remove_produit_composition(int(comp_id))
            except EfashionApiError:
                continue
        for item in compositions:
            if not isinstance(item, dict):
                continue
            id_composition = item.get("id_composition")
            localisation = item.get("localisation")
            pourcentage = item.get("pourcentage", item.get("value"))
            if id_composition is None or localisation in (None, ""):
                continue
            try:
                loc_id = int(localisation)
                pct = float(pourcentage if pourcentage is not None else 100)
            except (TypeError, ValueError):
                continue
            self.create_produit_composition(
                id_produit,
                id_composition=int(id_composition),
                id_composition_localisation=loc_id,
                value=pct,
            )

    def get_product_photo_urls(self, product_id: int) -> list[str]:
        """URLs publiques des photos produit (ordre d'affichage)."""
        url = (
            f"{EFASHION_REST_BASE_URL.rstrip('/')}"
            f"/api/product-photos/{int(product_id)}"
        )
        response = self._client.get(url)
        if response.status_code == 401:
            raise EfashionApiError(
                "Session EFashion expirée ou non autorisée. Reconnectez-vous."
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise EfashionApiError("Réponse product-photos invalide.") from exc
        if response.status_code >= 400 or not (
            isinstance(body, dict) and body.get("success")
        ):
            message = body.get("message") if isinstance(body, dict) else None
            raise EfashionApiError(str(message or "Impossible de lister les photos."))
        photos = body.get("photos") if isinstance(body, dict) else None
        if not isinstance(photos, list):
            return []
        return [str(item) for item in photos if item]

    def delete_product_photo(self, product_id: int, filename: str) -> None:
        """Supprime une photo produit (toutes tailles S3)."""
        filename = str(filename or "").strip()
        if not filename:
            return
        # Ne garder que le nom de fichier
        if "/" in filename:
            filename = filename.rsplit("/", 1)[-1]
        self.post_rest(
            "/api/product-photo/delete",
            {"productId": str(int(product_id)), "filename": filename},
        )

    def clear_product_photos(self, product_id: int) -> int:
        """Supprime toutes les photos d'une fiche. Retourne le nombre supprimé."""
        urls = self.get_product_photo_urls(product_id)
        deleted = 0
        for url in urls:
            name = str(url).rsplit("/", 1)[-1].split("?", 1)[0]
            if not name:
                continue
            try:
                self.delete_product_photo(product_id, name)
                deleted += 1
            except EfashionApiError:
                continue
        return deleted

    def replace_product_photos(
        self,
        product_id: int,
        files: list[tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        """Efface les photos existantes puis upload le nouveau jeu PFS."""
        self.clear_product_photos(product_id)
        if not files:
            return {"success": True, "cleared_only": True}
        return self.upload_product_photos(product_id, files)

    def toggle_main_product(
        self, product_id: int, *, apply_offline_rule: bool = False
    ) -> bool:
        mutation = """
        mutation ToggleMainProduct($idProduit: Int!, $applyOfflineRule: Boolean) {
          toggleMainProduct(
            idProduit: $idProduit
            applyOfflineRule: $applyOfflineRule
          )
        }
        """
        data = self.graphql(
            mutation,
            {
                "idProduit": int(product_id),
                "applyOfflineRule": bool(apply_offline_rule),
            },
        )
        return bool(data.get("toggleMainProduct"))

    def upload_product_photos(
        self,
        product_id: int,
        files: list[tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        """Upload photos via l'API officielle (resize + S3 côté backend)."""
        if not files:
            raise EfashionApiError("Aucun fichier photo à uploader.")

        url = f"{EFASHION_REST_BASE_URL.rstrip('/')}/api/upload-product-photo"
        mp = CurlMime.from_list(
            [
                {
                    "name": "photos",
                    "content_type": content_type or "image/jpeg",
                    "filename": filename,
                    "data": content,
                }
                for filename, content, content_type in files
            ]
        )
        try:
            response = self._client.post(
                url,
                data={"productId": str(product_id)},
                multipart=mp,
            )
        finally:
            mp.close()

        if response.status_code == 401:
            raise EfashionApiError(
                "Session EFashion expirée ou non autorisée. Reconnectez-vous."
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise EfashionApiError("Réponse upload photo invalide.") from exc

        if response.status_code >= 400 or not (
            isinstance(body, dict) and body.get("success")
        ):
            message = body.get("message") if isinstance(body, dict) else None
            if not message:
                message = response.text[:300]
            raise EfashionApiError(str(message))

        if not isinstance(body, dict):
            raise EfashionApiError("Réponse upload photo inattendue.")
        return body

