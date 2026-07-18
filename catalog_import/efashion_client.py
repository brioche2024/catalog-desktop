from __future__ import annotations

from typing import Any

import httpx

from .config import EFASHION_API_URL, EFASHION_REST_BASE_URL, USER_AGENT
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
        self._client = httpx.Client(
            timeout=120.0,
            headers=_auth_headers(session, json_content=False),
            follow_redirects=True,
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

    def upload_product_photos(
        self,
        product_id: int,
        files: list[tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        """Upload photos via l'API officielle (resize + S3 côté backend)."""
        if not files:
            raise EfashionApiError("Aucun fichier photo à uploader.")

        url = f"{EFASHION_REST_BASE_URL.rstrip('/')}/api/upload-product-photo"
        multipart = [
            ("photos", (filename, content, content_type or "image/jpeg"))
            for filename, content, content_type in files
        ]
        response = self._client.post(
            url,
            data={"productId": str(product_id)},
            files=multipart,
        )

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

