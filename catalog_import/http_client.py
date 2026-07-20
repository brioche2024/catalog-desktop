from __future__ import annotations

from curl_cffi.requests import Response, Session
from curl_cffi.requests.exceptions import ConnectionError, Timeout

from .config import BROWSER_IMPERSONATE

NETWORK_ERRORS = (Timeout, ConnectionError)


def create_http_session(
    *,
    headers: dict[str, str] | None = None,
    timeout: float | tuple[float, float] = 60.0,
    allow_redirects: bool = True,
) -> Session:
    return Session(
        impersonate=BROWSER_IMPERSONATE,
        headers=headers,
        timeout=timeout,
        allow_redirects=allow_redirects,
    )
