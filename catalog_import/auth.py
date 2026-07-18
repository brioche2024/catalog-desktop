import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .config import (
    PFS_LOGIN_PAGE_URL,
    PFS_OAUTH_URL,
    PFS_SITE_ORIGIN,
    USER_AGENT,
)
from .session_store import AppSession, PfsSession, SessionStore


class AuthError(Exception):
    pass


def _extract_session_token(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    field = soup.select_one('input[name="session"]')
    if field and field.get("value"):
        return str(field["value"])
    match = re.search(r'name="session"\s+value="([^"]*)"', html)
    if match and match.group(1):
        return match.group(1)
    return "marketplace"


def _parse_oauth_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error_description") or payload.get("error")
            if message:
                return str(message)
    except ValueError:
        pass
    return f"Connexion PFS refusée (HTTP {response.status_code})."


def _extract_access_token(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    token = payload.get("access_token")
    if token:
        return str(token)
    data = payload.get("data")
    if isinstance(data, dict) and data.get("access_token"):
        return str(data["access_token"])
    return None


def login_pfs(
    email: str,
    password: str,
    store: SessionStore | None = None,
    existing: AppSession | None = None,
) -> PfsSession:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    }
    timeout = httpx.Timeout(20.0, connect=10.0)

    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        page = client.get(PFS_LOGIN_PAGE_URL)
        page.raise_for_status()

        session_token = _extract_session_token(page.text)
        oauth = client.post(
            PFS_OAUTH_URL,
            data={
                "email": email,
                "password": password,
                "session": session_token,
            },
            headers={
                "Referer": PFS_LOGIN_PAGE_URL,
                "Origin": PFS_SITE_ORIGIN,
            },
            follow_redirects=False,
        )

        if oauth.status_code in {301, 302, 303, 307, 308}:
            raise AuthError(
                "Connexion PFS : redirection inattendue. "
                "Vérifiez que vous utilisez un compte vendeur PFS."
            )

        if oauth.status_code != 200:
            raise AuthError(_parse_oauth_error(oauth))

        try:
            payload = oauth.json()
        except ValueError as exc:
            raise AuthError("Réponse de connexion PFS invalide (JSON attendu).") from exc

        access_token = _extract_access_token(payload)
        if not access_token:
            raise AuthError("Token d'accès PFS manquant après connexion.")

        pfs_session = PfsSession(email=email, access_token=str(access_token))

    if store:
        app_session = existing or AppSession()
        app_session.pfs = pfs_session
        store.save(app_session)

    return pfs_session
