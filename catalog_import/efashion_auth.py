from __future__ import annotations

from typing import Any

from .config import EFASHION_API_URL
from .http_client import create_http_session
from .session_store import AppSession, EfashionSession, SessionStore


class EfashionAuthError(Exception):
    pass


LOGIN_MUTATION = """
mutation Login($email: String!, $password: String!, $rememberMe: Boolean!) {
  login(email: $email, password: $password, rememberMe: $rememberMe) {
    user {
      id_vendeur
      email
      nomBoutique
    }
    message
  }
}
"""


def _parse_graphql_errors(payload: dict[str, Any]) -> str:
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            message = first.get("message")
            if message:
                return str(message)
    return "Connexion EFashion refusée."


def login_efashion(
    email: str,
    password: str,
    store: SessionStore,
    existing: AppSession,
) -> EfashionSession:
    with create_http_session(timeout=(10.0, 20.0), allow_redirects=True) as client:
        response = client.post(
            EFASHION_API_URL,
            json={
                "query": LOGIN_MUTATION,
                "variables": {
                    "email": email,
                    "password": password,
                    "rememberMe": False,
                },
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        if response.status_code >= 400:
            raise EfashionAuthError(
                f"Connexion EFashion impossible (HTTP {response.status_code})."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise EfashionAuthError("Réponse EFashion invalide (JSON attendu).") from exc

        if not isinstance(payload, dict):
            raise EfashionAuthError("Réponse EFashion inattendue.")

        if payload.get("errors"):
            raise EfashionAuthError(_parse_graphql_errors(payload))

        data = payload.get("data")
        if not isinstance(data, dict):
            raise EfashionAuthError("Connexion EFashion refusée.")

        login_data = data.get("login")
        if not isinstance(login_data, dict):
            raise EfashionAuthError("Connexion EFashion refusée.")

        user = login_data.get("user")
        if not isinstance(user, dict):
            raise EfashionAuthError("Compte EFashion introuvable après connexion.")

        access_token = response.cookies.get("auth-token", "")
        if not access_token:
            raise EfashionAuthError(
                "Token EFashion manquant. Vérifiez l'URL API ou vos identifiants."
            )

        id_vendeur = user.get("id_vendeur")
        efashion_session = EfashionSession(
            email=str(user.get("email") or email),
            access_token=str(access_token),
            id_vendeur=int(id_vendeur) if id_vendeur is not None else None,
        )

    existing.efashion = efashion_session
    store.save(existing)
    return efashion_session
