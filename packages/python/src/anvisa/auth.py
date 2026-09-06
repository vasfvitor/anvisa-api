"""Credentials and the OAuth2 client-credentials flow against ANVISA's Keycloak.

Tokens last 1740 s and there is no refresh token, so `TokenAuth` simply requests a new
one when the cached token is within `margin` seconds of expiring, or after a 401.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from .errors import AuthError, CredentialsError

TOKEN_URL = (
    "https://acesso.prd.apps.anvisa.gov.br/auth/realms/externo/protocol/openid-connect/token"
)
CREDENTIALS_FILE = Path("~/.config/anvisa/credentials.env")


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    token_url: str = TOKEN_URL

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        path: Path = CREDENTIALS_FILE,
    ) -> Credentials:
        """From ANVISA_CLIENT_ID/ANVISA_CLIENT_SECRET, else CLIENT_ID=/CLIENT_SECRET= in `path`."""
        env = os.environ if env is None else env
        file_values = read_env_file(path.expanduser())
        client_id = env.get("ANVISA_CLIENT_ID") or file_values.get("CLIENT_ID")
        client_secret = env.get("ANVISA_CLIENT_SECRET") or file_values.get("CLIENT_SECRET")
        if not (client_id and client_secret):
            raise CredentialsError(
                "no ANVISA credentials: set ANVISA_CLIENT_ID and ANVISA_CLIENT_SECRET, "
                f"or write CLIENT_ID=... and CLIENT_SECRET=... to {path} (chmod 600). "
                "Credentials come from https://api.anvisa.gov.br/ (login Gov.br)."
            )
        return cls(client_id, client_secret)

    def __repr__(self) -> str:  # never print the secret
        return f"Credentials(client_id={self.client_id!r}, client_secret='***')"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse `KEY=VALUE` lines (comments, blank lines, `export` and quotes allowed)."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


class TokenAuth(httpx.Auth):
    """httpx auth hook: fetches and caches the bearer token, retries once on 401."""

    requires_response_body = True

    def __init__(
        self,
        credentials: Credentials,
        *,
        margin: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.credentials = credentials
        self.margin = margin
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def token_valid(self) -> bool:
        return self._token is not None and self._clock() < self._expires_at - self.margin

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if not self.token_valid:
            yield from self._fetch_token(request)
        request.headers["Authorization"] = f"Bearer {self._token}"
        response = yield request
        if response.status_code == 401:
            yield from self._fetch_token(request)
            request.headers["Authorization"] = f"Bearer {self._token}"
            yield request

    def _fetch_token(
        self, original: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        token_request = httpx.Request(
            "POST",
            self.credentials.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
            },
            # the token endpoint sits behind the same Cloudflare rules: reuse the caller's UA
            headers={"User-Agent": original.headers.get("User-Agent", "anvisa-python")},
        )
        response = yield token_request
        if response.status_code != 200:
            raise AuthError(f"token request failed: HTTP {response.status_code}")
        body = response.json()
        self._token = body["access_token"]
        self._expires_at = self._clock() + float(body.get("expires_in", 0))
